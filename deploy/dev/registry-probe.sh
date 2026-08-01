#!/usr/bin/env bash
# Is the Controller's KV registry actually populated? (issue #13)
#
# The Controller's `kv_pool` is in-memory and admission is one-shot per chunk, so
# there are states where every lookup returns {} and NOTHING in the logs says so —
# both arms of an experiment then look identical for the wrong reason. This probe
# detects that without patching anything.
#
# How it works: send the same long prefix N times. With a populated registry
# `kvaware` pins every request to the Instance holding the prefix. With an empty
# one it falls back to QPS routing and spreads them. So N/N on one engine = live,
# a split = dead.
#
# Usage:  ./registry-probe.sh [seed] [n]
# Exit:   0 = registry live, 1 = registry empty (do not measure)
#
# ALWAYS use a seed you have not used before on these engine pods: a prefix whose
# admits were lost is poisoned for the life of the engine process (see README).
set -uo pipefail

NS="${NS:-cache-llm}"
URL="${URL:-https://llm-cache-llm.apps.gapu-2.customers.k8s.co.il/v1/completions}"
MODEL="${MODEL:-Qwen/Qwen2.5-3B-Instruct}"
SEED="${1:-$$}"
N="${2:-4}"
REQ=$(mktemp)
trap 'rm -f "$REQ"' EXIT

# >2000 tokens, or kvaware never takes the cache path (kv_aware_threshold)
python3 - "$SEED" "$MODEL" > "$REQ" <<'PY'
import json, random, sys
rng = random.Random(int(sys.argv[1]))
vocab = ["alpha","bravo","charlie","delta","echo","foxtrot","golf","hotel","india","juliet"]
prompt = " ".join(rng.choice(vocab) for _ in range(3000)) + " Summarize:"
print(json.dumps({"model": sys.argv[2], "prompt": prompt,
                  "max_tokens": 4, "temperature": 0}))
PY

echo "==> $N identical requests, seed $SEED"
ids=()
for _ in $(seq "$N"); do
  id=$(curl -k -s -m 240 -X POST "$URL" -H 'Content-Type: application/json' --data @"$REQ" \
       | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])' 2>/dev/null)
  [ -n "$id" ] || { echo "request failed — is the stack serving?" >&2; exit 1; }
  ids+=("$id")
done

best=0
for pod in $(oc get pods -n "$NS" -o name | grep vllm | sed 's|pod/||'); do
  log=$(oc logs "$pod" -n "$NS" --since=10m 2>/dev/null)
  n=0
  for id in "${ids[@]}"; do echo "$log" | grep -qa "$id" && n=$((n+1)); done
  echo "    $pod: $n/$N"
  [ "$n" -gt "$best" ] && best=$n
done

if [ "$best" -eq "$N" ]; then
  echo "==> registry LIVE — all $N pinned to one instance"
  exit 0
fi
echo "==> registry EMPTY — requests were spread, so kvaware is running blind."
echo "    Wait for both workers to re-register (~40 s after a router restart),"
echo "    then re-probe with a FRESH seed. Do not measure until this passes."
exit 1

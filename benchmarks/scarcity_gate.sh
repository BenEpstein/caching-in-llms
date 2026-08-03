#!/usr/bin/env bash
# Scarcity gate (methodology amendment, issue #3, 2026-08-03).
#
# Before spending the sweep, prove the memory config actually created scarcity.
# Deploys ONE arm (roundrobin - routing is irrelevant here, we are measuring the
# engines), restarts the engines cold, verifies the realised KV pool from the
# engine's own startup log (validity rule 5), then makes two warm-up passes over
# the 64-prefix pool and reads vLLM's OWN `Prefix cache hit rate`.
#
# Why vLLM's metric and not LMCache's: the LMCache figure is each engine's hit
# rate against its own CPU tier, which saturated at ~0.95 on every arm in the
# pilot. vLLM's prefix-cache hit rate is the HBM residency that scarcity moves.
#
# PASS  = pass-2 hit rate well below the pilot's ~0.95 saturation -> engines are
#         evicting, there is a placement decision to get right, run the sweep.
# FAIL  = still ~0.95 -> scarcity did not take. Stop. Re-derive the sizing.
#
# Usage:  ./scarcity_gate.sh
set -euo pipefail

NS="${NS:-cache-llm}"
RELEASE="${RELEASE:-stack}"
CHART="${CHART:-vllm/vllm-stack}"
CHART_VERSION="${CHART_VERSION:-0.1.11}"
BASE_URL="${BASE_URL:-https://llm-cache-llm.apps.gapu-2.customers.k8s.co.il}"
MODEL="${MODEL:-Qwen/Qwen2.5-3B-Instruct}"
ROUTER_DEPLOY="${ROUTER_DEPLOY:-stack-deployment-router}"
ENGINE_DEPLOY="${ENGINE_DEPLOY:-stack-llm-deployment-vllm}"

BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$BENCH_DIR/.." && pwd)"

# The pilot saturated here; the gate is "clearly below", not "any drop at all".
PILOT_SATURATION=0.95
THRESHOLD="${THRESHOLD:-0.80}"

echo "==> scarcity gate: deploying roundrobin at the amended memory config"
python3 "$BENCH_DIR/freeze_workloads.py"

helm upgrade --install "$RELEASE" "$CHART" -n "$NS" --version "$CHART_VERSION" \
  -f "$REPO_ROOT/deploy/values-baseline-kvaware.yaml" \
  --set routerSpec.routingLogic=roundrobin

if oc get deploy "$ROUTER_DEPLOY" -n "$NS" \
    -o jsonpath='{.spec.template.spec.volumes[*].name}' | grep -q router-patch; then
  NS="$NS" "$REPO_ROOT/deploy/dev/revert-router-patch.sh"
fi
# baseline arm: HF_HOME on (#21), loadaware vars off so nothing leaks
oc set env "deploy/$ROUTER_DEPLOY" -n "$NS" HF_HOME=/tmp/hf LOADAWARE_ALPHA- LOADAWARE_BETA-
oc rollout status "deploy/$ROUTER_DEPLOY" -n "$NS" --timeout=10m

echo "==> cold engine restart"
oc rollout restart "deploy/$ENGINE_DEPLOY" -n "$NS"
oc rollout status "deploy/$ENGINE_DEPLOY" -n "$NS" --timeout=30m

# ---- validity rule 5: realised KV pool, from the engine's own log -----------
echo "==> realised KV pool (validity rule 5)"
POOL_OK=0
for pod in $(oc get pods -n "$NS" -l model=llm -o jsonpath='{.items[*].metadata.name}'); do
  line=$(oc logs -n "$NS" "$pod" 2>/dev/null \
    | grep -m1 "GPU KV cache size" || true)
  echo "    $pod: ${line#*] }"
  [ -n "$line" ] && POOL_OK=1
done
[ "$POOL_OK" = 1 ] || { echo "could not read the KV pool from any engine log" >&2; exit 1; }

# ---- two warm-up passes; pass 2 is the measurement -------------------------
echo "==> warm-up pass 1 (populate)"
python3 "$BENCH_DIR/warmup.py" --base-url "$BASE_URL" --model "$MODEL" --insecure --passes 1

PASS2_START=$(date +%s)
echo "==> warm-up pass 2 (the gate: every prefix was seen in pass 1)"
python3 "$BENCH_DIR/warmup.py" --base-url "$BASE_URL" --model "$MODEL" --insecure --passes 1

# vLLM logs "Prefix cache hit rate: N%" on its periodic stats line. Read only
# lines emitted during pass 2 - pass 1 necessarily misses and would drag it down
# for the wrong reason.
since=$(( $(date +%s) - PASS2_START )); [ "$since" -lt 1 ] && since=1
rates=$(for pod in $(oc get pods -n "$NS" -l model=llm -o jsonpath='{.items[*].metadata.name}'); do
  oc logs -n "$NS" "$pod" --since="${since}s" 2>/dev/null \
    | grep -o "Prefix cache hit rate: [0-9.]*%" | grep -o "[0-9.]*" || true
done)

[ -n "$rates" ] || { echo "no prefix-cache-hit-rate lines in the pass-2 window" >&2; exit 1; }
echo "$rates" | python3 -c "
import sys
xs=[float(l)/100 for l in sys.stdin if l.strip()]
mean=sum(xs)/len(xs)
print(f'==> pass-2 vLLM prefix cache hit rate: mean {mean:.3f} over {len(xs)} samples '
      f'(min {min(xs):.3f}, max {max(xs):.3f})')
print(f'    pilot saturation was ~$PILOT_SATURATION; gate threshold $THRESHOLD')
if mean < $THRESHOLD:
    print('==> PASS: engines are evicting - there is a placement decision to get right')
else:
    print('==> FAIL: still saturated - scarcity did not take, do NOT run the sweep')
    sys.exit(1)
"

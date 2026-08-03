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

echo "==> cold, stale-free start"
# roundrobin: no LMCache controller, so no registrations to wait for
NS="$NS" ROUTER_DEPLOY="$ROUTER_DEPLOY" ENGINE_DEPLOY="$ENGINE_DEPLOY" \
  EXPECT_REGISTRATIONS=0 "$BENCH_DIR/cold_start.sh"

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

# ---- warm up, then measure a load burst ------------------------------------
# NOT the `Prefix cache hit rate` field on vLLM's periodic log line: that is
# CUMULATIVE SINCE ENGINE START, so a time-windowed grep of it silently reports
# whatever the running average happened to be (it read 0.085 mid-warm-up on
# 2026-08-03 while the true windowed rate was 0.889). Use the counters and take
# a delta.
#
# Measure under LOAD, not under warm-up: at warm-up concurrency the in-flight KV
# is negligible, so almost the whole pool is free for retention and the hit rate
# flatters the config. The experiment runs loaded; the gate must too.
snapshot() {
  for pod in $(oc get pods -n "$NS" -l model=llm -o jsonpath='{.items[*].metadata.name}'); do
    oc exec -n "$NS" "$pod" -- curl -s localhost:8000/metrics 2>/dev/null \
      | grep -E "^vllm:prefix_cache_(queries|hits)_total" \
      | awk -v P="$pod" '{split($0,a," "); print P, $1, a[2]}'
  done
}

echo "==> warm-up"
python3 "$BENCH_DIR/warmup.py" --base-url "$BASE_URL" --model "$MODEL" --insecure --passes 1

snapshot > /tmp/scarcity_before.txt
echo "==> load burst (seed 1 at ${PROBE_RATE:-7.5} req/s)"
python3 "$BENCH_DIR/load_driver.py" \
  --base-url "$BASE_URL" --model "$MODEL" --insecure \
  --workload "$BENCH_DIR/workloads/seed-1.jsonl" \
  --rate "${PROBE_RATE:-7.5}" --seed 1 --out /tmp/scarcity_probe.csv
snapshot > /tmp/scarcity_after.txt

python3 - "$THRESHOLD" "$PILOT_SATURATION" <<'PY'
import sys
thr, sat = float(sys.argv[1]), float(sys.argv[2])
def load(f):
    d = {}
    for line in open(f):
        pod, name, val = line.split()
        d[(pod, "hits" if "hits" in name else "queries")] = float(val)
    return d
b, a = load("/tmp/scarcity_before.txt"), load("/tmp/scarcity_after.txt")
tq = th = 0.0
for pod in sorted({k[0] for k in b}):
    dq = a[(pod, "queries")] - b[(pod, "queries")]
    dh = a[(pod, "hits")] - b[(pod, "hits")]
    tq += dq; th += dh
    print(f"    {pod[-12:]}: queries +{dq:,.0f} hits +{dh:,.0f} "
          f"rate {dh/dq:.3f}" if dq else f"    {pod[-12:]}: no traffic")
rate = th / tq
print(f"==> windowed prefix cache hit rate under load: {rate:.3f} "
      f"(pilot saturation ~{sat}, gate threshold {thr})")
if rate < thr:
    print("==> PASS: engines are evicting - there is a placement decision to get right")
else:
    print("==> FAIL: scarcity did not take. Do NOT run the sweep; re-derive the sizing.")
    sys.exit(1)
PY

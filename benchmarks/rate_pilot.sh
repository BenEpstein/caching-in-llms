#!/usr/bin/env bash
# Step 0 of the methodology (issue #3): find the TTFT-p95 knee on kvaware,
# then freeze ~75% of it as the fixed open-loop rate for EVERY sweep cell.
#
# Deploys kvaware itself (the gate leaves roundrobin behind, and the knee is
# arm-specific), restarts the engines cold, warms up, then ramps the rates below
# with a 200-request pilot workload (seed 999 - same frozen pool, so it does not
# poison measurement prefixes) and prints one summary per rate; pick the knee
# by eye and pass rate ≈ 0.75 × knee to run_sweep.sh.
#
# Usage:  ./rate_pilot.sh [rate ...]        (default: 2 4 6 8 10)
set -euo pipefail

BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$BENCH_DIR/.." && pwd)"
BASE_URL="${BASE_URL:-https://llm-cache-llm.apps.gapu-2.customers.k8s.co.il}"
MODEL="${MODEL:-Qwen/Qwen2.5-3B-Instruct}"
NS="${NS:-cache-llm}"
RELEASE="${RELEASE:-stack}"
CHART="${CHART:-vllm/vllm-stack}"
CHART_VERSION="${CHART_VERSION:-0.1.11}"
ROUTER_DEPLOY="${ROUTER_DEPLOY:-stack-deployment-router}"
ENGINE_DEPLOY="${ENGINE_DEPLOY:-stack-llm-deployment-vllm}"
if [ $# -gt 0 ]; then RATES=("$@"); else RATES=(2 4 6 8 10); fi

echo "==> deploying kvaware for the pilot"
helm upgrade --install "$RELEASE" "$CHART" -n "$NS" --version "$CHART_VERSION" \
  -f "$REPO_ROOT/deploy/values-baseline-kvaware.yaml"
oc set env "deploy/$ROUTER_DEPLOY" -n "$NS" HF_HOME=/tmp/hf LOADAWARE_ALPHA- LOADAWARE_BETA-
oc rollout status "deploy/$ROUTER_DEPLOY" -n "$NS" --timeout=10m
NS="$NS" ROUTER_DEPLOY="$ROUTER_DEPLOY" ENGINE_DEPLOY="$ENGINE_DEPLOY" \
  "$BENCH_DIR/cold_start.sh"
"$REPO_ROOT/deploy/dev/registry-probe.sh" "$(date +%s)"

PILOT_DIR="${PILOT_DIR:-results/rate-pilot}"
mkdir -p "$PILOT_DIR"
PILOT_WORKLOAD="$PILOT_DIR/pilot-seed999.jsonl"
# MUST carry the frozen pool shape. workload_gen's CLI defaults are the ORIGINAL
# 20-prefix s=1.2 profile, so omitting these silently pilots a workload the
# sweep will never run - and the knee is a property of the workload.
eval "$(python3 -c "
import sys; sys.path.insert(0, '$BENCH_DIR')
from freeze_workloads import frozen_config
c = frozen_config(seed=999)
print(f'POOL={c.prefix_pool_size}; ZIPF={c.zipf_s}; PTOK={c.prefix_tokens}; STOK={c.suffix_tokens}; PSEED={c.pool_seed}')
")"
echo "==> pilot workload: pool=$POOL zipf_s=$ZIPF prefix_tokens=$PTOK"
python3 "$BENCH_DIR/workload_gen.py" --num-requests 200 --seed 999 \
  --prefix-pool-size "$POOL" --zipf-s "$ZIPF" \
  --prefix-tokens "$PTOK" --suffix-tokens "$STOK" --pool-seed "$PSEED" \
  --out "$PILOT_WORKLOAD"

echo "==> warm-up over the frozen prefix pool"
python3 "$BENCH_DIR/warmup.py" --base-url "$BASE_URL" --model "$MODEL" --insecure --passes 1

for rate in "${RATES[@]}"; do
  echo
  echo "==> pilot at rate=$rate req/s"
  python3 "$BENCH_DIR/load_driver.py" \
    --base-url "$BASE_URL" --model "$MODEL" --insecure \
    --workload "$PILOT_WORKLOAD" --rate "$rate" \
    --out "$PILOT_DIR/pilot-rate$rate.csv"
done

echo
echo "Pick the rate where TTFT p95 elbows; freeze ~75% of it for run_sweep.sh."

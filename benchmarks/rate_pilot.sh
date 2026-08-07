#!/usr/bin/env bash
# Step 0 of the methodology (issue #3): find the TTFT-p95 knee on kvaware,
# then freeze the fixed open-loop rate for EVERY sweep cell.
#
# Deploys kvaware itself: the scarcity gate leaves roundrobin behind and the knee
# is arm-specific. The pilot workload is 200 requests on seed 999 - the same
# frozen pool, so it does not poison measurement prefixes.
#
# READ THE ACHIEVED req/s, NOT ONLY THE LATENCIES. The rate range must bracket
# the knee on BOTH sides or there is nothing to take a fraction of: a dead-flat
# TTFT p95 across the whole range means the pilot never reached the knee, not
# that there is no knee, and a rate frozen from inside the flat region leaves
# `vllm:num_requests_waiting` at zero on every engine - no load for a load-aware
# router to be aware of. Where this cluster's knee landed:
# benchmarks/README.md, "Picking the rate (step 0)".
#
# Pick the rate where achieved req/s stops tracking offered and TTFT p95 elbows.
# A load-balancing policy can only pay off at or above the knee; below it the
# sweep measures cache locality alone.
#
# Usage:  ./rate_pilot.sh [rate ...]        (default: 4 8 12 14 16 18 20)
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
if [ $# -gt 0 ]; then RATES=("$@"); else RATES=(4 8 12 14 16 18 20); fi

echo "==> deploying kvaware for the pilot"
helm upgrade --install "$RELEASE" "$CHART" -n "$NS" --version "$CHART_VERSION" \
  -f "$REPO_ROOT/deploy/values-baseline-kvaware.yaml"
oc set env "deploy/$ROUTER_DEPLOY" -n "$NS" HF_HOME=/tmp/hf LOADAWARE_BETA-
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
echo "==> saturation curve (offered vs achieved - the knee is where they diverge)"
python3 - "$PILOT_DIR" <<'PY'
import csv, glob, re, sys
def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, round(p / 100 * (len(xs) - 1))))] if xs else float("nan")
rows = []
for path in glob.glob(f"{sys.argv[1]}/pilot-rate*.csv"):
    offered = float(re.search(r"rate([\d.]+)\.csv", path).group(1))
    R = list(csv.DictReader(open(path)))
    ok = [r for r in R if r["status"] == "ok" and r["ttft_s"]]
    if not ok:
        continue
    sends = [float(r["send_ts"]) for r in R]
    ends = [float(r["send_ts"]) + float(r["e2e_s"]) for r in R if r["e2e_s"]]
    wall = max(ends) - min(sends)
    rows.append((offered, len(ok) / wall, pct([float(r["ttft_s"]) for r in ok], 95),
                 len(R) - len(ok)))
print(f"{'offered':>8} {'achieved':>9} {'ratio':>6} {'ttft_p95':>9} {'err':>4}")
for o, a, t, e in sorted(rows):
    print(f"{o:8.1f} {a:9.2f} {a / o:6.2f} {t:9.3f} {e:4d}")
print("\nKnee = the first rate where ratio drops well below ~0.95 and ttft_p95 lifts.")
print("Freeze AT or just under it. A rate deep in the flat region cannot show a")
print("load-balancing effect: with zero queueing there is no load to balance.")
PY

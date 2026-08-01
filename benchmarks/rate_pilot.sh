#!/usr/bin/env bash
# Step 0 of the methodology (issue #3): find the TTFT-p95 knee on kvaware,
# then freeze ~75% of it as the fixed open-loop rate for EVERY sweep cell.
#
# Run against a kvaware deployment that has been probed + warmed (run_cell.sh
# steps 1-6, or an existing healthy deployment). Ramps the rates below with a
# 200-request pilot workload (seed 999 - same frozen pool, so it does not
# poison measurement prefixes) and prints one summary per rate; pick the knee
# by eye and pass rate ≈ 0.75 × knee to run_sweep.sh.
#
# Usage:  ./rate_pilot.sh [rate ...]        (default: 1 2 4 6 8 12)
set -euo pipefail

BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_URL="${BASE_URL:-https://llm-cache-llm.apps.gapu-2.customers.k8s.co.il}"
MODEL="${MODEL:-Qwen/Qwen2.5-3B-Instruct}"
if [ $# -gt 0 ]; then RATES=("$@"); else RATES=(1 2 4 6 8 12); fi

PILOT_DIR="${PILOT_DIR:-results/rate-pilot}"
mkdir -p "$PILOT_DIR"
PILOT_WORKLOAD="$PILOT_DIR/pilot-seed999.jsonl"
python3 "$BENCH_DIR/workload_gen.py" --num-requests 200 --seed 999 --out "$PILOT_WORKLOAD"

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

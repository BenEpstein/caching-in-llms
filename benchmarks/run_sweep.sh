#!/usr/bin/env bash
# The full merged sweep (methodology, issue #3 as amended 2026-08-03): 6 cells,
# 32 seed-replays x 500 requests, one unattended batch (~1.7-2.3 h depending on
# the rate). Requires the fixed rate from rate_pilot.sh and, for the loadaware
# cells, LOADAWARE_TAG.
#
# Usage:  LOADAWARE_TAG=<sha> ./run_sweep.sh <rate> [results-root]
set -euo pipefail

RATE="${1:?usage: run_sweep.sh <rate> [results-root]}"
RESULTS_ROOT="${2:-results}"
BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Amended methodology (#3, 2026-08-03): seeds are spent where they carry
# inference. The headline pair gets 10 (n=6 cannot survive one reversal - the
# pilot hit exactly that); the beta-sweep and the context baseline get 3, since
# a tuning curve and a sanity comparator are not hypothesis tests.
CELL_SEEDS=(
  "loadaware-b0.1:1 2 3 4 5 6 7 8 9 10"
  "kvaware:1 2 3 4 5 6 7 8 9 10"
  "loadaware-b0:1 2 3"
  "loadaware-b0.5:1 2 3"
  "loadaware-b1.0:1 2 3"
  "roundrobin:1 2 3"
)
for entry in "${CELL_SEEDS[@]}"; do
  SEEDS="${entry#*:}" "$BENCH_DIR/run_cell.sh" "${entry%%:*}" "$RATE" "$RESULTS_ROOT"
done
echo "==> sweep complete under $RESULTS_ROOT"
echo "    headline: python3 benchmarks/analyze.py compare <loadaware-b0.1-dir> <kvaware-dir>"

#!/usr/bin/env bash
# The full merged sweep (methodology, issue #3): 6 cells × 6 seeds × 500
# requests, one unattended batch (~3 h). Requires the fixed rate from
# rate_pilot.sh and, for the loadaware cells, LOADAWARE_TAG.
#
# Usage:  LOADAWARE_TAG=<sha> ./run_sweep.sh <rate> [results-root]
set -euo pipefail

RATE="${1:?usage: run_sweep.sh <rate> [results-root]}"
RESULTS_ROOT="${2:-results}"
BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CELLS=(loadaware-b0 loadaware-b0.1 loadaware-b0.5 loadaware-b1.0 kvaware roundrobin)
for cell in "${CELLS[@]}"; do
  "$BENCH_DIR/run_cell.sh" "$cell" "$RATE" "$RESULTS_ROOT"
done
echo "==> sweep complete under $RESULTS_ROOT"
echo "    headline: python3 benchmarks/analyze.py compare <loadaware-b0.1-dir> <kvaware-dir>"

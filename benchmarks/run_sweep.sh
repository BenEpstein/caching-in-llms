#!/usr/bin/env bash
# The merged sweep (methodology, issue #3 as amended 2026-08-04): 5 cells,
# 30 seed-replays x 500 requests, one unattended batch (~55-65 min at a rate
# near the knee, down from 76). Requires the fixed rate from rate_pilot.sh and,
# for the loadaware cells, LOADAWARE_TAG.
#
# Usage:  LOADAWARE_TAG=<sha> ./run_sweep.sh <rate> [results-root]
set -euo pipefail

RATE="${1:?usage: run_sweep.sh <rate> [results-root]}"
RESULTS_ROOT="${2:-results}"
BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Where the wall-clock actually goes, measured on the 2026-08-03 sweep: ~8 min
# of fixed setup per CELL (helm upgrade, drain, cold engine restart with a model
# load, warm-up, Prometheus dump) against ~50 s per SEED replay. 6 cells x 8 min
# was 48 of the 76 minutes. So cells are the only lever worth pulling to shorten
# a run, and seeds are close to free - which is lucky, because the headline pair
# was underpowered at n=10 (p=0.0527) and cutting seeds would have guaranteed a
# null result while saving under a minute each.
#
#   dropped  loadaware-b1.0  - beta=1.0 is far past the optimum and b0.5 already
#                              establishes the direction and the mechanism
#                              (hit rate 0.918 -> 0.787 -> 0.735). One cell, ~10
#                              min, no inferential content. -> saves ~10 min
#   kept     loadaware-b0    - this is the ABLATION. beta=0 is the extension
#                              with its load term switched off, i.e. the only
#                              cell that isolates what beta buys. Dropping it
#                              leaves "is the load term doing anything?"
#                              unanswerable, and it currently posts the best
#                              p95 at n=3 - which is exactly why it needs more
#                              seeds, not fewer. Raised 3 -> 5.
#   roundrobin      3 -> 2    - a context baseline that is 18x worse on TTFT p95
#                              does not need a variance estimate to make its
#                              point. -> saves ~1 min
#
# The headline pair stays at 10. It is the one number in this file that must not
# shrink: at n=10 the sweep returned p=0.0527, so any cut guarantees a null
# result while saving under a minute per seed. If more power is wanted, raise
# SEEDS in freeze_workloads.py and re-freeze the manifest - that is a
# methodology change and belongs in a pre-registration, not here.
CELL_SEEDS=(
  "loadaware-b0.1:1 2 3 4 5 6 7 8 9 10"
  "kvaware:1 2 3 4 5 6 7 8 9 10"
  "loadaware-b0:1 2 3 4 5"
  "loadaware-b0.5:1 2 3"
  "roundrobin:1 2"
)
for entry in "${CELL_SEEDS[@]}"; do
  SEEDS="${entry#*:}" "$BENCH_DIR/run_cell.sh" "${entry%%:*}" "$RATE" "$RESULTS_ROOT"
done
echo "==> sweep complete under $RESULTS_ROOT"
echo "    headline: python3 benchmarks/analyze.py compare <loadaware-b0.1-dir> <kvaware-dir>"

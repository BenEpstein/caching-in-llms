#!/usr/bin/env bash
# The confirmatory sweep (methodology, issue #3 as amended 2026-08-04): 4 cells,
# 63 seed-replays x 500 requests, one unattended batch (~85 min at a rate at or
# above the knee). Requires the rate AND the headline beta from rate_pilot.sh,
# plus LOADAWARE_TAG for the loadaware cells.
#
# Usage:
#   LOADAWARE_TAG=<sha> BETA_HEADLINE=<b> BETA_HIGH=<2b> ./run_sweep.sh <rate> [results-root]
set -euo pipefail

RATE="${1:?usage: run_sweep.sh <rate> [results-root]}"
RESULTS_ROOT="${2:-results}"
BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- why this shape ---------------------------------------------------------
#
# Where the wall-clock actually goes, measured on the 2026-08-03 sweep: ~8 min
# of fixed setup per CELL (helm upgrade, drain, cold engine restart with a model
# load, warm-up, Prometheus dump) against ~50 s per SEED replay. So cells are the
# only lever worth pulling to shorten a run, and seeds are close to free.
#
# The 2026-08-03 amended sweep found no queueing at any scrape on any engine
# (`vllm:num_requests_waiting` max 0.00, both arms, both engines) because 10.5
# req/s was frozen as "75% of a knee" the pilot had never reached. beta had
# nothing to act on. This sweep runs at or above the knee, where it does.
#
#   n=20 on the three inferential cells - n=10 returned p=0.0527 on the headline,
#           which is underpowered, and ten more seeds cost ~8 min per cell.
#           Requires SEEDS=1..20 in freeze_workloads.py (done, purely additive).
#   kept    loadaware-b0 at n=20 - this is the ABLATION, the only cell that
#           isolates what beta buys. It must match the headline's n or the
#           b<headline>-vs-b0 paired test cannot be run at all.
#   kept    ONE high-beta cell at n=3 - establishes the direction of the
#           tradeoff at this rate. beta's meaning is tied to absolute concurrency
#           (benefit is in [0,1] but load is a raw in-flight count), so the
#           10.5 req/s curve does not transfer and the direction has to be
#           re-shown here. Three seeds is enough for a direction.
#   dropped roundrobin - already measured at 10.5 req/s (TTFT p95 5.502 s, 18x
#           worse, 25 preemptions, 0.709 hit rate). It is a context baseline, not
#           a hypothesis test, and at the knee it is expected to breach the 1%
#           error gate. Set INCLUDE_ROUNDROBIN=1 to add it back at n=2 (~9 min);
#           a breach is then REPORTED as a saturation finding, per the
#           pre-registered carve-out, never treated as a run failure.
#
: "${BETA_HEADLINE:?set BETA_HEADLINE - calibrated from the rate pilot (see #3), not guessed}"
: "${BETA_HIGH:?set BETA_HIGH - roughly 2x BETA_HEADLINE, for the tradeoff direction}"
INCLUDE_ROUNDROBIN="${INCLUDE_ROUNDROBIN:-0}"

SEEDS_FULL="1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20"

CELL_SEEDS=(
  "loadaware-b${BETA_HEADLINE}:${SEEDS_FULL}"
  "kvaware:${SEEDS_FULL}"
  "loadaware-b0:${SEEDS_FULL}"
  "loadaware-b${BETA_HIGH}:1 2 3"
)
[ "$INCLUDE_ROUNDROBIN" = "1" ] && CELL_SEEDS+=("roundrobin:1 2")

for entry in "${CELL_SEEDS[@]}"; do
  SEEDS="${entry#*:}" "$BENCH_DIR/run_cell.sh" "${entry%%:*}" "$RATE" "$RESULTS_ROOT"
done
echo "==> sweep complete under $RESULTS_ROOT"
echo "    headline:  python3 benchmarks/analyze.py compare <loadaware-b${BETA_HEADLINE}-dir> <kvaware-dir>"
echo "    ablation:  python3 benchmarks/analyze.py compare <loadaware-b${BETA_HEADLINE}-dir> <loadaware-b0-dir>"
echo "    placement: python3 benchmarks/analyze.py compare <loadaware-b0-dir> <kvaware-dir>"

#!/usr/bin/env bash
# The confirmatory sweep (methodology, issue #3 as amended 2026-08-04): 4 cells,
# 63 seed-replays x 500 requests, one unattended batch (~85 min at a rate at or
# above the knee). Requires the rate from rate_pilot.sh plus LOADAWARE_TAG for
# the loadaware cells. beta is no longer an input: it is dimensionless, so the
# grid is fixed (see below).
#
# Usage:
#   LOADAWARE_TAG=<sha> ./run_sweep.sh <rate> [results-root]
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
#   kept    TWO off-headline beta cells at n=3 - the tradeoff curve. Unlike the
#           pre-2026-08-04 formulation, beta is now dimensionless (the router
#           normalizes load against the live fleet mean), so a curve measured
#           here is a statement about the POLICY rather than about this rate,
#           and the grid does not have to be re-derived per operating point.
#           Three seeds is enough for a direction.
#   dropped roundrobin - already measured at 10.5 req/s (TTFT p95 5.502 s, 18x
#           worse, 25 preemptions, 0.709 hit rate). It is a context baseline, not
#           a hypothesis test, and at the knee it is expected to breach the 1%
#           error gate. Set INCLUDE_ROUNDROBIN=1 to add it back at n=2 (~9 min);
#           a breach is then REPORTED as a saturation finding, per the
#           pre-registered carve-out, never treated as a run failure.
#
# ---- the beta grid ----------------------------------------------------------
#
# FIXED, not calibrated. beta is a ratio of two dimensionless quantities - a
# fraction of this prompt against a fraction of this fleet's mean load - so the
# same value is the same policy on any rate, model or GPU count. The old
# `BETA_HEADLINE` input came out of `load_gate.beta_from()`, which solved for
# beta from ONE probe's absolute in-flight count; two probes at this very rate
# disagreed by 2.6x (0.013 vs 0.034), which is what retired it.
#
# The headline is the documented default: beta = 1.0, i.e. "an endpoint 100%
# above fleet-average load forfeits one full cache hit".
#
# The bracket comes from the 2026-08-04 evening sweep, converted into these
# units by beta_rel = beta_abs * live_fleet_mean. That sweep's TTFT optimum
# (beta_abs 0.034 at mean load 26.6-27.3) is beta_rel 0.90-0.93, and its ITL
# optimum (beta_abs 0.068 at mean load 19.65) is beta_rel 1.34 - so the whole
# measured tradeoff lives between 0.9 and 1.35, with the default between them.
# A decade-wide grid would put one point in that region and spend the rest of
# the cluster time confirming the collapse we already saw at 10.5 req/s. This
# also answers the "beta grid too coarse" gap on PR #22.
BETA_HEADLINE="${BETA_HEADLINE:-1.0}"
BETA_LOW="${BETA_LOW:-0.5}"
BETA_HIGH="${BETA_HIGH:-1.5}"
INCLUDE_ROUNDROBIN="${INCLUDE_ROUNDROBIN:-0}"

SEEDS_FULL="1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20"

CELL_SEEDS=(
  "loadaware-b${BETA_HEADLINE}:${SEEDS_FULL}"
  "kvaware:${SEEDS_FULL}"
  "loadaware-b0:${SEEDS_FULL}"
  "loadaware-b${BETA_LOW}:1 2 3"
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

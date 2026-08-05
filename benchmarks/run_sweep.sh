#!/usr/bin/env bash
# The confirmatory sweep (methodology, issue #3 as amended 2026-08-05): 5 cells,
# 100 seed-replays x 500 requests, one unattended batch (~1 h 41 min at rate 16).
# Requires the rate from rate_pilot.sh, BENCH_TAG for every cell, and
# LOADAWARE_TAG for the loadaware cells. beta is no longer an input: it is
# dimensionless, so the grid is fixed (see below).
#
# Usage:
#   LOADAWARE_TAG=<sha> BENCH_TAG=<sha> ./run_sweep.sh <rate> [results-root]
set -euo pipefail

RATE="${1:?usage: run_sweep.sh <rate> [results-root]}"
RESULTS_ROOT="${2:-results}"
BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Both tags are checked HERE, before the first helm upgrade, rather than being
# left to run_cell.sh's own guards. run_cell.sh would catch a missing BENCH_TAG
# in its first second, but only for the cell it is running: with `set -e` the
# sweep would then abort mid-batch, and on the loadaware arms that is ~8 min of
# setup into a cell that was never going to measure anything. The measured
# replay runs in-cluster from the bench image (#27), so BENCH_TAG is required on
# BOTH arms - the baseline needs it exactly as much as loadaware does.
: "${BENCH_TAG:?the sweep needs BENCH_TAG=<git short SHA of the CI-built bench image> - every cell replays from it, both arms}"
: "${LOADAWARE_TAG:?the sweep needs LOADAWARE_TAG=<git short SHA of the CI-built router image> - ${BETA_GRID:-0 0.5 1.0 2.0} are loadaware cells}"

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
#   n=20 EVERYWHERE - see the note above CELL_SEEDS. Short version: the exact
#           Wilcoxon cannot reach the 0.025 threshold below n=6 whatever the
#           effect size, and n=10 is the first that survives one reversal.
#   kept    loadaware-b0 at n=20 - the ABLATION, the only cell that isolates
#           what beta buys, and pre-declared falsifiable. It must match the
#           headline's n or the b<headline>-vs-b0 paired test cannot be run.
#   dropped roundrobin - a context baseline, not a hypothesis test, and at rate
#           16 it SATURATES (10.74 req/s achieved against 16 offered, 67%), so
#           it is not at the same operating point as the other arms. Already
#           measured; cite that cell rather than spending 20 min re-confirming
#           it. Report the throughput shortfall as the honest headline for that
#           arm, never the latency ratio.
#   dropped beta=0.25 - measured 2026-08-04 at imbalance 2.257 against a
#           same-hour kvaware control of 2.113, i.e. no effect. The useful
#           range starts at 0.5.
#
# OSL is 64 (run_cell.sh MAX_TOKENS). Piloted 2026-08-05: at OSL 128 the fleet
# saturates (65% of offered achieved) and at 256 it breaches the catastrophic
# error ceiling (11.4%, KV pegged at 1.000, 231 preemptions). Imbalance is also
# NON-monotonic in load - 2.99x at OSL 64, 3.98x at 128, 1.89x at 256 - because
# once both engines pin at capacity there is nowhere better to send anything.
# The window where load-aware routing can help closes at BOTH ends, and on this
# 2xA10 fleet rate 16 / OSL 64 sits inside it.
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
BETA_GRID="${BETA_GRID:-0.5 1.0 2.0 0}"
SEEDS_FULL="1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20"

# n=20 on EVERY cell, including the curve arms. The exact one-sided Wilcoxon's
# resolution is bounded by the pair count, not by the effect size: at n=3 the
# smallest attainable p is 0.125, at n=5 it is 0.031, so against the Bonferroni
# threshold of 0.025 those cannot reach significance however large the effect.
# n=10 is the first that survives a single reversal (p=0.0107) and the published
# imbalance headline was 19/20, i.e. one reversal. n=20 buys the margin for two
# or three. Seeds are also nearly free - 0.57 min each against 8.75 min of fixed
# setup per cell - so trimming them saves little and spends the whole reversal
# budget to do it.
#
# ORDER: kvaware first, then BETA_GRID in the order given. Position in the
# window is confounded with cell either way - 2026-08-04 showed the cluster can
# drift (client TTFT rose ~55% over an evening on identical config) - so the
# grid order is where that confound gets managed.
#
# The default above IS the pre-registered order (#31 amendment 1) - it is set as
# the default rather than passed by hand so the documented command cannot run
# the wrong sequence. It is deliberate on two counts:
#   - b0.5 runs second, adjacent to kvaware. Those two are the only cells
#     carrying a p-value, so the pair that matters spans the least wall-clock.
#   - b0 runs LAST. It is the cell expected to behave like kvaware, so placing
#     it at maximum separation makes it the drift sentinel: a b0 null across the
#     whole window is evidence the window held. In slot 2 it said nothing.
# A b0 that moves cannot separate drift from a real placement effect, which is
# the accepted cost, declared in the pre-registration rather than discovered.
#
# There is NO closing kvaware bracket. It was dropped (Ben, 2026-08-05): the
# bracket was written when drift meant client TTFT drifting over the WAN, and
# #27 removed that cause by moving the driver in-cluster. b0-last is what
# replaces it, at zero extra cluster time.
CELL_SEEDS=("kvaware:${SEEDS_FULL}")
for b in $BETA_GRID; do
  CELL_SEEDS+=("loadaware-b${b}:${SEEDS_FULL}")
done

for entry in "${CELL_SEEDS[@]}"; do
  SEEDS="${entry#*:}" "$BENCH_DIR/run_cell.sh" "${entry%%:*}" "$RATE" "$RESULTS_ROOT"
done
echo "==> sweep complete under $RESULTS_ROOT"
echo "    No closing kvaware cell - b0 ran last and is the drift sentinel (#31)."
echo "    beta* = 0.5, fixed in advance as the configuration of record:"
echo "      tested claim 1 (balance):  python3 benchmarks/analyze.py compare <loadaware-b0.5-dir> <kvaware-dir>"
echo "      tested claim 2 (TTFT p95): same pair, same command - both at alpha 0.025"
echo "      ablation:                  python3 benchmarks/analyze.py compare <loadaware-b0.5-dir> <loadaware-b0-dir>"
echo "      placement + drift:         python3 benchmarks/analyze.py compare <loadaware-b0-dir> <kvaware-dir>"
echo "    The last one does double duty: b0 vs kvaware is the ablation contrast AND,"
echo "    because b0 ran at maximum separation from kvaware, the drift check."

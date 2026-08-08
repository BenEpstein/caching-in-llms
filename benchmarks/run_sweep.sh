#!/usr/bin/env bash
# The confirmatory sweep (methodology, issue #3 as amended): 7 cells,
# 140 seed-replays x 500 requests, one unattended batch.
# Requires the rate from rate_pilot.sh, BENCH_TAG for every cell, and
# LOADAWARE_TAG for the loadaware cells. beta is dimensionless, so the grid is
# fixed rather than an input.
#
# Usage:
#   LOADAWARE_TAG=<sha> BENCH_TAG=<sha> ./run_sweep.sh <rate> [results-root]
set -euo pipefail

RATE="${1:?usage: run_sweep.sh <rate> [results-root]}"
RESULTS_ROOT="${2:-results}"

# One id for the whole batch, minted HERE so every cell agrees on it and no two runs collide.
# Left to run_cell.sh it would fall back to the results-root basename, which defaults to the
# shared `results` - one id for every sweep ever run, and a guard that passes everything.
export SWEEP_ID="${SWEEP_ID:-sweep-$(date +%Y%m%d-%H%M%S)}"
BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Both tags are checked HERE, before the first helm upgrade, rather than left to
# run_cell.sh's own guards: under `set -e` a missing tag aborts the batch minutes
# of setup into a cell that was never going to measure anything. BENCH_TAG is
# required on BOTH arms - the measured replay runs in-cluster from the bench
# image (#27), so the baseline needs it exactly as much as loadaware does.
: "${BENCH_TAG:?the sweep needs BENCH_TAG=<git short SHA of the CI-built bench image> - every cell replays from it, both arms}"
: "${LOADAWARE_TAG:?the sweep needs LOADAWARE_TAG=<git short SHA of the CI-built router image> - ${BETA_GRID:-0.5 1.0 2.0 0.25 0} are loadaware cells}"

# ---- why this shape ---------------------------------------------------------
#
# Fixed setup per CELL (helm upgrade, drain, cold engine restart with a model
# load, warm-up, Prometheus dump) dominates a per-SEED replay. Cells are the only
# lever worth pulling to shorten a run; seeds are close to free.
#
# The sweep must run at or above the knee rate_pilot.sh finds: below it no engine
# ever queues and beta has nothing to act on.
#
#   loadaware-b0  the ABLATION, and the only cell that isolates what beta buys.
#                 Must run at the headline's n or the b<headline>-vs-b0 paired
#                 test cannot be run.
#   roundrobin    DESCRIPTIVE. SATURATES at the sweep rate, so it is NOT at the
#                 same operating point as the other arms: report its throughput
#                 shortfall, NEVER its latency ratio.
#   beta=0.25     DESCRIPTIVE, the low end of the grid.
#
# The two descriptive cells carry no p-value, so the pre-registered alpha=0.025
# pair (b0.5 vs kvaware) is untouched and no multiplicity adjustment is owed.
# Set EXTRA_CELLS="" to drop roundrobin; see its definition below.
#
# OSL is 64 (run_cell.sh MAX_TOKENS) and must stay there: raise it and the fleet
# saturates, raise it further and it breaches the catastrophic error ceiling, and
# imbalance is NON-monotonic in load, so the window where load-aware routing can
# help closes at BOTH ends. Evidence: benchmarks/README.md, "Why the operating
# point is rate 16 / OSL 64".
#
# ---- the beta grid ----------------------------------------------------------
#
# FIXED, not calibrated. beta is a ratio of two dimensionless quantities - a
# fraction of this prompt against a fraction of this fleet's mean load - so the
# same value is the same policy on any rate, model or GPU count. Why it is no
# longer probe-calibrated: `load_gate.relative_imbalance()`.
#
# beta = 1.0 is the shipped default: "an endpoint 100% above fleet-average load
# forfeits one full cache hit".
#
# The grid brackets the measured tradeoff (#22), with one point either side of
# it; a decade-wide grid would spend most of the cluster time confirming a
# collapse already measured. See benchmarks/README.md, "Two eras of `beta`".
BETA_GRID="${BETA_GRID:-0.5 1.0 2.0 0.25 0}"
SEEDS_FULL="1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20"

# n=20 everywhere, curve arms included, pre-registered (#31). The exact one-sided
# Wilcoxon's resolution is bounded by the pair count, not by the effect size, so
# small n cannot reach the alpha=0.025 threshold however large the effect; n=20
# is what buys margin for a reversal or two. The derivation is in
# benchmarks/README.md, "Statistics (pre-registered)". Seeds are nearly free
# against the fixed per-cell setup above: do NOT trim them - it saves little and
# spends the whole reversal budget to do it.
#
# ORDER: kvaware first, then BETA_GRID in the order given, then roundrobin.
# Position in the window is confounded with cell either way - the cluster drifts
# over an evening on identical config - so the order is where that confound gets
# managed.
#
# The default extends #31's pre-registered order (kvaware, b0.5, b1.0, b2.0, b0)
# with two DESCRIPTIVE cells; #31 itself covers the five, not the seven. It is a
# default rather than a hand-passed argument so the documented command cannot run
# the wrong sequence. Two constraints fix it:
#   - b0.5 runs second, adjacent to kvaware. Those two are the only cells
#     carrying a p-value, so the pair that matters spans the least wall-clock.
#   - b0 runs LAST OF THE LOADAWARE CELLS, at maximum separation from kvaware,
#     which is what makes it the drift sentinel: a b0 null across the whole
#     window is evidence the window held. In slot 2 it says nothing. A b0 that
#     moves cannot separate drift from a real placement effect - the accepted
#     cost, declared in the pre-registration rather than discovered.
# roundrobin trails b0: a different arm on the stock image cannot be a sentinel
# for the loadaware cells, and inserting it earlier would push b0 off that slot.
#
# There is deliberately NO closing kvaware bracket - b0-last replaces it (#27
# removed the WAN drift it guarded) at zero extra cluster time.
# Non-beta cells appended after the grid. Overridable like BETA_GRID, and for the same reason:
# #31 pre-registered a FIVE-cell sweep with roundrobin explicitly out, so that run has to stay
# reproducible from this script:
#   BETA_GRID="0.5 1.0 2.0 0" EXTRA_CELLS="" ./run_sweep.sh 16
# Unset-vs-empty matters here, so no colon in the expansion: EXTRA_CELLS="" means none.
EXTRA_CELLS="${EXTRA_CELLS-roundrobin}"

CELLS="kvaware"
for b in $BETA_GRID; do
  CELLS+=" loadaware-b${b}"
done
[ -n "$EXTRA_CELLS" ] && CELLS+=" $EXTRA_CELLS"

for cell in $CELLS; do
  SEEDS="$SEEDS_FULL" "$BENCH_DIR/run_cell.sh" "$cell" "$RATE" "$RESULTS_ROOT"
done
echo "==> sweep complete under $RESULTS_ROOT"
echo "    No closing kvaware cell - b0 ran last of the loadaware cells and is the drift sentinel (#31)."
echo "    beta* = 0.5, fixed in advance as the configuration of record:"
echo "      tested claim 1 (balance):  python3 benchmarks/analyze.py compare --metric imbalance <loadaware-b0.5-dir> <kvaware-dir>"
echo "      tested claim 2 (TTFT p95): same pair, WITHOUT --metric (ttft_p95 is the default) - both at alpha 0.025"
echo "      ablation:                  same two commands on <loadaware-b0.5-dir> <loadaware-b0-dir>"
echo "      placement + drift:         same two commands on <loadaware-b0-dir> <kvaware-dir>"
echo "    The last one does double duty: b0 vs kvaware is the ablation contrast AND,"
echo "    because b0 ran at maximum separation from kvaware, the drift check."
echo "    secondary metric - the --slo objective is tunable, report it with the sweep (fig12):"
echo "      goodput: python3 benchmarks/analyze.py compare --metric ttft_slo_miss [--slo 0.15] <cand-dir> <base-dir>"
echo "    descriptive cells, NO p-value - the alpha=0.025 pair above is unaffected:"
echo "      b0.25      the low end of the grid at n=20"
echo "      roundrobin SATURATES at this rate: pass it to plot_results.py as --comparator,"
echo "                 and report its throughput shortfall, NEVER its latency ratio."

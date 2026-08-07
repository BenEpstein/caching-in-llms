#!/usr/bin/env bash
# The confirmatory sweep (methodology, issue #3 as amended 2026-08-05): 5 cells,
# 100 seed-replays x 500 requests, one unattended batch (~1 h 41 min at rate 16).
# Requires the rate from rate_pilot.sh, BENCH_TAG for every cell, and
# LOADAWARE_TAG for the loadaware cells. beta is dimensionless, so the grid is
# fixed rather than an input.
#
# Usage:
#   LOADAWARE_TAG=<sha> BENCH_TAG=<sha> ./run_sweep.sh <rate> [results-root]
set -euo pipefail

RATE="${1:?usage: run_sweep.sh <rate> [results-root]}"
RESULTS_ROOT="${2:-results}"
BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Both tags are checked HERE, before the first helm upgrade, rather than left to
# run_cell.sh's own guards: under `set -e` a missing tag aborts the batch ~8 min
# of setup into a cell that was never going to measure anything. BENCH_TAG is
# required on BOTH arms - the measured replay runs in-cluster from the bench
# image (#27), so the baseline needs it exactly as much as loadaware does.
: "${BENCH_TAG:?the sweep needs BENCH_TAG=<git short SHA of the CI-built bench image> - every cell replays from it, both arms}"
: "${LOADAWARE_TAG:?the sweep needs LOADAWARE_TAG=<git short SHA of the CI-built router image> - ${BETA_GRID:-0.5 1.0 2.0 0} are loadaware cells}"

# ---- why this shape ---------------------------------------------------------
#
# ~8 min of fixed setup per CELL (helm upgrade, drain, cold engine restart with a
# model load, warm-up, Prometheus dump) against ~50 s per SEED replay. Cells are
# the only lever worth pulling to shorten a run; seeds are close to free.
#
# The sweep must run at or above the knee rate_pilot.sh finds: below it no engine
# ever queues and beta has nothing to act on.
#
#   kept    loadaware-b0 at n=20 - the ABLATION, the only cell that isolates
#           what beta buys, and pre-declared falsifiable. It must match the
#           headline's n or the b<headline>-vs-b0 paired test cannot be run.
#   dropped roundrobin - a context baseline, not a hypothesis test, and at rate
#           16 it SATURATES (10.74 req/s achieved against 16 offered), so it is
#           not at the same operating point as the other arms. Cite the existing
#           cell, and report its throughput shortfall as the honest headline for
#           that arm, never the latency ratio.
#   dropped beta=0.25 - no effect against a same-hour kvaware control. The
#           useful range starts at 0.5.
#
# OSL is 64 (run_cell.sh MAX_TOKENS) and must stay there: at 128 the fleet
# saturates, at 256 it breaches the catastrophic error ceiling, and imbalance is
# NON-monotonic in load, so the window where load-aware routing can help closes
# at BOTH ends. Numbers in benchmarks/README.md, "Sweep design".
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
# The grid brackets the measured tradeoff: in these units the TTFT optimum
# measured 0.90-0.93 and the ITL optimum 1.34, so the whole tradeoff lives
# between 0.9 and 1.35, with 0.5 and 2.0 outside it on either side. A
# decade-wide grid would put one point in that region and spend the rest of the
# cluster time confirming a collapse already measured. This also answers the
# "beta grid too coarse" gap on PR #22.
BETA_GRID="${BETA_GRID:-0.5 1.0 2.0 0}"
SEEDS_FULL="1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20"

# n=20 on EVERY cell, including the curve arms. The exact one-sided Wilcoxon's
# resolution is bounded by the pair count, not by the effect size: at n=3 the
# smallest attainable p is 0.125, at n=5 it is 0.031, so against the Bonferroni
# threshold of 0.025 those cannot reach significance however large the effect.
# n=10 is the first that survives a single reversal (p=0.0107) and the published
# imbalance headline was 19/20, i.e. one reversal; n=20 buys the margin for two
# or three. Seeds are nearly free against the fixed per-cell setup above, so
# trimming them saves little and spends the whole reversal budget to do it.
#
# ORDER: kvaware first, then BETA_GRID in the order given. Position in the
# window is confounded with cell either way - the cluster can drift (client TTFT
# has risen ~55% over an evening on identical config) - so the grid order is
# where that confound gets managed.
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
# There is deliberately NO closing kvaware bracket: b0-last replaces it at zero
# extra cluster time.
CELLS="kvaware"
for b in $BETA_GRID; do
  CELLS+=" loadaware-b${b}"
done

for cell in $CELLS; do
  SEEDS="$SEEDS_FULL" "$BENCH_DIR/run_cell.sh" "$cell" "$RATE" "$RESULTS_ROOT"
done
echo "==> sweep complete under $RESULTS_ROOT"
echo "    No closing kvaware cell - b0 ran last and is the drift sentinel (#31)."
echo "    beta* = 0.5, fixed in advance as the configuration of record:"
echo "      tested claim 1 (balance):  python3 benchmarks/analyze.py compare --metric imbalance <loadaware-b0.5-dir> <kvaware-dir>"
echo "      tested claim 2 (TTFT p95): same pair, WITHOUT --metric (ttft_p95 is the default) - both at alpha 0.025"
echo "      ablation:                  same two commands on <loadaware-b0.5-dir> <loadaware-b0-dir>"
echo "      placement + drift:         same two commands on <loadaware-b0-dir> <kvaware-dir>"
echo "    The last one does double duty: b0 vs kvaware is the ablation contrast AND,"
echo "    because b0 ran at maximum separation from kvaware, the drift check."
echo "    secondary metric - the --slo objective is tunable, report it with the sweep (fig12):"
echo "      goodput: python3 benchmarks/analyze.py compare --metric ttft_slo_miss [--slo 0.15] <cand-dir> <base-dir>"

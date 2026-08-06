#!/usr/bin/env bash
# Regenerate every reported number from committed data, and fail on drift (#28).
#
# The claim this exists to make checkable, for §6:
#
#     "No number appears in this report that scripts/reproduce.sh cannot regenerate
#      from committed data."
#
# A grader verifies the whole Results section with one command and no hardware. Everything
# here reads only what is in the repository - no cluster, no GPU, no network.
#
#   ./scripts/reproduce.sh          # verify: regenerate and diff, non-zero on any drift
#   ./scripts/reproduce.sh --update # accept current output as the new expected baseline
#
# Figures are deliberately NOT byte-compared. Matplotlib output moves with font
# availability, version and embedded metadata, so a PNG diff would go red on a fresh runner
# for reasons that have nothing to do with our data - and a check that cries wolf gets muted,
# which is worse than no check. `plot_results.py --dump-data` writes the series behind every
# figure as JSON and that is what gets diffed: the numbers, not the pixels.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BENCH="$ROOT/benchmarks"
EXPECTED="$ROOT/results/expected"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

UPDATE=0
[ "${1:-}" = "--update" ] && UPDATE=1

# Accumulate failures rather than exiting on the first: a check script that stops at problem
# one hides problems two through five, and the person running it then fixes in serial.
FAILURES=()
fail() { echo "  FAIL $*" >&2; FAILURES+=("$*"); }
die()  { echo "FATAL: $*" >&2; exit 2; }
ok()   { echo "  ok   $*"; }

# Two kinds of comparison, and conflating them is dangerous:
#
#   verify_against  - the target is COMMITTED DATA (results/summary-per-seed.csv). Never
#                     written to. --update must not touch it: the generated file can be a
#                     strict subset when a run directory is missing, so "updating" it would
#                     silently delete real rows. That is not a hypothetical - the first run
#                     of this script found 5 such runs.
#   check           - the target is a derived BASELINE under results/expected/, which exists
#                     only to be compared against and is safe to refresh.
verify_against() {  # verify_against <generated> <committed-data> <label>
  _diff_or_fail "$1" "$2" "$3"
}

check() {  # check <generated> <expected-baseline> <label>
  local gen="$1" exp="$2" label="$3"
  if [ "$UPDATE" = "1" ]; then
    mkdir -p "$(dirname "$exp")"; cp "$gen" "$exp"; echo "  updated $label"; return
  fi
  if [ ! -f "$exp" ]; then
    fail "$label: no committed baseline at ${exp#$ROOT/} - run with --update once and commit it"
    return
  fi
  _diff_or_fail "$gen" "$exp" "$label"
}

_diff_or_fail() {  # _diff_or_fail <generated> <reference> <label>
  if diff -u "$2" "$1" >"$WORK/d.txt" 2>&1; then
    ok "$3"
  else
    echo "--- drift in $3 ---" >&2; head -30 "$WORK/d.txt" >&2
    fail "$3 no longer regenerates from committed data"
  fi
}

echo "==> 1/5 every run referenced by the summary has committed raw data"
# This is the check that catches the worst failure mode: a reported number whose evidence is
# not in the repository. It cannot be caught by regenerating, because a missing directory
# simply contributes no rows - the output shrinks and still looks well-formed.
missing=0
while read -r run; do
  [ -d "$ROOT/results/$run" ] || { echo "  MISSING results/$run" >&2; missing=$((missing+1)); }
done < <(awk -F, 'NR>1 {print $1}' "$ROOT/results/summary-per-seed.csv" | sort -u)
if [ "$missing" -eq 0 ]; then
  ok "all runs have raw data"
else
  fail "$missing run(s) in summary-per-seed.csv have no committed directory"
fi

echo "==> 2/5 frozen workloads are reconstructible (both profiles)"
# Same shape as the in-cluster path: copy the committed manifest into a writable directory
# and regenerate beside it, so this exercises the contract verify_dataset.sh depends on.
mkdir -p "$WORK/wl"; cp "$BENCH/workloads/manifest.json" "$WORK/wl/"
if python3 "$BENCH/freeze_workloads.py" --out-dir "$WORK/wl" >/dev/null; then
  ok "zipfian profile matches its manifest"
else
  fail "zipfian frozen workload does not reconstruct from its manifest"
fi
mkdir -p "$WORK/wln"; cp "$BENCH/workloads/novel/manifest.json" "$WORK/wln/"
if python3 "$BENCH/freeze_workloads.py" --profile novel --out-dir "$WORK/wln" >/dev/null; then
  ok "novel profile matches its manifest"
else
  fail "novel frozen workload does not reconstruct from its manifest"
fi

echo "==> 3/5 summary-per-seed.csv regenerates"
# No `mapfile`: macOS ships bash 3.2, and this repo has already been bitten once by a
# bash-4 builtin (`declare -A` in apply-router-patch.sh). Portable read loop instead.
RUNS=()
while read -r run; do
  [ -d "$ROOT/results/$run" ] && RUNS+=("$ROOT/results/$run")
done < <(awk -F, 'NR>1 {print $1}' "$ROOT/results/summary-per-seed.csv" | sort -u)
python3 "$BENCH/export_summary.py" "${RUNS[@]}" --out "$WORK/summary.csv" >/dev/null
verify_against "$WORK/summary.csv" "$ROOT/results/summary-per-seed.csv" "summary-per-seed.csv"

echo "==> 4/5 the reported statistics regenerate"
# The CONFIRMATORY sweep (#31, 2026-08-05 23:05 -> 2026-08-06 00:47), which is the run §5
# and §6 report. These defaults used to name the 2026-08-05 early-hours sweep while
# docs/figures had already been regenerated from the confirmatory one (a37cae5), so this
# script regenerated a superseded experiment and still passed: it diffed old-against-old and
# never touched the figures actually committed. A check that verifies the wrong run reads
# exactly like a check that passes.
: "${HEADLINE:=results/20260805-232541-loadaware-b0.5}"
: "${BASELINE:=results/20260805-230541-kvaware}"
: "${ABLATION:=results/20260806-002645-loadaware-b0}"
: "${BETA1:=results/20260805-234559-loadaware-b1.0}"
: "${BETA2:=results/20260806-000626-loadaware-b2.0}"
: "${COMPARATOR:=results/20260806-144135-roundrobin}"   # framing cell: fig12 ONLY, never a positional run
: "${SLO:=0.150}"   # analyze.TTFT_SLO_S; overridable like the cell paths above
{
  echo "# headline: $(basename "$HEADLINE") vs $(basename "$BASELINE")"
  python3 "$BENCH/analyze.py" compare "$ROOT/$HEADLINE" "$ROOT/$BASELINE" | grep -E "Wilcoxon|median relative"
  echo "# co-primary (balance): same pair, --metric imbalance"
  python3 "$BENCH/analyze.py" compare "$ROOT/$HEADLINE" "$ROOT/$BASELINE" --metric imbalance | grep -E "Wilcoxon|median relative"
  echo "# ablation: $(basename "$ABLATION") vs $(basename "$BASELINE")"
  python3 "$BENCH/analyze.py" compare "$ROOT/$ABLATION" "$ROOT/$BASELINE" | grep -E "Wilcoxon|median relative"
  # The "SLO <n> ms" line is in the grep alternation on purpose. analyze.py prints the
  # objective beside the p-value precisely so it cannot be separated from the number; a
  # grep that kept only the Wilcoxon line would strip it back off and leave two bare
  # verdicts in a committed baseline file with no record of which objective produced them.
  echo "# goodput: --metric ttft_slo_miss (secondary; objective swept in fig12)"
  python3 "$BENCH/analyze.py" compare "$ROOT/$HEADLINE" "$ROOT/$BASELINE" --metric ttft_slo_miss --slo "$SLO" | grep -E "SLO [0-9]+ ms|Wilcoxon|median relative"
  python3 "$BENCH/analyze.py" compare "$ROOT/$ABLATION" "$ROOT/$BASELINE" --metric ttft_slo_miss --slo "$SLO" | grep -E "SLO [0-9]+ ms|Wilcoxon|median relative"
} > "$WORK/stats.txt"
check "$WORK/stats.txt" "$EXPECTED/stats.txt" "reported statistics"

echo "==> 5/5 the numbers behind every figure regenerate"
python3 "$BENCH/plot_results.py" "$ROOT/$BASELINE" "$ROOT/$ABLATION" "$ROOT/$HEADLINE" \
  "$ROOT/$BETA1" "$ROOT/$BETA2" --comparator "$ROOT/$COMPARATOR" \
  --cand "loadaware-b0.5" --out "$WORK/figs" --dump-data "$WORK/figdata.json" >/dev/null
check "$WORK/figdata.json" "$EXPECTED/figure-data.json" "figure data"
nfigs=$(ls "$WORK/figs" | wc -l | tr -d ' ')
if [ "$nfigs" -ge 12 ]; then ok "$nfigs figures rendered"
else fail "expected at least 12 figures, got $nfigs"; fi

echo
if [ "${#FAILURES[@]}" -eq 0 ]; then
  echo "reproduce.sh: every reported number regenerated from committed data"
else
  echo "reproduce.sh: ${#FAILURES[@]} check(s) failed:" >&2
  printf '  - %s\n' "${FAILURES[@]}" >&2
  exit 1
fi

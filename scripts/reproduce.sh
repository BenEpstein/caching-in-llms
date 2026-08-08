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
# Figures are deliberately NOT byte-compared: `plot_results.py --dump-data` writes the series
# behind every figure as JSON and that is what gets diffed - the numbers, not the pixels.
# Rationale at `plot_results.dump_data`.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BENCH="$ROOT/benchmarks"
EXPECTED="$ROOT/results/expected"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

UPDATE=0
[ "${1:-}" = "--update" ] && UPDATE=1

# Where the regenerated figures land, ONE DIRECTORY PER SWEEP, named for the sweep_id in each
# cell's run.json so an artifact cannot be mistaken for another batch's. Defaults to the temp
# dir, so a local verify run still throws them away - this script's job is to prove the figures
# regenerate, not to produce them, and writing into docs/ by default would silently overwrite a
# committed set. CI points it at a real path and uploads the result.
FIGS="${FIGS_OUT:-$WORK/figs}"
mkdir -p "$FIGS"

# Accumulate failures rather than exiting on the first: stopping at problem one hides
# problems two through five, and they then get fixed in serial.
FAILURES=()
fail() { echo "  FAIL $*" >&2; FAILURES+=("$*"); }
die()  { echo "FATAL: $*" >&2; exit 2; }
ok()   { echo "  ok   $*"; }

# Two kinds of comparison, and conflating them is dangerous:
#
#   verify_against  - the target is COMMITTED DATA (a sweep's summary-per-seed.csv). Never
#                     written to. --update must not touch it: the generated file can be a
#                     strict subset when a run directory is missing, so "updating" it would
#                     silently delete real rows.
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

# The repo documents Python >= 3.10 (README "Setup", requirements.txt). Below that floor the
# regenerated numbers can differ in the last digit, and this script reports that as "figure data
# no longer regenerates from committed data" - a data-integrity failure that is really an
# environment mismatch, on the one check carrying the reproducibility claim. Observed on macOS
# system Python 3.9.6: itl_mean 0.036171 against the committed 0.03617. Fail early and say why.
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || die "needs Python >= 3.10, found $(python3 -V 2>&1). See README, \"Setup\"."

# Every summary-per-seed.csv in the tree: one per sweep directory (#75), none at the root.
# Column 1 of a table is the manifest of which runs belong to its sweep, so checks 1 and 3
# both walk it - resolved relative to the TABLE'S OWN directory, so a sweep's rows name
# paths inside that sweep.
#
# No `mapfile` or any other bash-4 builtin: macOS ships bash 3.2. Portable read loop instead.
summaries=()
while read -r extra; do summaries+=("$extra"); done < <(
  find "$ROOT/results" -mindepth 2 -name summary-per-seed.csv | sort)
[ "${#summaries[@]}" -gt 0 ] || die "no summary-per-seed.csv found under results/*/"

table_label() {  # table_label <table-path>
  echo "summary-per-seed.csv ($(basename "$(dirname "$1")"))"
}

echo "==> 1/7 every run referenced by a summary has committed raw data"
# Catches the worst failure mode: a reported number whose evidence is not in the repository.
# Regenerating cannot catch it - a missing directory contributes no rows, so the output
# shrinks and still looks well-formed.
missing=0
for table in "${summaries[@]}"; do
  base="$(dirname "$table")"
  while read -r run; do
    [ -d "$base/$run" ] || { echo "  MISSING ${base#$ROOT/}/$run" >&2; missing=$((missing+1)); }
  done < <(awk -F, 'NR>1 {print $1}' "$table" | sort -u)
done
if [ "$missing" -eq 0 ]; then
  ok "all runs in ${#summaries[@]} table(s) have raw data"
else
  fail "$missing run(s) referenced by a summary have no committed directory"
fi

echo "==> 2/7 frozen workloads are reconstructible (both profiles)"
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

echo "==> 3/7 every summary-per-seed.csv regenerates"
for table in "${summaries[@]}"; do
  base="$(dirname "$table")"
  label="$(table_label "$table")"
  RUNS=()
  while read -r run; do
    [ -d "$base/$run" ] && RUNS+=("$base/$run")
  done < <(awk -F, 'NR>1 {print $1}' "$table" | sort -u)
  if [ "${#RUNS[@]}" -eq 0 ]; then
    fail "$label: no run directories resolved from column 1"
    continue
  fi
  python3 "$BENCH/export_summary.py" "${RUNS[@]}" --out "$WORK/$(basename "$base")-summary.csv" >/dev/null
  verify_against "$WORK/$(basename "$base")-summary.csv" "$table" "$label"
done

echo "==> 4/7 the reported statistics regenerate"
# The CONFIRMATORY sweep (#31, 2026-08-05 23:05 -> 2026-08-06 00:47), which is the run §5
# and §6 report. These must name whatever sweep `docs/figures/` was last generated from: a
# check that verifies the wrong run reads exactly like a check that passes.
# See benchmarks/README.md, "Reproducing the reported numbers".
: "${HEADLINE:=results/gen2-confirmatory/20260805-232541-loadaware-b0.5}"
: "${BASELINE:=results/gen2-confirmatory/20260805-230541-kvaware}"
: "${ABLATION:=results/gen2-confirmatory/20260806-002645-loadaware-b0}"
: "${BETA1:=results/gen2-confirmatory/20260805-234559-loadaware-b1.0}"
: "${BETA2:=results/gen2-confirmatory/20260806-000626-loadaware-b2.0}"
: "${COMPARATOR:=results/gen2-confirmatory/20260806-144135-roundrobin}"   # framing cell: fig12 ONLY, never a positional run
: "${SLO:=0.150}"   # analyze.TTFT_SLO_S; overridable like the cell paths above
{
  echo "# headline: $(basename "$HEADLINE") vs $(basename "$BASELINE")"
  python3 "$BENCH/analyze.py" compare "$ROOT/$HEADLINE" "$ROOT/$BASELINE" | grep -E "Wilcoxon|median relative"
  echo "# co-primary (balance): same pair, --metric imbalance"
  python3 "$BENCH/analyze.py" compare "$ROOT/$HEADLINE" "$ROOT/$BASELINE" --metric imbalance | grep -E "Wilcoxon|median relative"
  echo "# ablation: $(basename "$ABLATION") vs $(basename "$BASELINE")"
  python3 "$BENCH/analyze.py" compare "$ROOT/$ABLATION" "$ROOT/$BASELINE" | grep -E "Wilcoxon|median relative"
  # The "SLO <n> ms" line is in the grep alternation on purpose: analyze.py prints the
  # objective beside the p-value so the two cannot be separated, and a grep that kept only
  # the Wilcoxon line would leave two bare verdicts in a committed baseline file with no
  # record of which objective produced them.
  echo "# goodput: --metric ttft_slo_miss (secondary; objective swept in fig12)"
  python3 "$BENCH/analyze.py" compare "$ROOT/$HEADLINE" "$ROOT/$BASELINE" --metric ttft_slo_miss --slo "$SLO" | grep -E "SLO [0-9]+ ms|Wilcoxon|median relative"
  python3 "$BENCH/analyze.py" compare "$ROOT/$ABLATION" "$ROOT/$BASELINE" --metric ttft_slo_miss --slo "$SLO" | grep -E "SLO [0-9]+ ms|Wilcoxon|median relative"
} > "$WORK/stats.txt"
check "$WORK/stats.txt" "$EXPECTED/stats.txt" "reported statistics"

# Regenerate one sweep's figure set and diff the series behind it. Data that no check
# regenerates is data that rots silently, so every committed generation gets one of these -
# including generations that back no report section yet.
#
#   figure_set <sweep> <expected-json> <label> <plot args...>
#
# `--comparator` belongs in the caller's args, not here: only sweeps that HAVE a roundrobin
# cell pass one, and it is a framing cell for fig12 that must never become a positional run
# (its 12 s p95 flattens fig1's whole beta curve).
FIG_MIN=12
figure_set() {
  local sweep="$1" expected="$2" label="$3"; shift 3
  python3 "$BENCH/plot_results.py" "$@" --cand "loadaware-b0.5" \
    --out "$FIGS/$sweep" --dump-data "$WORK/figdata-$sweep.json" >/dev/null
  check "$WORK/figdata-$sweep.json" "$expected" "$label figure data"
  local n; n=$(ls "$FIGS/$sweep" | wc -l | tr -d ' ')
  if [ "$n" -ge "$FIG_MIN" ]; then ok "$n $label figures rendered"
  else fail "$label: expected at least $FIG_MIN figures, got $n"; fi
}

echo "==> 5/7 the numbers behind every figure regenerate"
figure_set gen2-confirmatory "$EXPECTED/figure-data.json" "reported" \
  "$ROOT/$BASELINE" "$ROOT/$ABLATION" "$ROOT/$HEADLINE" "$ROOT/$BETA1" "$ROOT/$BETA2" \
  --comparator "$ROOT/$COMPARATOR"

echo "==> 6/7 the WAN generation regenerates its own figure set"
# Literal paths, unlike the cells above: this generation is frozen, so there is nothing to
# repoint. No --comparator - the WAN sweep has no roundrobin cell and fig12 omits that curve.
W="$ROOT/results/gen1-wan"
figure_set gen1-wan "$EXPECTED/figure-data-wan.json" "WAN" \
  "$W/20260805-005210-kvaware" "$W/20260805-011148-loadaware-b0" \
  "$W/20260805-013208-loadaware-b0.5" "$W/20260805-015202-loadaware-b1.0" \
  "$W/20260805-021215-loadaware-b2.0"

echo "==> 7/7 the gen-3 7-cell sweep regenerates its own figure set"
G3="$ROOT/results/gen3-7cell"
figure_set gen3-7cell "$EXPECTED/figure-data-gen3.json" "gen-3" \
  "$G3/20260808-023919-kvaware" "$G3/20260808-042133-loadaware-b0" \
  "$G3/20260808-040053-loadaware-b0.25" "$G3/20260808-025932-loadaware-b0.5" \
  "$G3/20260808-031955-loadaware-b1.0" "$G3/20260808-034018-loadaware-b2.0" \
  --comparator "$G3/20260808-044202-roundrobin"

echo
if [ "${#FAILURES[@]}" -eq 0 ]; then
  echo "reproduce.sh: every reported number regenerated from committed data"
else
  echo "reproduce.sh: ${#FAILURES[@]} check(s) failed:" >&2
  printf '  - %s\n' "${FAILURES[@]}" >&2
  exit 1
fi

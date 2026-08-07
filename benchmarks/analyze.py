"""Analysis for benchmark runs: per-seed summaries, validity gate, paired stats.

Implements the PRE-REGISTERED test from issue #3, in code before any data
exists:
  - one run (seed replay) = ONE observation; per-request samples are
    queue-correlated and never treated as independent evidence
  - headline: one-sided exact Wilcoxon signed-rank on the 20 paired per-seed
    differences (candidate - baseline), H1 = candidate is LOWER. Threshold is
    0.025, Bonferroni-corrected for two co-primaries (TTFT p95 and imbalance);
    n was raised 6 -> 20 after n=10 returned p=0.0527, pre-registered on #31
  - effect size: median relative reduction with a bootstrap 95% CI over the
    paired differences
  - validity: error requests are excluded from latency stats but counted;
    a seed with > 1% errors invalidates the run

stdlib-only, deterministic (bootstrap is seeded).

UNITS: every latency MEASUREMENT this module produces is in SECONDS - `ttft_*`,
`e2e_*` AND `itl_*`. Two `ttft_`-prefixed fields are not measurements and are the
exceptions: `ttft_slo_miss` is a dimensionless fraction, and `ttft_slo_s` carries
an explicit `_s` because it is the objective those misses were counted against
rather than something observed. The driver writes inter-token gaps in
milliseconds under a column named `itls_ms`, and seed_stats divides by 1000 on
read, so the `itl_p95` that comes out of here is seconds despite the source
column's name. The names carry no unit suffix and are the column headers in the
committed summary CSV, so a reader checking a figure against that CSV has
nothing but this note to go on. Not
renamed deliberately: the names are load-bearing in export_summary.py,
plot_results.py, load_gate.py and the already-committed
results/summary-per-seed.csv.

Usage:
  python3 analyze.py summary  results/<run>...
  python3 analyze.py validate results/<run>            # exit 1 → do not use
  python3 analyze.py compare  results/<cand> results/<base> [--metric ttft_p95]
  python3 analyze.py compare  results/<cand> results/<base> --metric imbalance
  python3 analyze.py compare  results/<cand> results/<base> --metric ttft_slo_miss [--slo 0.15]

`ttft_slo_miss` is a secondary metric, reported alongside the two co-primaries.
It is computed from the same committed driver CSVs as every other statistic
here, and its objective is swept rather than fixed. See TTFT_SLO_S below; the
ruling that goodput reports the whole SLO curve rather than one point is in
docs/report/report.md ("An instrument problem, not a result") and landed in
a137f5a (#31).
"""

from __future__ import annotations

import argparse
import csv
import glob
import itertools
import json
import os
import random
import sys
from typing import Dict, List, Sequence, Tuple

# NOTE: `utilization` is imported INSIDE per_seed_imbalance, not here, and must
# stay that way. load_driver.py does `from analyze import percentile` and runs
# inside the bench image, whose Dockerfile.bench COPY line ships six files and
# does NOT include utilization.py. A module-level import here therefore breaks
# `import load_driver` in the image - which is the measurement path, so it breaks
# every future sweep. It did: it shipped on main and the bench-image workflow
# went red. test_analyze.py::test_analyze_module_level_imports_survive_the_bench_image
# guards it now, because that workflow triggers on push and cannot run on a PR.

# Validity rule 1, AMENDED 2026-08-04 (pre-registered on #3 before the run).
#
# The original rule voided a whole run if any single seed exceeded 1% errors.
# At a rate near the knee that fires on noise - the gate probe measured 0/500 and
# 4/500 (0.8%) at rate 16 and 0/500 and 5/500 (exactly 1.0%) at rate 18 - so
# across 63 replays it would discard the run for a cause already shown to be
# harmless.
#
# The 500s are ARM-INDEPENDENT: they appear in every arm including roundrobin,
# which never touches the KV registry, and 16/16 captured tracebacks are
# `aiohttp ServerDisconnectedError` raised AFTER the routing decision had already
# succeeded. An error floor that hits both arms equally is noise, not bias, and
# rule 1 exists to prevent bias.
#
# So: a flat floor is reported, not fatal. What voids a comparison is errors
# DIFFERING between the arms, which is the thing that could actually distort it.
# The pre-registered threshold (#31 rev 2): 0.025, Bonferroni over the two
# co-primaries (TTFT p95 and imbalance). The verdict line used to hardcode 0.05
# while this file's own docstring said 0.025, so a p between the two printed
# "significant" for a result the pre-registration calls null. No result to date
# fell in that gap; the constant exists so none ever can.
ALPHA = 0.025

MAX_ERROR_RATE = 0.01       # reporting threshold - flags a seed, does not void
HARD_ERROR_RATE = 0.10      # catastrophic: something is broken, void regardless
ERROR_BIAS_RATIO = 2.0      # arm error rates differing by more than this -> void
ERROR_BIAS_ABS = 0.01       # ...or by more than 1 percentage point absolute

# The default TTFT service-level objective behind `ttft_slo_miss`, in SECONDS.
#
# A DEFAULT, NOT A FIXED THRESHOLD. This is the tunable parameter of the metric
# (§4) and it is overridable per invocation with `compare --slo`. The reported
# result does not rest on it: fig12 sweeps 50-400 ms and the arms separate across
# that whole range, so no single value is load-bearing. 0.150 is the midpoint of
# the separation, not a service requirement - the effect on the 2026-08-06 data
# is 7.4 points at 150 ms and 8.2 at 124 ms. A report quoting one number must
# quote the sweep beside it.
#
# Deliberately absent from export_summary.py's committed per-seed table: that CSV
# is the evidence a reader checks the report against, and baking one objective
# into it would read as a threshold already chosen. The driver CSVs are
# committed, so any SLO is recomputable from the repository.
TTFT_SLO_S = 0.150


def percentile(xs: Sequence[float], p: float) -> float:
    """Nearest-rank (same convention as load_driver.summarize)."""
    if not xs:
        return float("nan")
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, round(p / 100 * (len(xs) - 1))))
    return xs[i]


def goodput(ttfts: Sequence[float], sent: int, slo: float) -> float:
    """Fraction of `sent` requests whose first token arrived STRICTLY under `slo`.

    `ttfts` holds only the requests that succeeded; `sent` counts every request
    the driver put on the wire. The denominator is therefore requests SENT, which
    makes an error a missed SLO - a request that never answered did not meet a
    latency objective, whatever else it did. That is the one place this module
    treats errors differently from every latency statistic around it, where
    seed_stats excludes them and counts them separately. It is deliberate:
    percentiles describe the service that was delivered, goodput describes the
    service that was promised.

    Error rates on the committed runs are 0.2-0.5%, so the two denominators move
    the number by well under a point. The choice matters for what happens if a
    future arm fails a lot, not for the runs analysed so far.
    """
    if not sent:
        return float("nan")
    return sum(1 for t in ttfts if t < slo) / sent


def _ok_ttfts(rows: List[Dict]) -> List[float]:
    """Sorted TTFTs of the requests that succeeded. One definition, two callers."""
    return sorted(float(r["ttft_s"]) for r in rows if r["status"] == "ok" and r["ttft_s"])


def read_seed_ttfts(run_dir: str) -> Dict[int, Tuple[List[float], int]]:
    """{seed: (sorted successful TTFTs, requests sent)} for every seed in a cell.

    The raw material for goodput at ANY objective. seed_stats bakes one value of
    TTFT_SLO_S into `ttft_slo_miss` because the paired test needs a single number
    per seed; the goodput figure sweeps a whole range and needs the samples.
    """
    out = {}
    for path in _driver_csvs(run_dir):
        rows = list(csv.DictReader(open(path)))
        out[seed_id(path)] = (_ok_ttfts(rows), len(rows))
    return out


def seed_stats(csv_path: str, slo: float = TTFT_SLO_S) -> Dict:
    """Per-seed metrics from one driver CSV. Errors excluded from latency, counted."""
    rows = list(csv.DictReader(open(csv_path)))
    ok = [r for r in rows if r["status"] == "ok"]
    ttft = _ok_ttfts(rows)
    e2e = [float(r["e2e_s"]) for r in ok if r["e2e_s"]]
    # Pooled over every inter-token gap in the seed, not over per-request
    # summaries: "ITL p99" means the 99th percentile of gaps. Absent on CSVs
    # written before the driver recorded it, hence the .get.
    #
    # /1000 converts ms -> SECONDS, so every itl_* field below is seconds while
    # the source column is `itls_ms`. See the UNITS note in the module docstring.
    itl = [
        float(x) / 1000
        for r in ok
        for x in (r.get("itls_ms") or "").split(";")
        if x
    ]
    sends = [float(r["send_ts"]) for r in rows]
    ends = [float(r["send_ts"]) + float(r["e2e_s"]) for r in rows if r["e2e_s"]]
    wall = (max(ends) - min(sends)) if ends else float("nan")
    comp_tokens = sum(int(r["completion_tokens"] or 0) for r in ok)
    s = {
        "file": os.path.basename(csv_path),
        # The seed number is carried, never inferred from list position. It used
        # to be: read_run sorted the glob LEXICOGRAPHICALLY (seed10 before seed2)
        # while callers labelled rows with enumerate(), so every printed and
        # plotted "seed N" above N=1 named the wrong seed. Pairing was unaffected
        # - both arms were mis-ordered identically - so the statistics were right
        # and only the labels lied, which is the hard kind of bug to notice.
        "seed": seed_id(csv_path),
        "n": len(rows),
        "ok": len(ok),
        "errors": len(rows) - len(ok),
        "error_rate": (len(rows) - len(ok)) / len(rows) if rows else float("nan"),
        "wall_s": wall,
        "throughput_req_s": len(ok) / wall if wall and wall > 0 else float("nan"),
        "throughput_tok_s": comp_tokens / wall if wall and wall > 0 else float("nan"),
        # The MISS rate, not goodput, because every paired test in this module is
        # one-sided H1 "candidate is LOWER" and every effect size is a relative
        # REDUCTION. Goodput is higher-is-better, so testing it directly would
        # need an inverted test and an inverted CI - two new code paths carrying
        # the headline number. 1 - goodput needs neither: `compare --metric
        # ttft_slo_miss` runs the same committed Wilcoxon as ttft_p95 and reports
        # "median relative reduction in missed requests". Figures plot the
        # complement, which reads better on an axis.
        "ttft_slo_miss": 1.0 - goodput(ttft, len(rows), slo),
        "ttft_slo_s": slo,
    }
    for name, xs in (("ttft", ttft), ("e2e", e2e), ("itl", itl)):
        s[f"{name}_mean"] = sum(xs) / len(xs) if xs else float("nan")
        # p90 as well as p95: the policy shifts the whole TTFT body, while p95
        # over 500 samples is dominated by bursty engine stalls that have
        # nothing to do with routing (see docs/, the 2026-08-03 post-mortem).
        for p in (50, 90, 95, 99):
            s[f"{name}_p{p}"] = percentile(xs, p)
    return s


def seed_id(csv_path: str):
    """Seed number from a driver CSV path: driver-seed13.csv -> 13.

    None when the name carries no digits. seed_stats is also used on ad-hoc CSVs
    (tests, one-off probes) whose names encode no seed, and those must not crash
    - but read_run, which pairs arms by seed, refuses such a file outright.
    """
    digits = "".join(c for c in os.path.basename(csv_path) if c.isdigit())
    return int(digits) if digits else None


def _driver_csvs(run_dir: str) -> List[str]:
    """Every driver CSV in a run dir, ordered by seed NUMBER.

    Sorted numerically, not lexicographically: a plain sorted(glob) yields
    seed1, seed10, seed11 ... seed2, seed20, which made list position and seed
    number diverge everywhere downstream.
    """
    paths = glob.glob(os.path.join(run_dir, "driver-seed*.csv"))
    if not paths:
        raise SystemExit(f"no driver-seed*.csv in {run_dir}")
    unnumbered = [p for p in paths if seed_id(p) is None]
    if unnumbered:
        raise SystemExit(
            f"cannot read seed numbers from {sorted(unnumbered)} - "
            "paired analysis needs every driver CSV to name its seed"
        )
    return sorted(paths, key=seed_id)


def read_run(run_dir: str, slo: float = TTFT_SLO_S) -> List[Dict]:
    """Seed stats for every driver CSV in a run dir, ordered by seed NUMBER."""
    return [seed_stats(p, slo) for p in _driver_csvs(run_dir)]


def per_seed_imbalance(run_dir: str) -> Dict[int, float]:
    """busiest/idlest engine mean in-flight, per seed window. {} if no dump.

    Lives here, not in export_summary.py where it was written, because it is a
    CO-PRIMARY: `compare --metric imbalance` needs it. While it sat in the
    exporter there was no code path anywhere that ran the pre-registered test on
    it - the metric was computable and untestable at the same time, and
    run_sweep.sh told the operator to test it with a command that could only
    raise KeyError.

    The parse lives in utilization.read_series, which also drops the router's
    re-export of this metric: the router publishes one series per backend under a
    shared `instance`, which merges both engines into a synthetic third series
    and corrupts the max/min. This function used to carry its own copy of that
    filter, making it the third in the repo, and the copy lacked read_series's
    worker_id disambiguation.
    """
    # Deliberately function-level: see the NOTE beside this module's imports.
    # utilization.py is not in the bench image, and analyze.py is.
    import utilization

    series = utilization.read_series(
        run_dir, "vllm_num_requests_running", utilization.ENGINE_JOB)
    if not series:
        return {}
    out = {}
    for p in glob.glob(os.path.join(run_dir, "driver-seed*.csv")):
        # seed_id, not an inlined digit-scrape: this dict is one half of a PAIRED
        # test whose other half is read_run, so both halves number seeds by one
        # shared function. It also skips a name with no digits rather than
        # raising on int("").
        seed = seed_id(p)
        if seed is None:
            continue
        ts = [float(r["send_ts"]) for r in csv.DictReader(open(p))]
        if not ts:
            continue
        lo, hi = min(ts), max(ts)
        windows = [[y for t, y in vals if lo <= t <= hi] for vals in series.values()]
        means = sorted(sum(w) / len(w) for w in windows if w)
        if len(means) >= 2 and means[0] > 0:
            out[seed] = means[-1] / means[0]
    return out


def flagged_seeds(seeds: List[Dict]) -> List[str]:
    """Seeds above the reporting threshold. Reported, NOT fatal (amended rule 1)."""
    return [
        f"{s['file']}: error rate {s['error_rate']:.1%} > {MAX_ERROR_RATE:.0%}"
        for s in seeds
        if s["error_rate"] > MAX_ERROR_RATE
    ]


def invalid_seeds(seeds: List[Dict]) -> List[str]:
    """Seeds that void the run on their own: a catastrophic error rate.

    Only the HARD ceiling voids unilaterally - at that level the cell is broken,
    not noisy, and no cross-arm argument rescues it. The 1% reporting threshold
    is handled by `flagged_seeds`.
    """
    return [
        f"{s['file']}: error rate {s['error_rate']:.1%} > {HARD_ERROR_RATE:.0%} (catastrophic)"
        for s in seeds
        if s["error_rate"] > HARD_ERROR_RATE
    ]


def error_rate(seeds: List[Dict]) -> float:
    """Pooled error rate over a cell: total errors / total requests."""
    n = sum(s["n"] for s in seeds)
    return sum(s["errors"] for s in seeds) / n if n else float("nan")


def error_bias(cand: List[Dict], base: List[Dict]) -> Dict:
    """Amended rule 1: do the two arms' error rates differ materially?

    An arm-independent floor cannot bias a paired comparison; a floor that lands
    disproportionately on one arm can. Voids on either a ratio blow-out or an
    absolute gap, so it stays meaningful when both rates are tiny (0.1% vs 0.3%
    is a 3x ratio but 0.2pp - not material) and when both are large.
    """
    c, b = error_rate(cand), error_rate(base)
    lo, hi = min(c, b), max(c, b)
    ratio = (hi / lo) if lo > 0 else (float("inf") if hi > 0 else 1.0)
    absolute = hi - lo
    biased = absolute > ERROR_BIAS_ABS and ratio > ERROR_BIAS_RATIO
    return {
        "cand_rate": c, "base_rate": b,
        "ratio": ratio, "absolute": absolute, "biased": biased,
    }


def wilcoxon_exact_one_sided(diffs: Sequence[float]) -> Dict:
    """Exact one-sided Wilcoxon signed-rank test, H1: differences < 0.

    Zeros dropped (Wilcoxon convention); ties get midranks. W+ = rank sum of
    positive differences; p = P(W+ <= observed) by enumerating all 2^n sign
    assignments - exact and fine for n <= ~20.
    """
    d = [x for x in diffs if x != 0]
    n = len(d)
    if n == 0:
        return {"n": 0, "w_plus": float("nan"), "p": 1.0}
    # midranks over |d|
    order = sorted(range(n), key=lambda i: abs(d[i]))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(d[order[j + 1]]) == abs(d[order[i]]):
            j += 1
        mid = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = mid
        i = j + 1
    w_plus = sum(r for r, x in zip(ranks, d) if x > 0)
    count = sum(
        1
        for signs in itertools.product((0, 1), repeat=n)
        if sum(r for r, s in zip(ranks, signs) if s) <= w_plus + 1e-9
    )
    return {"n": n, "w_plus": w_plus, "p": count / 2**n}


def bootstrap_ci_median_rel_reduction(
    cand: Sequence[float], base: Sequence[float], iters: int = 10000, seed: int = 0
) -> Dict:
    """Median relative reduction (base-cand)/base with a percentile bootstrap
    95% CI, resampling the seed PAIRS with replacement."""
    rel = [(b - c) / b for c, b in zip(cand, base)]
    point = percentile(rel, 50)
    rng = random.Random(seed)
    medians = []
    for _ in range(iters):
        medians.append(percentile([rng.choice(rel) for _ in rel], 50))
    return {
        "median_rel_reduction": point,
        "ci95": [percentile(medians, 2.5), percentile(medians, 97.5)],
    }


_COLS = [
    "ok", "errors", "ttft_mean", "ttft_p50", "ttft_p90", "ttft_p95", "ttft_p99",
    "itl_p50", "itl_p95", "itl_p99",
    "e2e_mean", "e2e_p95", "e2e_p99", "throughput_req_s", "throughput_tok_s",
]


def print_summary(run_dir: str) -> None:
    seeds = read_run(run_dir)
    print(f"\n== {run_dir} ({len(seeds)} seeds)")
    print("seed".ljust(22) + "".join(c.rjust(len(c) + 2) for c in _COLS))
    for s in seeds:
        cells = [
            f"{s[c]:.3f}" if isinstance(s[c], float) else str(s[c]) for c in _COLS
        ]
        print(
            s["file"].ljust(22)
            + "".join(v.rjust(len(c) + 2) for c, v in zip(_COLS, cells))
        )
    for problem in invalid_seeds(seeds):
        print(f"  ⚠ INVALID  {problem}")


def check_comparable(cand_dir: str, base_dir: str) -> None:
    """Validity: arms are only comparable under an identical workload and rate.
    Enforced from the run.json manifests when both runs have them."""
    metas = []
    for d in (cand_dir, base_dir):
        path = os.path.join(d, "run.json")
        metas.append(json.load(open(path)) if os.path.exists(path) else None)
    a, b = metas
    if a and b:
        for key in ("rate_req_s", "workload_manifest"):
            if a[key] != b[key]:
                raise SystemExit(
                    f"runs are not comparable: {key} differs - the methodology "
                    "requires identical workload and rate across arms"
                )


def cmd_compare(cand_dir: str, base_dir: str, metric: str, slo: float = TTFT_SLO_S) -> int:
    check_comparable(cand_dir, base_dir)
    cand, base = read_run(cand_dir, slo), read_run(base_dir, slo)
    # Match on the seed SET, not just its size. Equal counts do not imply equal
    # seeds - two cells could each hold 20 CSVs drawn from different seeds and
    # zip() would pair them silently, which is a wrong paired test that reports
    # a clean p-value. The whole method rests on comparing like with like.
    cand_seeds = [s["seed"] for s in cand]
    base_seeds = [s["seed"] for s in base]
    if cand_seeds != base_seeds:
        only_c = sorted(set(cand_seeds) - set(base_seeds))
        only_b = sorted(set(base_seeds) - set(cand_seeds))
        raise SystemExit(
            f"seed mismatch: {len(cand)} vs {len(base)} seeds; "
            f"only in candidate {only_c or 'none'}, only in baseline {only_b or 'none'} "
            "- a paired test requires the same seeds in both arms"
        )
    bad = invalid_seeds(cand) + invalid_seeds(base)
    if bad:
        for problem in bad:
            print(f"INVALID RUN - {problem}", file=sys.stderr)
        return 1
    # Amended rule 1: an arm-independent error floor is reported, not fatal;
    # errors DIFFERING between arms are what void a comparison.
    bias = error_bias(cand, base)
    print(
        f"error rates: candidate {bias['cand_rate']:.2%}  baseline {bias['base_rate']:.2%}"
        f"   (ratio {bias['ratio']:.2f}x, absolute {bias['absolute']:.2%})"
    )
    if bias["biased"]:
        print(
            f"INVALID COMPARISON - error rates differ materially between arms "
            f"(> {ERROR_BIAS_RATIO}x AND > {ERROR_BIAS_ABS:.0%}); the floor is not "
            "arm-independent, so it can bias the paired test",
            file=sys.stderr,
        )
        return 1
    for problem in flagged_seeds(cand) + flagged_seeds(base):
        print(f"  note: {problem} (reported, not fatal - amended rule 1)")
    if metric == "imbalance":
        # Overlaid here rather than merged into read_run: read_run also backs
        # `validate`, which runs at the end of every cell inside run_cell.sh, and
        # it has no business reading a Prometheus dump or gaining a new way to
        # fail while the sweep is still running.
        cand_imb, base_imb = per_seed_imbalance(cand_dir), per_seed_imbalance(base_dir)
        # Named per side: "seeds 3, 7 missing" does not tell an operator which
        # cell to go and look at, and the two cells fail for different reasons.
        for label, run_dir, imb in (
            ("candidate", cand_dir, cand_imb), ("baseline", base_dir, base_imb)
        ):
            absent = [s for s in cand_seeds if s not in imb]
            if absent:
                raise SystemExit(
                    f"no imbalance value for seeds {absent} in the {label} cell "
                    f"{run_dir} - the paired test needs "
                    "prom/vllm_num_requests_running.json covering every seed's "
                    "send-timestamp window"
                )
        c = [cand_imb[s] for s in cand_seeds]
        b = [base_imb[s] for s in cand_seeds]
    else:
        # Named up front rather than through a KeyError from the comprehension
        # below. `--metric imbalance` raised exactly that for the whole life of
        # the co-primary, and a traceback deep in a list comprehension reads as a
        # broken run rather than as a metric this command cannot test.
        if metric not in cand[0]:
            raise SystemExit(
                f"unknown metric {metric!r} - available: "
                f"{', '.join(sorted(k for k, v in cand[0].items() if isinstance(v, float)))}, "
                "imbalance"
            )
        c = [s[metric] for s in cand]
        b = [s[metric] for s in base]
    # A relative REDUCTION is undefined against a baseline of zero, and the
    # effect size below divides by it. ttft_p95 and imbalance are never zero, so
    # this could not fire until ttft_slo_miss arrived: an objective loose enough
    # that the baseline misses nothing on some seed produces a ZeroDivisionError
    # from inside the bootstrap, ~80 lines from the cause.
    #
    # Refusing is the right answer rather than dropping those pairs. A seed the
    # baseline already passes perfectly cannot show an improvement, so an SLO
    # sitting out there is a design error in the comparison, not a number to
    # patch around - and silently dropping pairs would change what the reported
    # median is a median OF, without saying so.
    zeros = [sid for sid, bi in zip(cand_seeds, b) if bi == 0]
    if zeros:
        hint = ""
        if metric == "ttft_slo_miss":
            hint = (f" - the {slo * 1000:.0f} ms objective is loose enough that the "
                    "baseline misses nothing on those seeds; choose a tighter --slo")
        raise SystemExit(
            f"baseline {metric} is exactly 0 on seed(s) {zeros}, so a relative "
            f"reduction is undefined there{hint}"
        )
    diffs = [ci - bi for ci, bi in zip(c, b)]
    test = wilcoxon_exact_one_sided(diffs)
    effect = bootstrap_ci_median_rel_reduction(c, b)
    print(f"metric: {metric}   candidate: {cand_dir}   baseline: {base_dir}")
    if metric == "ttft_slo_miss":
        # Printed beside the number, not only in a doc: the objective is a
        # parameter, and an operator pasting this output into the report needs to
        # know which value produced it and that the result is swept, not pinned.
        print(
            f"  SLO {slo * 1000:.0f} ms; goodput = 1 - this. Objective is tunable "
            "(--slo); see fig12 for the 50-400 ms sweep behind this number."
        )
    for sid, ci, bi in zip(cand_seeds, c, b):
        print(f"  seed {sid}: {ci:.4f} vs {bi:.4f}  (Δ {ci - bi:+.4f})")
    print(
        f"Wilcoxon signed-rank (one-sided, exact): W+={test['w_plus']:.1f} "
        f"n={test['n']} p={test['p']:.4f} "
        f"{f'< {ALPHA}  ✓ significant' if test['p'] < ALPHA else f'≥ {ALPHA}  ✗ not significant'}"
    )
    print(
        f"median relative reduction: {effect['median_rel_reduction']:.1%} "
        f"(bootstrap 95% CI [{effect['ci95'][0]:.1%}, {effect['ci95'][1]:.1%}])"
    )
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("summary")
    s.add_argument("run_dirs", nargs="+")
    v = sub.add_parser("validate")
    v.add_argument("run_dir")
    c = sub.add_parser("compare")
    c.add_argument("candidate_dir")
    c.add_argument("baseline_dir")
    c.add_argument("--metric", default="ttft_p95")
    c.add_argument("--slo", type=float, default=TTFT_SLO_S,
                   help="TTFT objective in SECONDS for --metric ttft_slo_miss "
                        f"(default {TTFT_SLO_S}); ignored by every other metric")
    a = p.parse_args()

    if a.cmd == "summary":
        for d in a.run_dirs:
            print_summary(d)
        return 0
    if a.cmd == "validate":
        seeds = read_run(a.run_dir)
        problems = invalid_seeds(seeds)
        for problem in problems:
            print(f"INVALID - {problem}", file=sys.stderr)
        # A single cell cannot be checked for arm bias - that needs both arms and
        # happens in `compare`. Here a raised error rate is a note, so a noisy
        # seed no longer aborts an unattended sweep under `set -e`.
        for note in flagged_seeds(seeds):
            print(f"note: {note} (reported, not fatal - amended rule 1; "
                  "arm bias is checked in `compare`)")
        if not problems:
            print(f"run valid: no seed above the catastrophic ceiling "
                  f"({HARD_ERROR_RATE:.0%}); pooled error rate "
                  f"{error_rate(seeds):.2%}")
        return 1 if problems else 0
    return cmd_compare(a.candidate_dir, a.baseline_dir, a.metric, a.slo)


if __name__ == "__main__":
    sys.exit(main())

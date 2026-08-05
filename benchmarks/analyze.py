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

Usage:
  python3 analyze.py summary  results/<run>...
  python3 analyze.py validate results/<run>            # exit 1 → do not use
  python3 analyze.py compare  results/<cand> results/<base> [--metric ttft_p95]
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
from typing import Dict, List, Sequence

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
MAX_ERROR_RATE = 0.01       # reporting threshold - flags a seed, does not void
HARD_ERROR_RATE = 0.10      # catastrophic: something is broken, void regardless
ERROR_BIAS_RATIO = 2.0      # arm error rates differing by more than this -> void
ERROR_BIAS_ABS = 0.01       # ...or by more than 1 percentage point absolute


def percentile(xs: Sequence[float], p: float) -> float:
    """Nearest-rank (same convention as load_driver.summarize)."""
    if not xs:
        return float("nan")
    xs = sorted(xs)
    i = min(len(xs) - 1, max(0, round(p / 100 * (len(xs) - 1))))
    return xs[i]


def seed_stats(csv_path: str) -> Dict:
    """Per-seed metrics from one driver CSV. Errors excluded from latency, counted."""
    rows = list(csv.DictReader(open(csv_path)))
    ok = [r for r in rows if r["status"] == "ok"]
    ttft = [float(r["ttft_s"]) for r in ok if r["ttft_s"]]
    e2e = [float(r["e2e_s"]) for r in ok if r["e2e_s"]]
    # Pooled over every inter-token gap in the seed, not over per-request
    # summaries: "ITL p99" means the 99th percentile of gaps. Absent on CSVs
    # written before the driver recorded it, hence the .get.
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


def read_run(run_dir: str) -> List[Dict]:
    """Seed stats for every driver CSV in a run dir, ordered by seed NUMBER.

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
    return [seed_stats(p) for p in sorted(paths, key=seed_id)]


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


def cmd_compare(cand_dir: str, base_dir: str, metric: str) -> int:
    check_comparable(cand_dir, base_dir)
    cand, base = read_run(cand_dir), read_run(base_dir)
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
    c = [s[metric] for s in cand]
    b = [s[metric] for s in base]
    diffs = [ci - bi for ci, bi in zip(c, b)]
    test = wilcoxon_exact_one_sided(diffs)
    effect = bootstrap_ci_median_rel_reduction(c, b)
    print(f"metric: {metric}   candidate: {cand_dir}   baseline: {base_dir}")
    for sid, ci, bi in zip(cand_seeds, c, b):
        print(f"  seed {sid}: {ci:.4f} vs {bi:.4f}  (Δ {ci - bi:+.4f})")
    print(
        f"Wilcoxon signed-rank (one-sided, exact): W+={test['w_plus']:.1f} "
        f"n={test['n']} p={test['p']:.4f} "
        f"{'< 0.05  ✓ significant' if test['p'] < 0.05 else '≥ 0.05  ✗ not significant'}"
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
    return cmd_compare(a.candidate_dir, a.baseline_dir, a.metric)


if __name__ == "__main__":
    sys.exit(main())

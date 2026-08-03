"""Analysis for benchmark runs: per-seed summaries, validity gate, paired stats.

Implements the PRE-REGISTERED test from issue #3, in code before any data
exists:
  - one run (seed replay) = ONE observation; per-request samples are
    queue-correlated and never treated as independent evidence
  - headline: one-sided exact Wilcoxon signed-rank on the 6 paired per-seed
    differences (candidate - baseline), H1 = candidate is LOWER, p < 0.05
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

MAX_ERROR_RATE = 0.01  # >1% errors invalidates the seed (pre-registered)


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
    sends = [float(r["send_ts"]) for r in rows]
    ends = [float(r["send_ts"]) + float(r["e2e_s"]) for r in rows if r["e2e_s"]]
    wall = (max(ends) - min(sends)) if ends else float("nan")
    comp_tokens = sum(int(r["completion_tokens"] or 0) for r in ok)
    s = {
        "file": os.path.basename(csv_path),
        "n": len(rows),
        "ok": len(ok),
        "errors": len(rows) - len(ok),
        "error_rate": (len(rows) - len(ok)) / len(rows) if rows else float("nan"),
        "wall_s": wall,
        "throughput_req_s": len(ok) / wall if wall and wall > 0 else float("nan"),
        "throughput_tok_s": comp_tokens / wall if wall and wall > 0 else float("nan"),
    }
    for name, xs in (("ttft", ttft), ("e2e", e2e)):
        s[f"{name}_mean"] = sum(xs) / len(xs) if xs else float("nan")
        for p in (50, 95, 99):
            s[f"{name}_p{p}"] = percentile(xs, p)
    return s


def read_run(run_dir: str) -> List[Dict]:
    """Seed stats for every driver CSV in a run dir, ordered by seed number."""
    paths = sorted(glob.glob(os.path.join(run_dir, "driver-seed*.csv")))
    if not paths:
        raise SystemExit(f"no driver-seed*.csv in {run_dir}")
    return [seed_stats(p) for p in paths]


def invalid_seeds(seeds: List[Dict]) -> List[str]:
    return [
        f"{s['file']}: error rate {s['error_rate']:.1%} > {MAX_ERROR_RATE:.0%}"
        for s in seeds
        if s["error_rate"] > MAX_ERROR_RATE
    ]


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
    "ok", "errors", "ttft_mean", "ttft_p50", "ttft_p95", "ttft_p99",
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
    if len(cand) != len(base):
        raise SystemExit(f"seed count mismatch: {len(cand)} vs {len(base)}")
    bad = invalid_seeds(cand) + invalid_seeds(base)
    if bad:
        for problem in bad:
            print(f"INVALID RUN - {problem}", file=sys.stderr)
        return 1
    c = [s[metric] for s in cand]
    b = [s[metric] for s in base]
    diffs = [ci - bi for ci, bi in zip(c, b)]
    test = wilcoxon_exact_one_sided(diffs)
    effect = bootstrap_ci_median_rel_reduction(c, b)
    print(f"metric: {metric}   candidate: {cand_dir}   baseline: {base_dir}")
    for i, (ci, bi) in enumerate(zip(c, b), 1):
        print(f"  seed {i}: {ci:.4f} vs {bi:.4f}  (Δ {ci - bi:+.4f})")
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
        problems = invalid_seeds(read_run(a.run_dir))
        for problem in problems:
            print(f"INVALID - {problem}", file=sys.stderr)
        if not problems:
            print("run valid: all seeds under the error threshold")
        return 1 if problems else 0
    return cmd_compare(a.candidate_dir, a.baseline_dir, a.metric)


if __name__ == "__main__":
    sys.exit(main())

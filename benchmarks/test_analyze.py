"""Unit tests for the analysis layer (run: pytest benchmarks/).

The Wilcoxon implementation is the pre-registered headline test (issue #3), so
it gets the heaviest scrutiny: known exact values for N=6, a brute-force
cross-check, zero/tie handling.
"""

import csv
import itertools
import math
import random

import pytest

from analyze import (
    MAX_ERROR_RATE,
    bootstrap_ci_median_rel_reduction,
    invalid_seeds,
    percentile,
    seed_stats,
    wilcoxon_exact_one_sided,
)


# ---- percentile -------------------------------------------------------------

def test_percentile_nearest_rank():
    xs = list(range(1, 102))  # 1..101, odd length → unambiguous median
    assert percentile(xs, 50) == 51
    assert percentile(xs, 95) == 96
    assert percentile(xs, 100) == 101
    assert percentile(xs, 0) == 1
    assert percentile(list(range(1, 101)), 95) == 95


def test_percentile_empty_is_nan():
    assert math.isnan(percentile([], 95))


# ---- Wilcoxon ---------------------------------------------------------------

def test_wilcoxon_all_negative_n6_is_1_over_64():
    # every seed improved → W+ = 0 → p = 1/2^6
    r = wilcoxon_exact_one_sided([-1, -2, -3, -4, -5, -6])
    assert r["w_plus"] == 0
    assert math.isclose(r["p"], 1 / 64)


def test_wilcoxon_one_small_wrongway_seed_still_significant():
    # 5 improvements, 1 tiny regression (smallest |d| → rank 1): W+ = 1 → p = 2/64
    r = wilcoxon_exact_one_sided([-10, -9, -8, -7, -6, 0.5])
    assert r["w_plus"] == 1
    assert math.isclose(r["p"], 2 / 64)
    assert r["p"] < 0.05


def test_wilcoxon_all_positive_is_never_significant():
    r = wilcoxon_exact_one_sided([1, 2, 3, 4, 5, 6])
    assert r["p"] == 1.0


def test_wilcoxon_drops_zeros():
    r = wilcoxon_exact_one_sided([-1, -2, 0, 0, -3])
    assert r["n"] == 3
    assert math.isclose(r["p"], 1 / 8)


def test_wilcoxon_matches_bruteforce_random_inputs():
    # independent brute force: enumerate sign assignments over midranks
    rng = random.Random(7)
    for _ in range(20):
        d = [round(rng.uniform(-5, 5), 1) for _ in range(rng.randint(3, 8))]
        d = [x for x in d if x != 0]
        if not d:
            continue
        r = wilcoxon_exact_one_sided(d)
        n = len(d)
        srt = sorted(range(n), key=lambda i: abs(d[i]))
        ranks = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and abs(d[srt[j + 1]]) == abs(d[srt[i]]):
                j += 1
            for k in range(i, j + 1):
                ranks[srt[k]] = (i + j) / 2 + 1
            i = j + 1
        w_obs = sum(r_ for r_, x in zip(ranks, d) if x > 0)
        count = sum(
            1
            for signs in itertools.product((0, 1), repeat=n)
            if sum(r_ for r_, s in zip(ranks, signs) if s) <= w_obs + 1e-9
        )
        assert math.isclose(r["p"], count / 2**n), f"diffs={d}"


# ---- bootstrap --------------------------------------------------------------

def test_bootstrap_point_estimate_and_determinism():
    cand = [0.8, 0.9, 0.85, 0.7, 0.95, 0.75]
    base = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    a = bootstrap_ci_median_rel_reduction(cand, base, iters=2000, seed=1)
    b = bootstrap_ci_median_rel_reduction(cand, base, iters=2000, seed=1)
    assert a == b, "bootstrap must be seeded/deterministic"
    # median of reductions [0.2,0.1,0.15,0.3,0.05,0.25] → nearest-rank p50
    assert 0.05 <= a["median_rel_reduction"] <= 0.3
    lo, hi = a["ci95"]
    assert lo <= a["median_rel_reduction"] <= hi
    assert 0.05 <= lo and hi <= 0.3  # CI bounded by observed reductions


def test_bootstrap_degenerate_identical_pairs():
    r = bootstrap_ci_median_rel_reduction([2.0] * 6, [2.0] * 6, iters=100, seed=0)
    assert r["median_rel_reduction"] == 0.0
    assert r["ci95"] == [0.0, 0.0]


# ---- seed stats + validity --------------------------------------------------

def _write_csv(path, rows):
    fields = [
        "index", "prefix_id", "send_ts", "ttft_s", "e2e_s",
        "prompt_tokens", "completion_tokens", "status", "error",
    ]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def _row(i, ttft, e2e, status="ok"):
    return {
        "index": i, "prefix_id": 0, "send_ts": 1000.0 + i,
        "ttft_s": ttft if status == "ok" else "",
        "e2e_s": e2e, "prompt_tokens": 2080, "completion_tokens": 64,
        "status": status, "error": "" if status == "ok" else "boom",
    }


def test_seed_stats_excludes_errors_from_latency_but_counts_them(tmp_path):
    p = tmp_path / "driver-seed1.csv"
    rows = [_row(i, 0.1 * (i + 1), 1.0) for i in range(4)]
    rows.append(_row(4, 99.0, 99.0, status="error"))
    _write_csv(p, rows)
    s = seed_stats(str(p))
    assert s["n"] == 5 and s["ok"] == 4 and s["errors"] == 1
    assert s["error_rate"] == pytest.approx(0.2)
    # error row's 99s TTFT must not pollute stats
    assert s["ttft_p99"] <= 0.4


def test_validity_threshold_is_1_percent(tmp_path):
    ok = tmp_path / "ok.csv"
    _write_csv(ok, [_row(i, 0.1, 1.0) for i in range(100)])
    bad = tmp_path / "bad.csv"
    rows = [_row(i, 0.1, 1.0) for i in range(98)]
    rows += [_row(98, 0.1, 1.0, "error"), _row(99, 0.1, 1.0, "error")]
    _write_csv(bad, rows)
    assert invalid_seeds([seed_stats(str(ok))]) == []
    assert len(invalid_seeds([seed_stats(str(bad))])) == 1
    assert MAX_ERROR_RATE == 0.01

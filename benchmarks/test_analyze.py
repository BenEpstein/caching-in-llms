"""Unit tests for the analysis layer (run: pytest benchmarks/).

The Wilcoxon implementation is the pre-registered headline test (issue #3), so
it gets the heaviest scrutiny: known exact values for N=6, a brute-force
cross-check, zero/tie handling.
"""

import csv
import math

import pytest

from analyze import (
    HARD_ERROR_RATE,
    error_bias,
    flagged_seeds,
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


def test_wilcoxon_midranks_on_ties():
    # hand-computed: |d| = 1,1,2 → midranks 1.5,1.5,3; W+ = 3 (the +2).
    # Over 2^3 sign assignments the rank sums are {0,1.5,1.5,3,3,4.5,4.5,6},
    # so P(W+ <= 3) = 5/8.
    r = wilcoxon_exact_one_sided([-1, -1, 2])
    assert r["w_plus"] == 3
    assert math.isclose(r["p"], 5 / 8)


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


def _seed_with_errors(tmp_path, name, n_err, n_total=100):
    p = tmp_path / name
    rows = [_row(i, 0.1, 1.0) for i in range(n_total - n_err)]
    rows += [_row(n_total - n_err + j, 0.1, 1.0, "error") for j in range(n_err)]
    _write_csv(p, rows)
    return seed_stats(str(p))


def test_two_percent_errors_are_flagged_but_do_not_void(tmp_path):
    """Amended rule 1 (2026-08-04, pre-registered on #3 before the run). The old
    rule voided a whole run on one seed over 1%; near the knee that fires on
    noise (probe: 0.8% and exactly 1.0% per seed) and would discard 85 minutes
    for an error floor already shown to be arm-independent."""
    s = _seed_with_errors(tmp_path, "noisy.csv", 2)
    assert len(flagged_seeds([s])) == 1      # reported...
    assert invalid_seeds([s]) == []          # ...but not fatal
    assert MAX_ERROR_RATE == 0.01


def test_catastrophic_error_rate_still_voids_unilaterally(tmp_path):
    """A cell that is broken rather than noisy is not rescued by any cross-arm
    argument."""
    s = _seed_with_errors(tmp_path, "broken.csv", 20)   # 20%
    assert len(invalid_seeds([s])) == 1
    assert HARD_ERROR_RATE == 0.10


def test_arm_independent_error_floor_is_not_bias(tmp_path):
    """The floor that actually occurred: both arms ~1%. It cannot bias a paired
    comparison, so the comparison stands."""
    cand = [_seed_with_errors(tmp_path, f"c{i}.csv", 1) for i in range(3)]
    base = [_seed_with_errors(tmp_path, f"b{i}.csv", 1) for i in range(3)]
    assert error_bias(cand, base)["biased"] is False


def test_errors_concentrated_on_one_arm_void_the_comparison(tmp_path):
    """The failure mode rule 1 exists to catch: if one arm errors far more, the
    surviving requests are a biased sample of that arm's latency."""
    cand = [_seed_with_errors(tmp_path, f"c{i}.csv", 0) for i in range(3)]
    base = [_seed_with_errors(tmp_path, f"b{i}.csv", 5) for i in range(3)]
    bias = error_bias(cand, base)
    assert bias["biased"] is True
    assert bias["absolute"] == pytest.approx(0.05)


def test_bias_needs_both_a_ratio_and_an_absolute_gap(tmp_path):
    """0.0% vs 1.0% is an infinite ratio but only a 1pp gap - not material, and
    voiding on ratio alone would make the rule fire on near-zero noise."""
    cand = [_seed_with_errors(tmp_path, f"c{i}.csv", 0) for i in range(3)]
    base = [_seed_with_errors(tmp_path, f"b{i}.csv", 1) for i in range(3)]
    bias = error_bias(cand, base)
    assert bias["ratio"] == float("inf")
    assert bias["biased"] is False

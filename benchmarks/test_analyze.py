"""Unit tests for the analysis layer (run: pytest benchmarks/).

The Wilcoxon implementation is the pre-registered headline test (issue #3), so
it gets the heaviest scrutiny: known exact values for N=6, a brute-force
cross-check, zero/tie handling.
"""

import csv
import json
import math
import os

import pytest

from analyze import (
    HARD_ERROR_RATE,
    cmd_compare,
    read_run,
    seed_id,
    error_bias,
    flagged_seeds,
    MAX_ERROR_RATE,
    bootstrap_ci_median_rel_reduction,
    invalid_seeds,
    per_seed_imbalance,
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


# ---- seed identity ----------------------------------------------------------
# Regression guard for the 2026-08-04 mislabelling defect: read_run sorted the
# glob lexicographically (seed10 before seed2) while callers labelled rows with
# enumerate(), so every printed and plotted "seed N" above N=1 named the wrong
# seed. Pairing was unaffected - both arms mis-ordered identically - so the
# statistics stayed correct and only the labels lied. Nothing failed loudly.

def _run_dir(tmp_path, name, seeds):
    d = tmp_path / name
    d.mkdir()
    for s in seeds:
        _write_csv(d / f"driver-seed{s}.csv", [_row(i, 0.1 * s, 1.0) for i in range(4)])
    return str(d)


def test_seed_id_parses_the_number_not_the_position():
    assert seed_id("results/x/driver-seed13.csv") == 13
    assert seed_id("driver-seed1.csv") == 1


def test_read_run_orders_numerically_not_lexicographically(tmp_path):
    """sorted(glob) gives 1,10,11..19,2,20,3..9 - the trap that caused the bug."""
    d = _run_dir(tmp_path, "cell", range(1, 21))
    assert [s["seed"] for s in read_run(d)] == list(range(1, 21))


def test_list_position_equals_seed_number_after_the_fix(tmp_path):
    """The invariant the labels relied on, now actually true rather than assumed."""
    d = _run_dir(tmp_path, "cell", range(1, 21))
    for i, s in enumerate(read_run(d), start=1):
        assert s["seed"] == i, f"position {i} carries seed {s['seed']}"


def test_seed_stats_carries_its_seed(tmp_path):
    d = _run_dir(tmp_path, "cell", [7])
    assert read_run(d)[0]["seed"] == 7


def test_compare_rejects_equal_counts_drawn_from_different_seeds(tmp_path):
    """Equal length is not equal seeds: zip() would pair seed 5 against seed 21
    and report a clean p-value for a comparison that never happened."""
    cand = _run_dir(tmp_path, "cand", [1, 2, 3])
    base = _run_dir(tmp_path, "base", [1, 2, 99])
    with pytest.raises(SystemExit) as e:
        cmd_compare(cand, base, "ttft_p95")
    assert "seed mismatch" in str(e.value)


def test_compare_accepts_matching_seed_sets(tmp_path):
    cand = _run_dir(tmp_path, "cand", [1, 2, 3])
    base = _run_dir(tmp_path, "base", [1, 2, 3])
    assert cmd_compare(cand, base, "ttft_p95") == 0


# ---- imbalance co-primary ---------------------------------------------------
#
# Added 2026-08-06. Until then `compare --metric imbalance` raised KeyError:
# per_seed_imbalance lived in export_summary.py, so the co-primary was
# computable and untestable at once, and run_sweep.sh printed a command for it
# that could not work.

def _prom(run_dir, engines, router=None, lo=1000.0, hi=1003.0):
    """Write a vllm_num_requests_running dump. engines: {pod: constant value}."""
    result = [
        {"metric": {"job": "vllm-engines", "pod": pod},
         "values": [[t, str(v)] for t in (lo, (lo + hi) / 2, hi)]}
        for pod, v in engines.items()
    ]
    if router is not None:
        # The trap the function documents: the router re-exports the same metric
        # for every backend under one instance label.
        result.append(
            {"metric": {"job": "router", "instance": "stack-router-service:80",
                        "server": "engine-1"},
             "values": [[t, str(router)] for t in (lo, (lo + hi) / 2, hi)]}
        )
    p = os.path.join(run_dir, "prom")
    os.makedirs(p, exist_ok=True)
    with open(os.path.join(p, "vllm_num_requests_running.json"), "w") as f:
        json.dump({"data": {"result": result}}, f)


def test_per_seed_imbalance_is_busiest_over_idlest(tmp_path):
    d = _run_dir(tmp_path, "cell", [1])
    _prom(d, {"engine-a": 4.0, "engine-b": 1.0})
    assert per_seed_imbalance(d) == {1: pytest.approx(4.0)}


def test_per_seed_imbalance_windows_each_seed_separately(tmp_path):
    """The reason the function is per-SEED at all.

    Every other test here gives all seeds the same send window and a constant
    series, so they would pass identically if the window filter were deleted.
    Here seed 1 sends while the fleet is 4:1 and seed 2 while it is 2:1. Ignore
    the windows and both seeds read the pooled 3:1 instead.
    """
    d = str(tmp_path / "cell")
    os.makedirs(d)
    for seed, base in ((1, 1000.0), (2, 2000.0)):
        rows = [_row(i, 0.1, 1.0) for i in range(4)]
        for i, r in enumerate(rows):
            r["send_ts"] = base + i
        _write_csv(os.path.join(d, f"driver-seed{seed}.csv"), rows)

    busy = [[t, "4.0"] for t in (1000.0, 1001.5, 1003.0)] + \
           [[t, "2.0"] for t in (2000.0, 2001.5, 2003.0)]
    idle = [[t, "1.0"] for t in (1000.0, 1001.5, 1003.0, 2000.0, 2001.5, 2003.0)]
    os.makedirs(os.path.join(d, "prom"))
    with open(os.path.join(d, "prom", "vllm_num_requests_running.json"), "w") as f:
        json.dump({"data": {"result": [
            {"metric": {"job": "vllm-engines", "pod": "engine-a"}, "values": busy},
            {"metric": {"job": "vllm-engines", "pod": "engine-b"}, "values": idle},
        ]}}, f)

    assert per_seed_imbalance(d) == {1: pytest.approx(4.0), 2: pytest.approx(2.0)}


def test_per_seed_imbalance_ignores_the_router_job(tmp_path):
    """A router series must not enter the max/min as a phantom third engine."""
    d = _run_dir(tmp_path, "cell", [1])
    _prom(d, {"engine-a": 4.0, "engine-b": 1.0}, router=99.0)
    assert per_seed_imbalance(d) == {1: pytest.approx(4.0)}


def test_per_seed_imbalance_is_empty_without_a_dump(tmp_path):
    d = _run_dir(tmp_path, "cell", [1, 2])
    assert per_seed_imbalance(d) == {}


def test_per_seed_imbalance_keys_by_seed_number_not_list_position(tmp_path):
    d = _run_dir(tmp_path, "cell", [1, 10])
    _prom(d, {"engine-a": 3.0, "engine-b": 1.0})
    assert sorted(per_seed_imbalance(d)) == [1, 10]


def test_compare_runs_the_imbalance_metric(tmp_path):
    """The regression that matters: this raised KeyError before the metric moved."""
    cand = _run_dir(tmp_path, "cand", [1, 2, 3])
    base = _run_dir(tmp_path, "base", [1, 2, 3])
    _prom(cand, {"engine-a": 1.2, "engine-b": 1.0})
    _prom(base, {"engine-a": 4.0, "engine-b": 1.0})
    assert cmd_compare(cand, base, "imbalance") == 0


def test_compare_imbalance_without_a_dump_says_so(tmp_path):
    """Never a silent partial pairing: absent imbalance is a hard, named error."""
    cand = _run_dir(tmp_path, "cand", [1, 2, 3])
    base = _run_dir(tmp_path, "base", [1, 2, 3])
    _prom(cand, {"engine-a": 1.2, "engine-b": 1.0})
    with pytest.raises(SystemExit) as e:
        cmd_compare(cand, base, "imbalance")
    assert "no imbalance value for seeds" in str(e.value)


# ---- bench-image import contract --------------------------------------------

def test_analyze_module_level_imports_survive_the_bench_image():
    """analyze.py must not import a local module the bench image does not ship.

    load_driver.py does `from analyze import percentile` and runs INSIDE that
    image, so a module-level `import utilization` in analyze.py makes
    `import load_driver` raise ModuleNotFoundError there - breaking the
    measurement path for every future sweep. That shipped to main on 2026-08-06
    and turned the bench-image workflow red.

    It WAS catchable before merge and was missed. bench-image.yml triggers on
    `push` with a paths filter, so it runs on any branch push touching
    benchmarks/**: it ran on that commit and failed at 22:41:23Z, and the merge
    happened at 22:48:13Z. The PR head at merge was a later docs-only commit that
    the paths filter skipped, so no run existed for the head SHA and
    `statusCheckRollup` carried the previous commit's success forward - green,
    from a commit that predated the break.

    This test runs under pytest on every commit, with no paths filter and nothing
    to carry forward, so the signal cannot go stale the same way. It reads
    Dockerfile.bench rather than hardcoding the file list so it stays true when
    that list changes.
    """
    import ast

    bench = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(bench)

    dockerfile = open(os.path.join(root, "Dockerfile.bench")).read()
    # The COPY lines are backslash-continued; flatten before scanning.
    shipped = {
        os.path.basename(tok)[:-3]
        for tok in dockerfile.replace("\\\n", " ").split()
        if tok.startswith("benchmarks/") and tok.endswith(".py")
    }
    assert "analyze" in shipped, "this test assumes analyze.py ships in the image"

    local = {f[:-3] for f in os.listdir(bench) if f.endswith(".py")}
    tree = ast.parse(open(os.path.join(bench, "analyze.py")).read())
    top_level = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]

    imported = set()
    for node in top_level:
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])

    missing = sorted((imported & local) - shipped)
    assert not missing, (
        f"analyze.py imports {missing} at module level, but Dockerfile.bench does "
        f"not COPY {missing} into the image. `import load_driver` will raise "
        f"ModuleNotFoundError in the bench image. Either move the import inside "
        f"the function that needs it, or add the file to Dockerfile.bench (which "
        f"changes BENCH_TAG and needs a rebuilt, re-validated image)."
    )

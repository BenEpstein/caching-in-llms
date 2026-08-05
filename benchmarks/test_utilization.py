"""Tests for the §3 utilization readout and its coverage gate (#35).

The regression case that matters is `test_tail_truncation_is_caught`: the #27
b0.5 pilot lost the last 171 s of a 712 s window as a clean tail truncation with
no internal gaps, and every check the harness had at the time passed.
"""

import json
import os
import subprocess
import sys

import pytest

import utilization

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))
START, END = 1_000_000.0, 1_000_700.0


def prom_payload(series):
    """A query_range response shaped like the real one: [(labels, [(ts, val)])]."""
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {"metric": labels, "values": [[ts, str(v)] for ts, v in pts]}
                for labels, pts in series
            ],
        },
    }


def flat(start, end, step=5.0, value=0.5):
    return [(t, value) for t in _times(start, end, step)]


def _times(start, end, step):
    t = start
    while t <= end:
        yield t
        t += step


@pytest.fixture
def cell(tmp_path):
    """A minimal well-formed cell: full-window Prometheus series + run.json."""
    d = tmp_path / "20260806-000000-loadaware-b0.5"
    (d / "prom").mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({
        "cell": "loadaware-b0.5", "arm": "loadaware", "beta": "0.5",
        "window": {"start_ts": int(START), "end_ts": int(END)},
    }))
    write_prom(d, "vllm_kv_cache_usage_perc", [
        ({"job": "vllm-engines", "pod": "engine-a"}, flat(START, END, value=0.2)),
        ({"job": "vllm-engines", "pod": "engine-b"}, flat(START, END, value=0.4)),
    ])
    write_prom(d, "process_cpu_seconds_total", [
        ({"job": "router", "instance": "r:80"},
         [(t, (t - START) * 0.2) for t in _times(START, END, 5.0)]),
    ])
    return d


def write_prom(d, metric, series):
    (d / "prom" / f"{metric}.json").write_text(json.dumps(prom_payload(series)))


def write_dcgm(d, rows):
    lines = ["ts,metric,gpu,hostname,value"]
    lines += [f"{ts},{m},{g},{h},{v}" for ts, m, g, h, v in rows]
    (d / "dcgm.csv").write_text("\n".join(lines) + "\n")


# ---- reading ---------------------------------------------------------------

def test_engine_series_are_split_per_pod(cell):
    u = utilization.cell_utilization(str(cell))
    kv = u["engines"]["vllm_kv_cache_usage_perc"]
    assert set(kv) == {"engine-a", "engine-b"}
    assert kv["engine-a"]["mean"] == pytest.approx(0.2)
    assert kv["engine-b"]["mean"] == pytest.approx(0.4)


def test_router_reexport_of_engine_series_is_not_counted(cell):
    """The router re-exports vllm:* per backend under ONE shared `instance`.

    Without the job filter those samples merge both engines into a single
    synthetic series, and the spread - the whole point of the panel - collapses.
    """
    write_prom(cell, "vllm_kv_cache_usage_perc", [
        ({"job": "vllm-engines", "pod": "engine-a"}, flat(START, END, value=0.2)),
        ({"job": "vllm-engines", "pod": "engine-b"}, flat(START, END, value=0.4)),
        ({"job": "router", "instance": "r:80"}, flat(START, END, value=9.9)),
    ])
    kv = utilization.cell_utilization(str(cell))["engines"]["vllm_kv_cache_usage_perc"]
    assert set(kv) == {"engine-a", "engine-b"}


def test_counter_rate_on_a_clean_counter(cell):
    r = utilization.cell_utilization(str(cell))["router"]["process_cpu_seconds_total"]
    assert r["rate"] == pytest.approx(0.2)


def test_counter_rate_survives_a_restart(cell):
    """The router is scraped through its Service, so it carries no `pod` label
    and a restart does not change the series key. Differencing the endpoints
    would report a restarted router as CHEAPER - 0.114 against a true 0.2 - on
    the exact metric behind "router CPU is flat across arms"."""
    ts = list(_times(START, END, 5.0))
    reset_at = START + 300
    write_prom(cell, "process_cpu_seconds_total", [
        ({"job": "router", "instance": "r:80"},
         [(t, (t - START) * 0.2 if t < reset_at else (t - reset_at) * 0.2) for t in ts]),
    ])
    r = utilization.cell_utilization(str(cell))["router"]["process_cpu_seconds_total"]
    # One sampling interval of counts is lost across the reset itself, no more.
    assert r["rate"] == pytest.approx(0.2, rel=0.02)


def test_one_pod_with_several_label_sets_is_not_averaged_together(cell):
    """The lmcache gauges carry worker_id. Keyed on pod alone, a pod holding
    4 GB on worker 0 and 0 GB on worker 1 reads as a flat 2 GB - a number that
    is neither."""
    write_prom(cell, "lmcache_local_cache_usage", [
        ({"job": "vllm-engines", "pod": "engine-a", "worker_id": "0"},
         flat(START, END, value=4e9)),
        ({"job": "vllm-engines", "pod": "engine-a", "worker_id": "1"},
         flat(START, END, value=0.0)),
    ])
    per_pod = utilization.cell_utilization(str(cell))["engines"]["lmcache_local_cache_usage"]
    assert sorted(v["mean"] for v in per_pod.values()) == [0.0, 4e9]


def test_spread_is_idlest_then_busiest(cell):
    kv = utilization.cell_utilization(str(cell))["engines"]["vllm_kv_cache_usage_perc"]
    assert utilization.spread(kv) == pytest.approx((0.2, 0.4))


def test_spread_needs_two_engines():
    """One engine has no spread; returning (x, x) would draw a 1.00x bar that
    reads as a balanced result rather than as missing data."""
    assert utilization.spread({"only": {"mean": 0.3, "n": 5}}) is None


def test_nan_and_inf_samples_are_dropped(cell):
    (cell / "prom" / "router_cpu_usage_percent.json").write_text(json.dumps({
        "data": {"result": [{"metric": {"job": "router", "instance": "r:80"},
                             "values": [[START, "NaN"], [START + 5, "10"],
                                        [START + 10, "+Inf"], [START + 15, "20"]]}]}
    }))
    r = utilization.cell_utilization(str(cell))["router"]["router_cpu_usage_percent"]
    assert r["n"] == 2 and r["mean"] == pytest.approx(15.0)


def test_missing_prom_dir_does_not_raise(tmp_path):
    """A cell whose Prometheus dump failed still has valid driver CSVs, and
    utilization must not be the thing that discards it."""
    d = tmp_path / "cell"
    d.mkdir()
    (d / "run.json").write_text(json.dumps(
        {"window": {"start_ts": int(START), "end_ts": int(END)}}))
    u = utilization.cell_utilization(str(d))
    assert u["engines"] == {} and u["router"] == {} and u["gpu"] == {}


def test_engine_process_metrics_are_declared_unavailable(cell):
    """vLLM registers no process collector, so engine host-CPU/RSS is reported
    as absent rather than silently omitted from the figure."""
    assert utilization.cell_utilization(str(cell))["unavailable"] == [
        "engine process_cpu_seconds_total",
        "engine process_resident_memory_bytes",
    ]


# ---- DCGM ------------------------------------------------------------------

def test_dcgm_is_keyed_by_host_and_gpu(cell):
    write_dcgm(cell, [
        (START, "DCGM_FI_DEV_GPU_UTIL", 0, "worker0", 90),
        (START, "DCGM_FI_DEV_GPU_UTIL", 0, "worker1", 80),
        (END, "DCGM_FI_DEV_GPU_UTIL", 0, "worker0", 90),
        (END, "DCGM_FI_DEV_GPU_UTIL", 0, "worker1", 80),
    ])
    gpu = utilization.cell_utilization(str(cell))["gpu"]["DCGM_FI_DEV_GPU_UTIL"]
    # Every node's first GPU is "gpu0"; keying on the index alone loses one node.
    assert len(gpu) == 2
    assert {utilization.short_gpu(k) for k in gpu} == {"worker0/gpu0", "worker1/gpu0"}


def test_unparseable_dcgm_rows_are_skipped(cell):
    write_dcgm(cell, [
        (START, "DCGM_FI_DEV_GPU_UTIL", 0, "worker0", 90),
        ("bad", "DCGM_FI_DEV_GPU_UTIL", 0, "worker0", 90),
        (END, "DCGM_FI_DEV_GPU_UTIL", 0, "worker0", 70),
    ])
    gpu = utilization.cell_utilization(str(cell))["gpu"]["DCGM_FI_DEV_GPU_UTIL"]
    assert next(iter(gpu.values()))["n"] == 2


# ---- the coverage gate -----------------------------------------------------

def test_full_window_series_is_full_coverage(cell):
    cov = utilization.coverage(str(cell))
    assert cov["vllm_kv_cache_usage_perc"] == pytest.approx(1.0, abs=0.02)


def test_tail_truncation_is_caught(cell):
    """The #27 b0.5 pilot, exactly: dcgm.csv stopped 171 s before a 712 s window
    closed - 24% of the cell, as a clean tail truncation with no internal gaps.

    A sample-density check passes this: the samples that exist are perfectly
    regular. Only measuring against the window catches it.
    """
    write_dcgm(cell, [
        (t, "DCGM_FI_DEV_GPU_UTIL", 0, "worker0", 90)
        for t in _times(START, END - 171.0, 5.0)
    ])
    cov = utilization.coverage(str(cell))
    assert cov["DCGM_FI_DEV_GPU_UTIL"] < utilization.MIN_COVERAGE


def test_internal_gap_is_caught(cell):
    """The failure mode the DCGM supervisors CREATED (#35).

    Before them a dropped port-forward ended the series. Now it reconnects, so
    the same loss lands in the middle of the window instead of at the end. A
    span measure (max - min) scores that as perfect: on a real cell, dropping
    42% of the samples as one mid-window hole scored 99.8%.

    Same total loss as the tail-truncation case above, different shape - and it
    has to be caught just as hard, or the mitigation defeats the gate.
    """
    kept = [t for t in _times(START, END, 5.0)
            if not (START + 200 < t < START + 500)]
    write_dcgm(cell, [(t, "DCGM_FI_DEV_GPU_UTIL", 0, "worker0", 90) for t in kept])
    cov = utilization.coverage(str(cell))
    assert cov["DCGM_FI_DEV_GPU_UTIL"] < utilization.MIN_COVERAGE


def test_healthy_short_cell_does_not_false_warn(cell):
    """The gate must not cry wolf on pilots and demos.

    Samples land up to one sampling interval inside the window at each end, so a
    measure with no edge tolerance caps at 1 - step/window: `results/demo-a`
    (150 s) scored exactly 96.7% on perfect data, spending 3.3 of the 5-point
    budget on quantization alone.
    """
    short_end = START + 150.0
    (cell / "run.json").write_text(json.dumps(
        {"cell": "short", "window": {"start_ts": int(START), "end_ts": int(short_end)}}))
    write_dcgm(cell, [(t, "DCGM_FI_DEV_GPU_UTIL", 0, "worker0", 90)
                      for t in _times(START + 5, short_end - 5, 5.0)])
    assert utilization.coverage(str(cell))["DCGM_FI_DEV_GPU_UTIL"] >= utilization.MIN_COVERAGE


def test_total_loss_is_scored_zero_not_omitted(cell):
    """A source that produced nothing must not vanish from the report.

    Omitting the key leaves a run.json indistinguishable from a healthy cell -
    and total loss is worse than the 24% truncation the gate was built for.
    """
    write_dcgm(cell, [])                      # header only: the poller ran, got nothing
    write_prom(cell, "router_cpu_usage_percent", [])   # file present, no series
    cov = utilization.coverage(str(cell))
    assert cov["DCGM_FI_DEV_GPU_UTIL"] == 0.0
    assert cov["router_cpu_usage_percent"] == 0.0


def test_metric_absent_from_the_scrape_list_is_not_scored(cell):
    """The lmcache gauges post-date most cells on disk. No file means the metric
    was never requested for that run, which is not that cell failing - scoring
    it 0.0 would warn on every historical cell forever."""
    assert "lmcache_local_cache_usage" not in utilization.coverage(str(cell))


def test_coverage_reports_the_worst_engine_not_the_mean(cell):
    """One engine going dark IS the failure; averaging it against a healthy one
    hides exactly the thing the gate exists to surface."""
    write_prom(cell, "vllm_kv_cache_usage_perc", [
        ({"job": "vllm-engines", "pod": "engine-a"}, flat(START, END, value=0.2)),
        ({"job": "vllm-engines", "pod": "engine-b"}, flat(START, START + 100, value=0.4)),
    ])
    assert utilization.coverage(str(cell))["vllm_kv_cache_usage_perc"] == pytest.approx(
        100 / 700, abs=0.02)


def test_no_window_means_no_coverage(tmp_path):
    """Nothing to compare against - inventing a window from file mtimes would
    report a number that means nothing."""
    d = tmp_path / "cell"
    (d / "prom").mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"cell": "x"}))
    write_prom(d, "vllm_kv_cache_usage_perc", [
        ({"job": "vllm-engines", "pod": "a"}, flat(START, END))])
    assert utilization.coverage(str(d)) == {}


def test_coverage_writes_run_json_and_never_fails(cell):
    write_dcgm(cell, [(t, "DCGM_FI_DEV_GPU_UTIL", 0, "worker0", 90)
                      for t in _times(START, END - 300, 5.0)])
    rc = subprocess.run(
        [sys.executable, os.path.join(BENCH_DIR, "utilization.py"), "coverage",
         str(cell), "--update-run-json"],
        capture_output=True, text=True)
    # Short coverage warns; it must not fail the cell - the driver CSVs are the
    # primary measurement.
    assert rc.returncode == 0
    assert "WARNING" in rc.stderr
    run = json.loads((cell / "run.json").read_text())
    assert run["utilization_coverage"]["DCGM_FI_DEV_GPU_UTIL"] < utilization.MIN_COVERAGE
    assert run["cell"] == "loadaware-b0.5", "existing manifest keys must survive"


def test_report_runs_on_a_cell_with_no_dcgm(cell):
    rc = subprocess.run(
        [sys.executable, os.path.join(BENCH_DIR, "utilization.py"), "report", str(cell)],
        capture_output=True, text=True)
    assert rc.returncode == 0
    assert "GPU memory" in rc.stdout
    assert "no dcgm.csv" in rc.stdout

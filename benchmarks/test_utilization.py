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

sys.path.insert(0, os.path.dirname(__file__))

import utilization  # noqa: E402

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


def ramp(start, end, step=5.0, value=0.5):
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
        ({"job": "vllm-engines", "pod": "engine-a"}, ramp(START, END, value=0.2)),
        ({"job": "vllm-engines", "pod": "engine-b"}, ramp(START, END, value=0.4)),
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
        ({"job": "vllm-engines", "pod": "engine-a"}, ramp(START, END, value=0.2)),
        ({"job": "vllm-engines", "pod": "engine-b"}, ramp(START, END, value=0.4)),
        ({"job": "router", "instance": "r:80"}, ramp(START, END, value=9.9)),
    ])
    kv = utilization.cell_utilization(str(cell))["engines"]["vllm_kv_cache_usage_perc"]
    assert set(kv) == {"engine-a", "engine-b"}


def test_counter_rate_is_endpoint_to_endpoint(cell):
    r = utilization.cell_utilization(str(cell))["router"]["process_cpu_seconds_total"]
    assert r["rate"] == pytest.approx(0.2)


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

    A density check passes this (the samples that exist are perfectly regular);
    only a span check against the window catches it.
    """
    stop = END - 171.0
    write_dcgm(cell, [
        (t, "DCGM_FI_DEV_GPU_UTIL", 0, "worker0", 90)
        for t in _times(START, stop, 5.0)
    ])
    cov = utilization.coverage(str(cell))
    assert cov["DCGM_FI_DEV_GPU_UTIL"] == pytest.approx(1 - 171 / 700, abs=0.02)
    assert cov["DCGM_FI_DEV_GPU_UTIL"] < utilization.MIN_COVERAGE


def test_coverage_reports_the_worst_engine_not_the_mean(cell):
    """One engine going dark IS the failure; averaging it against a healthy one
    hides exactly the thing the gate exists to surface."""
    write_prom(cell, "vllm_kv_cache_usage_perc", [
        ({"job": "vllm-engines", "pod": "engine-a"}, ramp(START, END, value=0.2)),
        ({"job": "vllm-engines", "pod": "engine-b"}, ramp(START, START + 100, value=0.4)),
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
        ({"job": "vllm-engines", "pod": "a"}, ramp(START, END))])
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

"""Unit tests for the load gate and the untested analysis paths (#7 follow-up).

Two groups:

  1. `load_gate` - the new precondition. The bug it exists to prevent is a sweep
     that passes every validity rule and still cannot test the hypothesis, so
     the important cases are the FAILURES: saturated-but-symmetric, and
     asymmetric-but-idle (which is exactly what the 2026-08-03 sweep was).
  2. The paths Ben flagged as having zero coverage: ITL parsing, the old-CSV
     `.get()` fallback, and the `job=vllm-engines` filter - that last one
     silently corrupted 17 of 74 committed seed rows before it was caught, and
     nothing stopped it returning.
"""

import json

import pytest

from analyze import seed_stats
from load_gate import MIN_ASYMMETRY, _engine_series, gate, relative_imbalance

# --------------------------------------------------------------------------
# fixtures: a minimal run directory
# --------------------------------------------------------------------------

_CSV_HEADER = (
    "index,prefix_id,send_ts,ttft_s,e2e_s,prompt_tokens,completion_tokens,"
    "status,error,itls_ms\n"
)


def _write_run(tmp_path, running, waiting=None, preempt=None, sends=(100.0, 110.0),
               rate=16.0, job="vllm-engines", ttft=0.40, itl_ms=45.0):
    """Build a run dir with one driver CSV and hand-made Prometheus dumps.

    `running` is {pod: [(ts, value)]}. Timestamps inside `sends` are in-window.
    """
    run = tmp_path / "run"
    (run / "prom").mkdir(parents=True)
    lo, hi = sends
    rows = [
        f"{i},0,{ts},{ttft},1.0,100,64,ok,,{itl_ms};{itl_ms}\n"
        for i, ts in enumerate((lo, hi))
    ]
    (run / "driver-seed1.csv").write_text(_CSV_HEADER + "".join(rows))
    (run / "run.json").write_text(json.dumps({"rate_req_s": rate}))

    def dump(name, series):
        (run / "prom" / f"{name}.json").write_text(json.dumps({
            "data": {"result": [
                {"metric": {"job": job, "pod": pod},
                 "values": [[t, str(v)] for t, v in vals]}
                for pod, vals in (series or {}).items()
            ]}
        }))

    dump("vllm_num_requests_running", running)
    dump("vllm_num_requests_waiting", waiting)
    dump("vllm_num_preemptions_total", preempt)
    return str(run)


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------


def test_asymmetric_but_undegraded_fails_this_is_the_20260803_sweep(tmp_path):
    """The case the gate exists for: kvaware concentrates load 1.7x, but latency
    is at idle levels, so the concentration costs nothing and beta has nothing to
    buy. Every validity rule passed and the run still could not test the
    hypothesis."""
    run = _write_run(
        tmp_path,
        running={"a": [(100.0, 18.9), (110.0, 18.9)], "b": [(100.0, 11.2), (110.0, 11.2)]},
        ttft=0.17, itl_ms=32.0,        # ~idle baseline
    )
    r = gate(run)
    assert r["asymmetric"] is True
    assert r["degraded"] is False
    assert r["pass"] is False


def test_degraded_but_symmetric_fails(tmp_path):
    """Both engines equally loaded is not an opportunity - there is nowhere
    better to send the request, so a load-aware policy cannot help however bad
    the latency is."""
    run = _write_run(
        tmp_path,
        running={"a": [(100.0, 40.0), (110.0, 40.0)], "b": [(100.0, 39.0), (110.0, 39.0)]},
        ttft=0.90, itl_ms=90.0,
    )
    r = gate(run)
    assert r["degraded"] is True
    assert r["asymmetric"] is False
    assert r["pass"] is False


def test_degraded_and_asymmetric_passes(tmp_path):
    run = _write_run(
        tmp_path,
        running={"a": [(100.0, 40.0), (110.0, 40.0)], "b": [(100.0, 16.0), (110.0, 16.0)]},
        ttft=0.60, itl_ms=70.0,
    )
    r = gate(run)
    assert r["pass"] is True
    assert r["delta_load"] == pytest.approx(24.0)


def test_itl_degradation_alone_is_enough(tmp_path):
    """At a COMPUTE-bound operating point the batch grows and every token slows,
    so ITL degrades even when TTFT has barely moved. Requiring TTFT to degrade
    too would miss the mechanism this policy actually acts on."""
    run = _write_run(
        tmp_path,
        running={"a": [(100.0, 40.0), (110.0, 40.0)], "b": [(100.0, 16.0), (110.0, 16.0)]},
        ttft=0.17, itl_ms=90.0,        # TTFT idle-ish, ITL 2.9x baseline
    )
    r = gate(run)
    assert r["degraded"] is True
    assert r["pass"] is True


def test_a_cell_with_no_ttft_is_unmeasurable_not_a_pass(tmp_path):
    """A cell recorded with the pre-2026-08-04 driver has an empty TTFT column.
    The gate must refuse to judge it rather than silently reporting FAIL (or,
    worse, PASS) on a metric that was never captured."""
    run = tmp_path / "run"
    (run / "prom").mkdir(parents=True)
    (run / "driver-seed1.csv").write_text(
        _CSV_HEADER
        + "0,0,100.0,,1.0,100,64,ok,,\n"      # no ttft, no itls - the bug's signature
        + "1,0,110.0,,1.0,100,64,ok,,\n"
    )
    (run / "run.json").write_text(json.dumps({"rate_req_s": 16.0}))
    (run / "prom" / "vllm_num_requests_running.json").write_text(json.dumps({
        "data": {"result": [
            {"metric": {"job": "vllm-engines", "pod": p},
             "values": [[100.0, str(v)], [110.0, str(v)]]}
            for p, v in (("a", 40.0), ("b", 16.0))
        ]}
    }))
    r = gate(str(run))
    assert r["measurable"] is False
    assert r["pass"] is False


def test_preemption_counter_is_differenced_not_summed(tmp_path):
    """`num_preemptions_total` is cumulative from engine start. Reading the raw
    value instead of the in-window delta is the same class of bug that gave the
    scarcity gate a false PASS at 0.085. It is reported for the record even
    though it is structurally 0 on this compute-bound workload."""
    run = _write_run(
        tmp_path,
        running={"a": [(100.0, 40.0), (110.0, 40.0)], "b": [(100.0, 16.0), (110.0, 16.0)]},
        preempt={"a": [(100.0, 500.0), (110.0, 500.0)]},  # 500 from BEFORE the window
    )
    assert gate(run)["preemptions"] == pytest.approx(0.0)


def test_out_of_window_samples_are_excluded(tmp_path):
    """Warm-up and the cold engine restart precede the first send. Including
    them dilutes mean in-flight toward zero, and that number feeds beta."""
    run = _write_run(
        tmp_path,
        running={
            "a": [(10.0, 0.0), (100.0, 40.0), (110.0, 40.0)],   # 10.0 = warm-up
            "b": [(10.0, 0.0), (100.0, 16.0), (110.0, 16.0)],
        },
        preempt={"a": [(100.0, 0.0), (110.0, 9.0)]},
    )
    r = gate(run)
    assert r["mean_busiest"] == pytest.approx(40.0)
    assert r["mean_idlest"] == pytest.approx(16.0)


def test_router_job_series_are_ignored(tmp_path):
    """`vllm:num_requests_running` is exported per-backend under job=router too,
    where all series share one instance label. Counting them creates a synthetic
    third 'engine' averaging the real two - it corrupted 17 of 74 seed rows."""
    run = _write_run(
        tmp_path,
        running={"a": [(100.0, 40.0)], "b": [(100.0, 16.0)]},
        job="router",
    )
    assert "error" in gate(run)
    assert _engine_series(run, "vllm_num_requests_running") == {}


def test_missing_dump_is_an_error_not_a_pass(tmp_path):
    run = tmp_path / "bare"
    (run / "prom").mkdir(parents=True)
    (run / "driver-seed1.csv").write_text(_CSV_HEADER + "0,0,100.0,0.1,1.0,1,1,ok,,\n")
    assert "error" in gate(str(run))


def test_min_asymmetry_is_the_documented_threshold():
    assert MIN_ASYMMETRY == 1.5


# --------------------------------------------------------------------------
# beta calibration
# --------------------------------------------------------------------------


def test_relative_imbalance_is_the_gap_as_a_fraction_of_the_fleet_mean():
    """The quantity the policy acts on, and the one §5 reports."""
    assert relative_imbalance(mean_busiest=48.0, mean_fleet=32.0) == pytest.approx(0.5)
    assert relative_imbalance(mean_busiest=32.0, mean_fleet=32.0) == pytest.approx(0.0)


def test_relative_imbalance_is_invariant_to_the_absolute_load_level():
    """Why it replaced `beta_from()`.

    The old calibration read beta off the ABSOLUTE gap, so the same shape of
    imbalance at a different concurrency produced a different beta: two probes
    at the same offered rate gave delta_load 39.46 and 14.69, i.e. beta 0.013
    and 0.034. Ten times the load with the same shape is the same imbalance.
    """
    assert relative_imbalance(48.0, 32.0) == pytest.approx(
        relative_imbalance(480.0, 320.0)
    )


def test_relative_imbalance_of_a_dead_fleet_is_zero_not_a_division_error():
    assert relative_imbalance(0.0, 0.0) == 0.0


def test_the_gate_reports_relative_imbalance_alongside_the_absolute_gap(tmp_path):
    """Both are emitted: the absolute number stays comparable with the older
    runs in results/, the relative one is what the policy sees."""
    run = _write_run(
        tmp_path,
        running={"engine-a": [(105.0, 48.0)], "engine-b": [(105.0, 16.0)]},
    )
    r = gate(run)
    assert r["delta_load"] == pytest.approx(32.0)
    assert r["mean_fleet"] == pytest.approx(32.0)
    assert r["relative_imbalance"] == pytest.approx(0.5)


# --------------------------------------------------------------------------
# previously uncovered analysis paths (Ben's open item 3)
# --------------------------------------------------------------------------


def test_itl_is_pooled_over_gaps_and_converted_to_seconds(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text(
        _CSV_HEADER
        + "0,0,100.0,0.1,1.0,10,3,ok,,10.0;20.0\n"
        + "1,0,101.0,0.1,1.0,10,3,ok,,30.0;40.0\n"
    )
    s = seed_stats(str(p))
    # pooled over all four gaps, not over two per-request summaries
    assert s["itl_mean"] == pytest.approx(0.025)
    # nearest-rank (analyze.percentile): index round(0.5*(4-1)) = 2 of
    # [0.01, 0.02, 0.03, 0.04]. Pinned so a change of convention is loud.
    assert s["itl_p50"] == pytest.approx(0.030, abs=1e-9)


def test_itl_absent_on_old_csvs_does_not_crash(tmp_path):
    """CSVs written before the driver recorded ITL have no such column; the
    `.get()` fallback must yield NaN rather than raising."""
    p = tmp_path / "old.csv"
    p.write_text(
        "index,prefix_id,send_ts,ttft_s,e2e_s,prompt_tokens,completion_tokens,status,error\n"
        "0,0,100.0,0.1,1.0,10,3,ok,\n"
    )
    s = seed_stats(str(p))
    assert s["itl_mean"] != s["itl_mean"]  # NaN
    assert s["ttft_p95"] == pytest.approx(0.1)


def test_errors_excluded_from_itl_but_still_counted(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text(
        _CSV_HEADER
        + "0,0,100.0,0.1,1.0,10,3,ok,,10.0\n"
        + "1,0,101.0,,,,,error,boom,999.0\n"
    )
    s = seed_stats(str(p))
    assert s["errors"] == 1
    assert s["itl_mean"] == pytest.approx(0.010)  # the error's 999 ms is excluded

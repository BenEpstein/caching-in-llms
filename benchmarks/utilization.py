"""Utilization metrics (§3): GPU, memory, CPU - read from the per-cell collectors.

§3 requires memory and CPU/GPU utilization alongside latency, hit rate and
throughput. Most of what this module reads was already being collected per cell
and read by nothing; the LMCache memory gauges were added alongside it (#35),
so they exist only on cells run from that commit onward.

Where each number comes from, and why (decided on #35, 2026-08-05):

  GPU utilization   DCGM_FI_DEV_GPU_UTIL / _POWER_USAGE / _MEM_COPY_UTIL, per GPU
                    vLLM's /metrics exposes 113 metric names and NONE of them is
                    SM% or power, so there is no Prometheus substitute for this
                    half of the requirement. DCGM stays the source of record and
                    is polled through port-forwards - promoting it into
                    Prometheus would need a RoleBinding in `nvidia-gpu-operator`,
                    which costs the property that `oc apply -f deploy/` works in
                    any namespace without cluster-admin.

  GPU memory        vllm:kv_cache_usage_perc, per engine.
                    Scraped in-cluster and therefore immune to the WAN that
                    truncates dcgm.csv. It is also the resource the policy
                    contends for, so it is the utilization series that actually
                    discriminates the arms (kvaware spreads 1.70x across the two
                    engines, loadaware b0.5 spreads 1.18x).

  Memory            lmcache:local_cache_usage, per engine (host RAM held by the
                    LMCache CPU backend, ~3.8 GB) + process_resident_memory_bytes
                    and router_memory_usage_percent for the router.

  CPU               router_cpu_usage_percent and process_cpu_seconds_total, router
                    only. The engines export no process_* metrics at all - verified
                    at the endpoint, not inferred from absence - so engine host-CPU
                    is UNAVAILABLE and is reported as such rather than faked. It is
                    also the uninteresting number: both arms run the identical model
                    at the identical offered rate and the engines are GPU-bound,
                    while the router is the only component the extension changes.

Coverage: every series is checked against the cell's measured window and the
covered fraction recorded in run.json. A utilization source that comes back
short must not do so silently - dcgm.csv once stopped 171 s before a 712 s
window closed (24% of the cell, as a clean tail truncation) and nothing noticed.
The gate WARNS and never fails: the driver CSVs are the primary measurement and
a cell with good latency data must not be discarded over utilization sampling.

Three ways a source can come back short, and all three are counted:

  tail truncation   the collector dies partway and never returns
  internal gap      the collector drops and RECONNECTS, leaving a hole in the
                    middle. This is the case the DCGM port-forward supervisors
                    created: they turned truncations into gaps, so a span-only
                    measure would have scored a cell missing 42% of its samples
                    at 99.8%. Coverage is therefore gap-aware, not a span.
  total loss        the collector produced a file with nothing in it. Scored 0.0
                    rather than omitted, because a missing KEY is indistinguishable
                    from a healthy cell once it reaches run.json - and total loss
                    is worse than the truncation this gate was built for.

Usage:
  python3 utilization.py report   results/<run>...
  python3 utilization.py coverage results/<run> [--update-run-json]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from typing import Dict, List, Optional, Sequence, Tuple

# Warn below this fraction of the measured window. Never fatal - see module docstring.
MIN_COVERAGE = 0.95
# A stretch with no samples is tolerated up to this many sampling intervals
# before it counts against coverage. Two intervals absorbs a single missed
# scrape (routine) while charging a genuine dropout (many intervals) in full.
GAP_TOLERANCE_STEPS = 2.0
# Fallback cadence when a series has too few samples to infer one: both
# collectors sample at 5 s (prom_dump --step, dcgm_poll --interval).
DEFAULT_STEP_S = 5.0

# Prometheus series this module reads, and the job whose samples count.
# The job filter is not cosmetic: the router re-exports vllm:* per backend under
# one shared `instance`, so dropping it merges both engines into one synthetic
# series (the same trap analyze.per_seed_imbalance documents).
ENGINE_JOB, ROUTER_JOB = "vllm-engines", "router"
ENGINE_GAUGES = ["vllm_kv_cache_usage_perc", "lmcache_local_cache_usage",
                 "lmcache_active_memory_objs_count"]
ROUTER_GAUGES = ["router_cpu_usage_percent", "router_memory_usage_percent",
                 "process_resident_memory_bytes"]
ROUTER_COUNTERS = ["process_cpu_seconds_total"]
ALL_SERIES = {
    **{m: ENGINE_JOB for m in ENGINE_GAUGES},
    **{m: ROUTER_JOB for m in ROUTER_GAUGES + ROUTER_COUNTERS},
}

DCGM_FIELDS = {
    "DCGM_FI_DEV_GPU_UTIL": "GPU SM utilization, percent",
    "DCGM_FI_DEV_POWER_USAGE": "GPU power draw, watts",
    "DCGM_FI_DEV_MEM_COPY_UTIL": "GPU memory-copy utilization, percent",
}

Samples = List[Tuple[float, float]]


def read_series(run_dir: str, metric: str, job: str) -> Dict[str, Samples]:
    """Prometheus query_range dump -> {series key: [(ts, value)]}, one entry per pod.

    Missing files return {} rather than raising: a cell whose Prometheus dump
    failed still has valid driver CSVs, and utilization is not allowed to be the
    thing that discards it.
    """
    path = os.path.join(run_dir, "prom", f"{metric}.json")
    if not os.path.exists(path):
        return {}
    out: Dict[str, Samples] = {}
    with open(path) as f:
        payload = json.load(f)
    for s in payload.get("data", {}).get("result", []):
        if s["metric"].get("job") != job:
            continue
        key = s["metric"].get("pod") or s["metric"].get("instance") or "?"
        # One pod can export the same metric under several label sets - the
        # lmcache gauges carry worker_id. Without it those series concatenate
        # under one key and average into a number that is neither: a pod holding
        # 4 GB on worker 0 and 0 GB on worker 1 reads as a flat 2 GB.
        if "worker_id" in s["metric"]:
            key = f"{key}/w{s['metric']['worker_id']}"
        pts = [
            (float(ts), float(v))
            for ts, v in s.get("values", [])
            if v not in ("NaN", "+Inf", "-Inf")
        ]
        if pts:
            out.setdefault(key, []).extend(pts)
    return {k: sorted(v) for k, v in out.items()}


def read_dcgm(run_dir: str) -> Dict[str, Dict[str, Samples]]:
    """dcgm.csv -> {field: {gpu: [(ts, value)]}}. Absent file -> {}."""
    path = os.path.join(run_dir, "dcgm.csv")
    if not os.path.exists(path):
        return {}
    out: Dict[str, Dict[str, Samples]] = {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            field = row.get("metric")
            if field not in DCGM_FIELDS:
                continue
            gpu = f"{row.get('hostname', '')}/gpu{row.get('gpu', '')}"
            try:
                pt = (float(row["ts"]), float(row["value"]))
            except (TypeError, ValueError):
                continue
            out.setdefault(field, {}).setdefault(gpu, []).append(pt)
    return {k: {g: sorted(v) for g, v in d.items()} for k, d in out.items()}


def gauge_stats(pts: Samples) -> Dict[str, float]:
    ys = [y for _, y in pts]
    if not ys:
        return {"mean": float("nan"), "max": float("nan"), "n": 0}
    return {"mean": sum(ys) / len(ys), "max": max(ys), "n": len(ys)}


def counter_rate(pts: Samples) -> float:
    """Per-second rate of a counter across the dumped window, reset-safe.

    Sums the POSITIVE deltas rather than differencing the endpoints. The
    difference matters: the router is scraped through its Service
    (deploy/prometheus.yaml), so there is no `pod` label and a restart does not
    change the series key - endpoint differencing would silently report a router
    that restarted mid-cell as cheaper. On a synthetic restart at t=300 in a
    700 s window it reported 0.114 against a true 0.2, and nothing flagged it.
    That number is the evidence behind "router CPU is flat across arms", so it
    has to survive a restart rather than assume one cannot happen.

    A reset still loses the counts accumulated between the last sample before it
    and the reset itself - at most one sampling interval's worth.
    """
    if len(pts) < 2:
        return float("nan")
    dt = pts[-1][0] - pts[0][0]
    if dt <= 0:
        return float("nan")
    return sum(max(0.0, b - a) for (_, a), (_, b) in zip(pts, pts[1:])) / dt


def manifest_window(run_dir: str) -> Optional[Tuple[float, float]]:
    """The cell's measured window from run.json, or None when unrecorded.

    NOT the same interval as load_gate._window(), which runs first-send to
    last-send and so excludes warm-up by construction. This one is
    CELL_START..CELL_END off the pod clock. Named apart on purpose - the two
    were one rename away from being used interchangeably.
    """
    path = os.path.join(run_dir, "run.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        w = json.load(f).get("window") or {}
    if "start_ts" in w and "end_ts" in w and w["end_ts"] > w["start_ts"]:
        return float(w["start_ts"]), float(w["end_ts"])
    return None


def _median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _covered_fraction(pts: Samples, start: float, end: float,
                      default_step: float = DEFAULT_STEP_S) -> float:
    """Fraction of [start, end] a series actually observed.

    Every stretch of wall-clock is credited up to GAP_TOLERANCE_STEPS sampling
    intervals; anything longer counts only that much, so a hole is charged
    against coverage whether it sits in the middle of the window or at either
    end. That is the whole point: a span measure (max - min) scores an internal
    gap as perfect, and the DCGM supervisors added in this same change turn
    dropped forwards into exactly that shape. Measured on a real cell, dropping
    42% of the samples as one mid-window hole scored 99.8% under a span.

    The sampling interval is inferred from the data (median inter-sample gap)
    rather than assumed, so a collector on a different cadence is judged against
    its own. The tolerance also gives the first and last sample a step of credit
    each, which is what keeps a healthy short cell off the warning list: samples
    land up to one interval inside the window at each end, so on a 150 s window
    at a 5 s step a perfect series would otherwise cap at 96.7%.
    """
    inside = sorted(t for t, _ in pts if start <= t <= end)
    window_s = end - start
    if not inside or window_s <= 0:
        return 0.0
    gaps = [b - a for a, b in zip(inside, inside[1:])]
    step = _median(gaps) if gaps else default_step
    if step <= 0:
        step = default_step
    tol = GAP_TOLERANCE_STEPS * step
    covered = sum(min(g, tol) for g in gaps)
    covered += min(inside[0] - start, tol)   # leading edge
    covered += min(end - inside[-1], tol)    # trailing edge
    return min(1.0, covered / window_s)


def coverage(run_dir: str) -> Dict[str, float]:
    """Covered fraction of the measured window, per utilization series.

    Empty when the cell records no window - there is nothing to compare against,
    and inventing one from file mtimes would report a number that means nothing.
    """
    win = manifest_window(run_dir)
    if not win:
        return {}
    start, end = win
    out: Dict[str, float] = {}
    for metric, job in ALL_SERIES.items():
        # An ABSENT dump file means the metric was not in that run's scrape list
        # (the lmcache gauges post-date most cells on disk), which is not this
        # cell failing. A file that EXISTS but yields nothing is a collector that
        # ran and came back empty - scored 0.0, never omitted, because a missing
        # key in run.json is indistinguishable from a healthy cell.
        if not os.path.exists(os.path.join(run_dir, "prom", f"{metric}.json")):
            continue
        series = read_series(run_dir, metric, job)
        # worst series, not the mean: one engine going dark is the failure, and
        # averaging it against a healthy one hides exactly that.
        out[metric] = (min(_covered_fraction(p, start, end) for p in series.values())
                       if series else 0.0)
    dcgm = read_dcgm(run_dir)
    for field in DCGM_FIELDS:
        per_gpu = dcgm.get(field) or {}
        # dcgm.csv missing or empty is scored, not skipped: DCGM is the source of
        # record for GPU utilization, so its absence IS the failure to report.
        out[field] = (min(_covered_fraction(p, start, end) for p in per_gpu.values())
                      if per_gpu else 0.0)
    return out


def cell_utilization(run_dir: str) -> Dict:
    """Every §3 utilization number for one cell, plus what is not available."""
    out: Dict = {
        "cell": os.path.basename(run_dir.rstrip("/")),
        "engines": {},
        "router": {},
        "gpu": {},
        # Stated, not silently omitted: vLLM registers no process collector, so
        # engine host-CPU and engine RSS cannot be reported from this deployment.
        # cmd_report prints this - an unreported limitation is not a stated one.
        "unavailable": ["engine process_cpu_seconds_total", "engine process_resident_memory_bytes"],
    }
    # Coverage is deliberately NOT computed here. It re-reads every file this
    # function already read, and the figure - the only other caller - throws it
    # away. cmd_coverage and cmd_report call coverage() directly instead.
    for metric in ENGINE_GAUGES:
        per_pod = read_series(run_dir, metric, ENGINE_JOB)
        if per_pod:
            out["engines"][metric] = {pod: gauge_stats(p) for pod, p in per_pod.items()}
    for metric in ROUTER_GAUGES:
        # Last series wins, and with one router replica there is only ever one.
        # A second replica would need this keyed by instance like the engines.
        for p in read_series(run_dir, metric, ROUTER_JOB).values():
            out["router"][metric] = gauge_stats(p)
    for metric in ROUTER_COUNTERS:
        for p in read_series(run_dir, metric, ROUTER_JOB).values():
            out["router"][metric] = {"rate": counter_rate(p), "n": len(p)}
    for field, per_gpu in read_dcgm(run_dir).items():
        out["gpu"][field] = {gpu: gauge_stats(p) for gpu, p in per_gpu.items()}
    return out


def spread(per_pod: Dict[str, Dict[str, float]]) -> Optional[Tuple[float, float]]:
    """(idlest, busiest) mean across engines - the imbalance in one resource.

    None below two engines: a one-engine cell has no spread to report, and
    returning (x, x) would draw a 1.00x bar that looks like a balanced result.
    """
    means = sorted(v["mean"] for v in per_pod.values() if v["n"])
    return (means[0], means[-1]) if len(means) >= 2 else None


def engine_labels(per_pod: Dict[str, Dict[str, float]]):
    """[(label, stats)] with engines numbered rather than named by pod hash.

    Engine pods are recreated every cell (#13 restarts them for identical
    initial state), so the pod suffix is a ReplicaSet hash - it renders as
    "rrcpr" or "b2xv8", changes between cells, and means nothing in a §6 table.
    Ordering is by pod name: arbitrary but stable within a cell, which is all a
    two-engine comparison needs.
    """
    return [(f"engine{i}", v) for i, (_, v) in enumerate(sorted(per_pod.items()), 1)]


def short_gpu(key: str) -> str:
    """"worker0.example.com/gpu0" -> "worker0/gpu0" - readable, still unique."""
    host, _, idx = key.rpartition("/")
    return f"{host.split('.')[0]}/{idx}" if host else idx


def _fmt(x: float, unit: str = "") -> str:
    return "n/a" if x != x else f"{x:,.3f}{unit}"


def cmd_report(run_dirs: Sequence[str]) -> int:
    for d in run_dirs:
        u = cell_utilization(d)
        print(f"\n== {u['cell']}")
        kv = u["engines"].get("vllm_kv_cache_usage_perc")
        if kv:
            sp = spread(kv)
            line = "  GPU memory (KV cache):  " + "  ".join(
                f"{lbl}={v['mean']:.3f} (max {v['max']:.3f})" for lbl, v in engine_labels(kv))
            if sp and sp[0] > 0:
                line += f"   spread {sp[1] / sp[0]:.2f}x"
            print(line)
        ram = u["engines"].get("lmcache_local_cache_usage")
        if ram:
            print("  LMCache host RAM:       " + "  ".join(
                f"{lbl}={v['mean'] / 1e9:.2f} GB" for lbl, v in engine_labels(ram)))
        objs = u["engines"].get("lmcache_active_memory_objs_count")
        if objs:
            print("  LMCache objects:        " + "  ".join(
                f"{lbl}={v['mean']:.0f}" for lbl, v in engine_labels(objs)))
        r = u["router"]
        if "process_cpu_seconds_total" in r:
            print(f"  Router CPU:             {_fmt(r['process_cpu_seconds_total']['rate'])} core-s/s"
                  f"   ({_fmt(r.get('router_cpu_usage_percent', {}).get('mean', float('nan')))}% reported)")
        if "process_resident_memory_bytes" in r:
            print(f"  Router memory:          {r['process_resident_memory_bytes']['mean'] / 1e9:.3f} GB"
                  f"   ({_fmt(r.get('router_memory_usage_percent', {}).get('mean', float('nan')))}% reported)")
        for field, per_gpu in sorted(u["gpu"].items()):
            # Keyed by host AND index: every node's first GPU is "gpu0", so
            # dropping the host silently prints two columns with the same name.
            print(f"  {DCGM_FIELDS[field]:<38}" + "  ".join(
                f"{short_gpu(gpu)}={v['mean']:.1f}" for gpu, v in sorted(per_gpu.items())))
        low = {k: v for k, v in coverage(d).items() if v < MIN_COVERAGE}
        if low:
            print("  ⚠ coverage below "
                  f"{MIN_COVERAGE:.0%}: " + ", ".join(f"{k} {v:.0%}" for k, v in sorted(low.items())))
        if not u["gpu"]:
            print("  note: no dcgm.csv - GPU utilization (SM%, power) unavailable for this cell")
        # The limitation is printed, not merely held in the dict: §3 asks for
        # CPU utilization and a reader running this needs to see which part of
        # it this deployment cannot supply.
        print("  not available:          " + ", ".join(u["unavailable"])
              + " (vLLM registers no process collector)")
    return 0


def cmd_coverage(run_dir: str, update_run_json: bool) -> int:
    cov = coverage(run_dir)
    if not cov:
        print(f"no utilization coverage computable for {run_dir} "
              "(no window in run.json, or no series collected)", file=sys.stderr)
        return 0
    for series, frac in sorted(cov.items()):
        flag = "  ⚠ SHORT" if frac < MIN_COVERAGE else ""
        print(f"{series:<38} {frac:6.1%}{flag}")
    low = {k: v for k, v in cov.items() if v < MIN_COVERAGE}
    if low:
        print(f"WARNING: {len(low)} utilization series covered less than "
              f"{MIN_COVERAGE:.0%} of the measured window in {run_dir}: "
              + ", ".join(f"{k} {v:.0%}" for k, v in sorted(low.items()))
              + " - reported, NOT fatal: the driver CSVs are the primary measurement",
              file=sys.stderr)
    if update_run_json:
        path = os.path.join(run_dir, "run.json")
        with open(path) as f:
            run = json.load(f)
        run["utilization_coverage"] = {k: round(v, 4) for k, v in sorted(cov.items())}
        # Write-then-rename: run.json is the cell's canonical provenance record
        # and this is the one place that rewrites it in place. Truncating it and
        # dying mid-write (disk full is the realistic trigger) would destroy the
        # manifest of a cell whose measurements are already complete.
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(run, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("report", help="print the §3 utilization numbers per cell")
    r.add_argument("run_dirs", nargs="+")
    c = sub.add_parser("coverage", help="window coverage per utilization series")
    c.add_argument("run_dir")
    c.add_argument("--update-run-json", action="store_true",
                   help="record the fractions in the cell's run.json")
    a = p.parse_args()
    if a.cmd == "report":
        return cmd_report(a.run_dirs)
    return cmd_coverage(a.run_dir, a.update_run_json)


if __name__ == "__main__":
    sys.exit(main())

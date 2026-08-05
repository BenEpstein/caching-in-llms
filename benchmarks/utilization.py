"""Utilization metrics (§3): GPU, memory, CPU - read from the per-cell collectors.

§3 requires memory and CPU/GPU utilization alongside latency, hit rate and
throughput. Everything below was already being collected per cell and read by
nothing; this module is what reads it.

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

# Prometheus series this module reads, and the job whose samples count.
# The job filter is not cosmetic: the router re-exports vllm:* per backend under
# one shared `instance`, so dropping it merges both engines into one synthetic
# series (the same trap export_summary.per_seed_imbalance documents).
ENGINE_GAUGES = {
    "vllm_kv_cache_usage_perc": ("vllm-engines", "GPU memory (KV cache) in use, fraction"),
    "lmcache_local_cache_usage": ("vllm-engines", "LMCache host RAM held, bytes"),
}
ROUTER_GAUGES = {
    "router_cpu_usage_percent": ("router", "router CPU, percent"),
    "router_memory_usage_percent": ("router", "router memory, percent"),
    "process_resident_memory_bytes": ("router", "router RSS, bytes"),
}
ROUTER_COUNTERS = {
    "process_cpu_seconds_total": ("router", "router CPU, core-seconds per second"),
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
    """Per-second rate of a monotonic counter across the dumped window.

    First-to-last, not an average of instantaneous rates: the series is a
    counter sampled every 5 s, so the endpoints ARE the total and this is exact
    rather than an approximation. Resets would break it, but a router process
    that restarts mid-cell invalidates the cell for other reasons first.
    """
    if len(pts) < 2:
        return float("nan")
    dt = pts[-1][0] - pts[0][0]
    return (pts[-1][1] - pts[0][1]) / dt if dt > 0 else float("nan")


def window(run_dir: str) -> Optional[Tuple[float, float]]:
    """The cell's measured window from run.json, or None when unrecorded."""
    path = os.path.join(run_dir, "run.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        w = json.load(f).get("window") or {}
    if "start_ts" in w and "end_ts" in w and w["end_ts"] > w["start_ts"]:
        return float(w["start_ts"]), float(w["end_ts"])
    return None


def _span_coverage(pts: Samples, start: float, end: float) -> float:
    """Fraction of [start, end] spanned by the samples, clipped to the window.

    Deliberately measures the SPAN, not sample density. The failure this exists
    to catch is a tail truncation - a collector that dies partway and leaves the
    end of the cell unmeasured - and a span catches that in one number. Internal
    gaps are a different failure and would need a different check.
    """
    inside = [t for t, _ in pts if start <= t <= end]
    if len(inside) < 2:
        return 0.0
    return (max(inside) - min(inside)) / (end - start)


def coverage(run_dir: str) -> Dict[str, float]:
    """Covered fraction of the measured window, per utilization series.

    Empty when the cell records no window - there is nothing to compare against,
    and inventing one from file mtimes would report a number that means nothing.
    """
    win = window(run_dir)
    if not win:
        return {}
    start, end = win
    out: Dict[str, float] = {}
    for metric, (job, _) in {**ENGINE_GAUGES, **ROUTER_GAUGES, **ROUTER_COUNTERS}.items():
        series = read_series(run_dir, metric, job)
        if series:
            # worst series, not the mean: one engine going dark is the failure,
            # and averaging it against a healthy one hides exactly that.
            out[metric] = min(_span_coverage(p, start, end) for p in series.values())
    dcgm = read_dcgm(run_dir)
    for field, per_gpu in dcgm.items():
        if per_gpu:
            out[field] = min(_span_coverage(p, start, end) for p in per_gpu.values())
    return out


def cell_utilization(run_dir: str) -> Dict:
    """Every §3 utilization number for one cell, plus what is not available."""
    out: Dict = {
        "cell": os.path.basename(run_dir.rstrip("/")),
        "engines": {},
        "router": {},
        "gpu": {},
        "coverage": coverage(run_dir),
        # Stated, not silently omitted: vLLM registers no process collector, so
        # engine host-CPU and engine RSS cannot be reported from this deployment.
        "unavailable": ["engine process_cpu_seconds_total", "engine process_resident_memory_bytes"],
    }
    for metric, (job, _) in ENGINE_GAUGES.items():
        per_pod = read_series(run_dir, metric, job)
        if per_pod:
            out["engines"][metric] = {pod: gauge_stats(p) for pod, p in per_pod.items()}
    for metric, (job, _) in ROUTER_GAUGES.items():
        per_pod = read_series(run_dir, metric, job)
        for p in per_pod.values():
            out["router"][metric] = gauge_stats(p)
    for metric, (job, _) in ROUTER_COUNTERS.items():
        per_pod = read_series(run_dir, metric, job)
        for p in per_pod.values():
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
                f"{pod.split('-')[-1]}={v['mean']:.3f} (max {v['max']:.3f})"
                for pod, v in sorted(kv.items())
            )
            if sp and sp[0] > 0:
                line += f"   spread {sp[1] / sp[0]:.2f}x"
            print(line)
        ram = u["engines"].get("lmcache_local_cache_usage")
        if ram:
            print("  LMCache host RAM:       " + "  ".join(
                f"{pod.split('-')[-1]}={v['mean'] / 1e9:.2f} GB" for pod, v in sorted(ram.items())))
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
        low = {k: v for k, v in u["coverage"].items() if v < MIN_COVERAGE}
        if low:
            print("  ⚠ coverage below "
                  f"{MIN_COVERAGE:.0%}: " + ", ".join(f"{k} {v:.0%}" for k, v in sorted(low.items())))
        if not u["gpu"]:
            print("  note: no dcgm.csv - GPU utilization (SM%, power) unavailable for this cell")
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
        with open(path, "w") as f:
            json.dump(run, f, indent=2)
            f.write("\n")
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

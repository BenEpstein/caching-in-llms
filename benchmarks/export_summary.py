"""Export per-seed results to one committed CSV.

`results/` is gitignored (driver CSVs and Prometheus dumps are megabytes), but
every number in the report has to be checkable by a reader who cannot rerun the
cluster. This writes the derived per-seed table - a few KB - which is committed
alongside each cell's `run.json`. Between them a reader can reproduce every
figure, every percentile, and both co-primary tests without the raw data.

Per-seed load imbalance is computed here, not in analyze.py, because it needs
the Prometheus dump windowed by each seed's driver CSV `send_ts` range.

Usage:
  python3 export_summary.py results/<run>... --out results/summary-per-seed.csv
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
from typing import Dict, List

from analyze import read_run

FIELDS = [
    # `run` FIRST and part of the sort key: `cell` alone is ambiguous - the same
    # cell name appears in the 7.5 req/s pilot and the 10.5 req/s amended sweep,
    # and grouping by it silently merges two different experiments.
    "run", "cell", "arm", "alpha", "beta", "rate_req_s", "git_commit", "router_image",
    "seed", "ok", "errors", "error_rate",
    "ttft_mean", "ttft_p50", "ttft_p95", "ttft_p99",
    "e2e_mean", "e2e_p95", "e2e_p99",
    "throughput_req_s", "throughput_tok_s", "imbalance",
]


def per_seed_imbalance(run_dir: str) -> Dict[int, float]:
    """busiest/idlest engine mean in-flight, per seed window. {} if no dump."""
    path = os.path.join(run_dir, "prom", "vllm_num_requests_running.json")
    if not os.path.exists(path):
        return {}
    series: Dict[str, List] = {}
    for s in json.load(open(path))["data"]["result"]:
        pod = s["metric"].get("pod") or s["metric"].get("instance", "?")
        series.setdefault(pod, []).extend(
            (float(t), float(v)) for t, v in s["values"]
            if v not in ("NaN", "+Inf", "-Inf"))

    out = {}
    for p in sorted(glob.glob(os.path.join(run_dir, "driver-seed*.csv"))):
        ts = [float(r["send_ts"]) for r in csv.DictReader(open(p))]
        if not ts:
            continue
        lo, hi = min(ts), max(ts)
        means = sorted(
            sum(v for t, v in vals if lo <= t <= hi) / max(1, len([1 for t, _ in vals if lo <= t <= hi]))
            for vals in series.values()
            if any(lo <= t <= hi for t, _ in vals))
        if len(means) >= 2 and means[0] > 0:
            seed = int("".join(c for c in os.path.basename(p) if c.isdigit()))
            out[seed] = means[-1] / means[0]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--out", default="results/summary-per-seed.csv")
    a = ap.parse_args()

    rows = []
    for run_dir in a.runs:
        manifest_path = os.path.join(run_dir, "run.json")
        if not os.path.exists(manifest_path):
            print(f"skip {run_dir}: no run.json")
            continue
        run = json.load(open(manifest_path))
        imb = per_seed_imbalance(run_dir)
        for s in read_run(run_dir):
            seed = int("".join(c for c in s["file"] if c.isdigit()))
            rows.append({
                "run": os.path.basename(run_dir.rstrip("/")),
                "cell": run["cell"], "arm": run["arm"],
                "alpha": run.get("alpha"), "beta": run.get("beta"),
                "rate_req_s": run["rate_req_s"],
                "git_commit": run["git_commit"][:12],
                "router_image": run["router_image"],
                "seed": seed, "ok": s["ok"], "errors": s["errors"],
                "error_rate": round(s["error_rate"], 5),
                **{k: round(s[k], 4) for k in (
                    "ttft_mean", "ttft_p50", "ttft_p95", "ttft_p99",
                    "e2e_mean", "e2e_p95", "e2e_p99",
                    "throughput_req_s", "throughput_tok_s")},
                "imbalance": round(imb[seed], 4) if seed in imb else "",
            })

    rows.sort(key=lambda r: (r["run"], r["seed"]))
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} seed rows from {len(a.runs)} cells -> {a.out}")


if __name__ == "__main__":
    main()

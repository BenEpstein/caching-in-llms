"""Export per-seed results to one committed CSV.

`results/` is gitignored (driver CSVs and Prometheus dumps are megabytes), but
every number in the report has to be checkable by a reader who cannot rerun the
cluster. This writes the derived per-seed table - a few KB - which is committed
alongside each cell's `run.json`. Between them a reader can reproduce every
figure, every percentile, and both co-primary tests without the raw data.

Latency columns (`ttft_*`, `e2e_*`, `itl_*`) are all in SECONDS - including the
itl ones, whose source column is named `itls_ms`. See the UNITS note in
analyze.py.

Usage:
  python3 export_summary.py results/<sweep>/<run>... --out results/<sweep>/summary-per-seed.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import os

from analyze import per_seed_imbalance, read_run

FIELDS = [
    # `run` FIRST and part of the sort key: `cell` alone is ambiguous - the same
    # cell name recurs across experiments run at different rates, and grouping by
    # it silently merges them.
    # "alpha" is retained for the runs recorded before it was removed from the
    # policy. Cells run since write it empty.
    #
    # *** `beta` MEANS TWO DIFFERENT THINGS IN THIS TABLE - CHECK git_commit ***
    #   before 7e2dffb: beta * absolute in-flight count
    #   7e2dffb onward: beta * (load - fleet_mean)/max(1, mean)
    # Not comparable and not convertible; only beta=0 means the same thing on
    # both sides. Never pool or compare across that boundary without saying which
    # side each cell is on. `git_commit` in this table is what says which side.
    # `sweep_id` travels in the row because the one-table-per-sweep layout separates batches
    # only until someone concatenates two tables. Why cells from different sweeps must never be
    # paired: analyze.check_comparable. Empty for run dirs that predate the field.
    "run", "sweep_id",
    "cell", "arm", "alpha", "beta", "rate_req_s", "osl_tokens", "git_commit", "router_image",
    "seed", "ok", "errors", "error_rate",
    "ttft_mean", "ttft_p50", "ttft_p90", "ttft_p95", "ttft_p99",
    "itl_p50", "itl_p95", "itl_p99",
    "e2e_mean", "e2e_p95", "e2e_p99",
    "throughput_req_s", "throughput_tok_s", "imbalance",
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--out", required=True, help="per-sweep CSV, e.g. results/<sweep>/summary-per-seed.csv")
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
            seed = s["seed"]
            rows.append({
                "run": os.path.basename(run_dir.rstrip("/")),
                "sweep_id": run.get("sweep_id", ""),
                "cell": run["cell"], "arm": run["arm"],
                "alpha": run.get("alpha"), "beta": run.get("beta"),
                "osl_tokens": run.get("osl_tokens"),
                "rate_req_s": run["rate_req_s"],
                "git_commit": run["git_commit"][:12],
                "router_image": run["router_image"],
                "seed": seed, "ok": s["ok"], "errors": s["errors"],
                "error_rate": round(s["error_rate"], 5),
                **{k: round(s[k], 4) for k in (
                    "ttft_mean", "ttft_p50", "ttft_p90", "ttft_p95", "ttft_p99",
                    "itl_p50", "itl_p95", "itl_p99",
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

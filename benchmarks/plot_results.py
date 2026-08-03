"""Figures for §5 from a completed sweep.

Reads the same run dirs analyze.py does and reuses its per-seed stats, so a
figure can never disagree with the table it sits next to.

  fig1-ttft-p95-vs-beta.png   centerpiece: TTFT p95 vs beta, baselines as bands
  fig2-ttft-ecdf.png          client-observed TTFT distribution per arm
  fig3-hit-rate.png           LMCache lookup hit rate over the measured window

Usage:
  python3 plot_results.py results/<...>-loadaware-b0 results/<...>-kvaware ... \
      --out docs/figures
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Dict, List

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from analyze import percentile, read_run  # noqa: E402

BASELINE_STYLE = {"kvaware": ("tab:red", "-"), "roundrobin": ("tab:gray", "--")}


def load(run_dir: str) -> Dict:
    """Run manifest + per-seed stats for one cell."""
    run = json.load(open(os.path.join(run_dir, "run.json")))
    return {
        "dir": run_dir,
        "cell": run["cell"],
        "arm": run["arm"],
        "beta": float(run["beta"]) if run["beta"] is not None else None,
        "rate": run["rate_req_s"],
        "seeds": read_run(run_dir),
    }


def ttft_p95s(cell: Dict) -> List[float]:
    return [s["ttft_p95"] for s in cell["seeds"]]


def median(xs: List[float]) -> float:
    return percentile(xs, 50)


def fig_p95_vs_beta(cells: List[Dict], out: str) -> None:
    """The centerpiece: does beta buy anything, and where is the minimum?

    Per-seed points are drawn behind the median line - with n=6 the spread is
    the honest part of the story, so it does not get hidden.
    """
    la = sorted([c for c in cells if c["arm"] == "loadaware"], key=lambda c: c["beta"])
    if not la:
        return
    betas = [c["beta"] for c in la]
    meds = [median(ttft_p95s(c)) for c in la]

    fig, ax = plt.subplots(figsize=(7, 4.5))
    for c in la:
        ax.scatter([c["beta"]] * len(c["seeds"]), ttft_p95s(c),
                   color="tab:blue", alpha=0.35, s=22, zorder=2)
    ax.plot(betas, meds, "o-", color="tab:blue", lw=2, zorder=3,
            label="loadaware (median of 6 seeds)")

    for c in cells:
        style = BASELINE_STYLE.get(c["arm"])
        if not style:
            continue
        color, ls = style
        m = median(ttft_p95s(c))
        ax.axhline(m, color=color, ls=ls, lw=1.6, zorder=1, label=f"{c['arm']} (median)")
        lo, hi = min(ttft_p95s(c)), max(ttft_p95s(c))
        ax.axhspan(lo, hi, color=color, alpha=0.08, zorder=0)

    ax.set_xlabel(r"$\beta$  (load weight; $\alpha$ fixed at 1.0)")
    ax.set_ylabel("TTFT p95 (s)")
    ax.set_title(f"TTFT p95 vs load weight - {cells[0]['rate']} req/s, "
                 "6 seeds x 500 requests per cell")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)


def fig_ecdf(cells: List[Dict], out: str) -> None:
    """Where in the distribution the difference actually lives."""
    import csv
    import glob

    fig, ax = plt.subplots(figsize=(7, 4.5))
    for c in sorted(cells, key=lambda c: (c["arm"], c["beta"] or 0)):
        ttft: List[float] = []
        for path in sorted(glob.glob(os.path.join(c["dir"], "driver-seed*.csv"))):
            for r in csv.DictReader(open(path)):
                if r["status"] == "ok" and r["ttft_s"]:
                    ttft.append(float(r["ttft_s"]))
        if not ttft:
            continue
        ttft.sort()
        ys = [(i + 1) / len(ttft) for i in range(len(ttft))]
        ax.plot(ttft, ys, lw=1.4, label=f"{c['cell']} (n={len(ttft)})")

    ax.set_xlabel("TTFT (s)")
    ax.set_ylabel("fraction of requests")
    ax.set_xlim(0, 1.0)
    ax.set_ylim(0.5, 1.0)  # the tail is the story; p50 is flat across arms
    ax.set_title("Client-observed TTFT distribution (upper half)")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)


def fig_hit_rate(cells: List[Dict], out: str) -> None:
    """LMCache lookup hit rate per arm, averaged over engines and the window.

    NOTE (verified 2026-08-03): `lmcache:lookup_hit_rate` is scraped from the
    ENGINES (job=vllm-engines), not the router - it is each engine's hit rate
    against its OWN local cache, not "did the router pick the instance holding
    the KV". With a 20-prefix pool both engines see every prefix repeatedly, so
    it saturates near 0.95 on every arm INCLUDING roundrobin. This figure
    therefore documents that hit rate does not discriminate the policies at this
    workload; TTFT is the discriminating metric. Do not read it as evidence
    either way about routing quality.
    """
    labels, values = [], []
    for c in sorted(cells, key=lambda c: (c["arm"], c["beta"] or 0)):
        path = os.path.join(c["dir"], "prom", "lmcache_lookup_hit_rate.json")
        if not os.path.exists(path):
            continue
        series = json.load(open(path)).get("data", {}).get("result", [])
        samples = [float(v[1]) for s in series for v in s.get("values", [])
                   if v[1] not in ("NaN", "+Inf", "-Inf")]
        if not samples:
            continue
        labels.append(c["cell"])
        values.append(sum(samples) / len(samples))

    if not labels:
        return
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.bar(labels, values, color="tab:blue", alpha=0.8)
    ax.set_ylabel("engine-local LMCache lookup hit rate (window mean)")
    ax.set_title("Engine-local cache hit rate by arm - saturated, does not\n"
                 "discriminate routing policy (see docstring)")
    ax.set_ylim(0, 1.05)
    ax.tick_params(axis="x", rotation=20)
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--out", default="docs/figures")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    cells = [load(r) for r in args.runs]

    rates = {c["rate"] for c in cells}
    if len(rates) > 1:
        raise SystemExit(f"cells span multiple rates {rates} - not one experiment")

    fig_p95_vs_beta(cells, os.path.join(args.out, "fig1-ttft-p95-vs-beta.png"))
    fig_ecdf(cells, os.path.join(args.out, "fig2-ttft-ecdf.png"))
    fig_hit_rate(cells, os.path.join(args.out, "fig3-hit-rate.png"))
    print(f"wrote figures to {args.out}")


if __name__ == "__main__":
    main()

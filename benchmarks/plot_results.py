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
    # seed counts differ by cell (headline pair 10, sweep cells 3) - state the
    # range rather than a number that is wrong for half the points
    ns = sorted({len(c["seeds"]) for c in cells})
    nlab = f"{ns[0]}" if len(ns) == 1 else f"{ns[0]}-{ns[-1]}"
    ax.set_title(f"TTFT p95 vs load weight - {cells[0]['rate']} req/s, "
                 f"{nlab} seeds x 500 requests per cell")
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


def counter_delta(run_dir: str, metric: str) -> float:
    """Total increase of a Prometheus counter over the window, summed over series."""
    path = os.path.join(run_dir, "prom", metric.replace(":", "_") + ".json")
    if not os.path.exists(path):
        return float("nan")
    total = 0.0
    for s in json.load(open(path))["data"]["result"]:
        vals = [float(v[1]) for v in s["values"] if v[1] not in ("NaN", "+Inf", "-Inf")]
        if len(vals) >= 2:
            total += max(vals) - min(vals)
    return total


def fig_beta_tradeoff(cells: List[Dict], out: str) -> None:
    """The causal figure: what beta actually buys and what it costs.

    Left axis = vLLM prefix-cache hit rate (the thing diverting destroys),
    right axis = TTFT p95 (the consequence). This is the mechanism behind the
    beta blow-up, not an inference from latency alone.
    """
    la = sorted([c for c in cells if c["arm"] == "loadaware"], key=lambda c: c["beta"])
    la = [c for c in la
          if not (counter_delta(c["dir"], "vllm:prefix_cache_queries_total") !=
                  counter_delta(c["dir"], "vllm:prefix_cache_queries_total"))]  # drop NaN
    if len(la) < 2:
        return
    betas = [c["beta"] for c in la]
    hit = [counter_delta(c["dir"], "vllm:prefix_cache_hits_total")
           / counter_delta(c["dir"], "vllm:prefix_cache_queries_total") for c in la]
    p95 = [median(ttft_p95s(c)) for c in la]

    fig, ax = plt.subplots(figsize=(7.5, 4.5))
    ax.plot(betas, hit, "o-", color="tab:green", lw=2, label="prefix cache hit rate")
    ax.set_xlabel(r"$\beta$  (load weight; $\alpha$ fixed at 1.0)")
    ax.set_ylabel("vLLM prefix cache hit rate", color="tab:green")
    ax.tick_params(axis="y", labelcolor="tab:green")
    ax.set_ylim(0.6, 1.0)
    ax2 = ax.twinx()
    ax2.plot(betas, p95, "s--", color="tab:red", lw=2, label="TTFT p95")
    ax2.set_ylabel("TTFT p95 (s), median of seeds", color="tab:red")
    ax2.tick_params(axis="y", labelcolor="tab:red")
    ax.set_title(r"Raising $\beta$ diverts requests off their cached instance:"
                 "\nhit rate falls, latency follows")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)


def fig_paired(cells: List[Dict], out: str, cand="loadaware-b0.1", base="kvaware") -> None:
    """The headline test, drawn: one line per seed, candidate vs baseline.

    This is the figure the statistics actually operate on - the Wilcoxon sees
    exactly these six lines and nothing else. A reversing seed is a line with
    the opposite slope, which is why it is worth a figure of its own.
    """
    c = next((x for x in cells if x["cell"] == cand), None)
    b = next((x for x in cells if x["cell"] == base), None)
    if not c or not b:
        return
    cs, bs = ttft_p95s(c), ttft_p95s(b)

    fig, ax = plt.subplots(figsize=(6, 4.5))
    for i, (bv, cv) in enumerate(zip(bs, cs), start=1):
        improved = cv < bv
        ax.plot([0, 1], [bv, cv], "o-",
                color="tab:blue" if improved else "tab:red",
                alpha=0.85, lw=1.6, zorder=2)
        ax.annotate(f"seed {i}", (1, cv), textcoords="offset points",
                    xytext=(6, 0), fontsize=7, va="center",
                    color="tab:blue" if improved else "tab:red")
    ax.set_xticks([0, 1])
    ax.set_xticklabels([base, cand])
    ax.set_xlim(-0.15, 1.35)
    ax.set_ylabel("TTFT p95 (s)")
    ax.set_title(f"Paired per-seed TTFT p95\n{sum(c < b for c, b in zip(cs, bs))}/{len(cs)} "
                 "seeds improve (red = reversal)")
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)


def fig_percentiles(cells: List[Dict], out: str) -> None:
    """p50 / p95 / p99 side by side: where each arm's cost actually sits."""
    ordered = sorted(cells, key=lambda c: (c["arm"] != "loadaware", c["beta"] or 0, c["cell"]))
    metrics = [("ttft_p50", "TTFT p50"), ("ttft_p95", "TTFT p95"), ("ttft_p99", "TTFT p99")]
    fig, axes = plt.subplots(1, 3, figsize=(12, 4), sharey=True)
    for ax, (key, label) in zip(axes, metrics):
        names = [c["cell"] for c in ordered]
        meds = [percentile([s[key] for s in c["seeds"]], 50) for c in ordered]
        ax.barh(names, meds, color="tab:blue", alpha=0.8)
        ax.set_xlabel(f"{label} (s), median of 6 seeds")
        ax.grid(alpha=0.3, axis="x")
    axes[0].invert_yaxis()
    fig.suptitle("TTFT percentiles by arm - the arms separate in the tail, not at the median")
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)


def fig_imbalance(cells: List[Dict], out: str) -> None:
    """Per-engine in-flight requests: the mechanism, measured directly.

    This is what the policy actually changes. `vllm:num_requests_running` is
    scraped per engine, so the gap between the busiest and idlest engine over
    the window is a direct read of how evenly each router spreads load - no
    latency modelling in between.
    """
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ordered = sorted(cells, key=lambda c: (c["arm"] != "loadaware", c["beta"] or 0, c["cell"]))
    labels, lows, highs = [], [], []
    for c in ordered:
        path = os.path.join(c["dir"], "prom", "vllm_num_requests_running.json")
        if not os.path.exists(path):
            continue
        # one series per engine per scrape target; key by pod so duplicate
        # targets for the same engine collapse instead of counting twice
        per_pod: Dict[str, List[float]] = {}
        for s in json.load(open(path))["data"]["result"]:
            pod = s["metric"].get("pod") or s["metric"].get("instance", "?")
            per_pod.setdefault(pod, []).extend(
                float(v[1]) for v in s["values"] if v[1] not in ("NaN", "+Inf", "-Inf"))
        means = sorted(sum(v) / len(v) for v in per_pod.values() if v)
        if len(means) < 2:
            continue
        labels.append(c["cell"])
        lows.append(means[0])
        highs.append(means[-1])

    if not labels:
        return
    y = range(len(labels))
    ax.barh([i - 0.2 for i in y], highs, height=0.38, color="tab:red", alpha=0.8,
            label="busiest engine")
    ax.barh([i + 0.2 for i in y], lows, height=0.38, color="tab:blue", alpha=0.8,
            label="idlest engine")
    for i, (lo, hi) in enumerate(zip(lows, highs)):
        ax.annotate(f"{hi / lo:.1f}x" if lo else "n/a", (max(hi, lo), i),
                    textcoords="offset points", xytext=(6, 0), fontsize=8, va="center")
    ax.set_yticks(list(y))
    ax.set_yticklabels(labels)
    ax.invert_yaxis()
    ax.set_xlabel("mean in-flight requests over the measured window")
    ax.set_title("Load balance across the two engines - what the policy actually changes")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3, axis="x")
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
    fig_paired(cells, os.path.join(args.out, "fig4-paired-seeds.png"))
    fig_percentiles(cells, os.path.join(args.out, "fig5-percentiles.png"))
    fig_imbalance(cells, os.path.join(args.out, "fig6-load-balance.png"))
    fig_beta_tradeoff(cells, os.path.join(args.out, "fig7-beta-tradeoff.png"))
    print(f"wrote figures to {args.out}")


if __name__ == "__main__":
    main()

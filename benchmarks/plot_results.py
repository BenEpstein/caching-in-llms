"""Figures for §5 from a completed sweep.

Reads the same run dirs analyze.py does and reuses its per-seed stats, so a
figure can never disagree with the table it sits next to.

  fig1-ttft-p95-vs-beta.png   centerpiece: TTFT p95 vs beta, baselines as bands
  fig2-ttft-ecdf.png          client-observed TTFT distribution per arm
  fig3-hit-rate.png           LMCache lookup hit rate over the measured window
  fig4-paired-seeds.png       the paired per-seed lines the Wilcoxon actually sees
  fig5-percentiles.png        TTFT percentile panels per arm
  fig6-load-balance.png       busiest vs idlest engine - what the policy changes
  fig7-beta-tradeoff.png      hit rate against latency across the beta grid
  fig8-itl-percentiles.png    inter-token latency p50/p95/p99 - the decode side
  fig9-throughput.png         sustained tok/s and req/s per arm
  fig10-utilization.png       §3 utilization: GPU, GPU memory, CPU, host memory

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

import utilization  # noqa: E402
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
    # Derived, never hardcoded. This read "median of 6 seeds" through the whole
    # 20-seed confirmatory sweep, contradicting the title on the same figure -
    # the same defect _panel_grid documents fixing for the bar panels, missed
    # here. If the loadaware cells ever carry different n, say so rather than
    # quietly picking one.
    ns = {len(c["seeds"]) for c in la}
    n_label = f"{ns.pop()} seeds" if len(ns) == 1 else "mixed n - see fig5"
    ax.plot(betas, meds, "o-", color="tab:blue", lw=2, zorder=3,
            label=f"loadaware (median of {n_label})")

    for c in cells:
        style = BASELINE_STYLE.get(c["arm"])
        if not style:
            continue
        color, ls = style
        m = median(ttft_p95s(c))
        ax.axhline(m, color=color, ls=ls, lw=1.6, zorder=1, label=f"{c['arm']} (median)")
        lo, hi = min(ttft_p95s(c)), max(ttft_p95s(c))
        ax.axhspan(lo, hi, color=color, alpha=0.08, zorder=0)

    ax.set_xlabel(r"$\beta$  (load weight; full cache hits per 100% above fleet-mean load)")
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
    """What beta costs in cache locality, and whether latency follows.

    Left axis = vLLM prefix-cache hit rate (the thing diverting is supposed to
    destroy), right axis = TTFT p95 (the putative consequence).

    The title used to assert "hit rate falls, latency follows". That was read off
    the 10.5 req/s sweep, where beta ran to 1.0 and hit rate genuinely collapsed
    0.918 -> 0.787 -> 0.735. It is FALSE on the rate-16 grid: across beta 0 ->
    0.034 -> 0.068 hit rate is flat (0.911, 0.895, 0.916), and fig3 agrees -
    every cache-aware arm sits at ~0.96 engine-local lookup hit rate. So any TTFT
    movement in this beta range is NOT bought with cache misses, and the figure
    must not claim a mechanism its own data refutes. Title now states what is
    plotted and leaves the reading to the caption.
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
    ax.set_xlabel(r"$\beta$  (load weight; full cache hits per 100% above fleet-mean load)")
    ax.set_ylabel("vLLM prefix cache hit rate", color="tab:green")
    ax.tick_params(axis="y", labelcolor="tab:green")
    ax.set_ylim(0.6, 1.0)
    ax2 = ax.twinx()
    ax2.plot(betas, p95, "s--", color="tab:red", lw=2, label="TTFT p95")
    ax2.set_ylabel("TTFT p95 (s), median of seeds", color="tab:red")
    ax2.tick_params(axis="y", labelcolor="tab:red")
    # Derived, not asserted. This subtitle hardcoded "hit rate is flat over this
    # range - diverting costs no locality here", a conclusion true of an earlier
    # grid and false of the 2026-08-06 confirmatory sweep, where hit rate falls
    # 91.2% -> 86.1% monotonically in beta and that decline IS the mechanism
    # behind the beta=2.0 latency reversal. A figure must not caption away the
    # effect it is plotting.
    drop_pts = (hit[0] - hit[-1]) * 100
    note = ("hit rate is flat over this range - diverting costs no locality here"
            if abs(drop_pts) < 2.0 else
            f"hit rate falls {drop_pts:.1f} pts across the grid"
            " - diverting load costs cache locality")
    ax.set_title(r"Cache hit rate and TTFT p95 vs $\beta$" "\n" + note)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)


def fig_paired(cells: List[Dict], out: str, cand: str, base: str = "kvaware") -> None:
    """The headline test, drawn: one line per seed, candidate vs baseline.

    This is the figure the statistics actually operate on - the Wilcoxon sees
    exactly these lines and nothing else. A reversing seed is a line with the
    opposite slope, which is why it is worth a figure of its own.

    `cand` is REQUIRED. It first defaulted to the literal "loadaware-b0.1" and returned
    silently when that cell was absent; that was replaced by an inference accepting exactly
    one non-b0 loadaware cell, which the standard grid (BETA_GRID="0 0.5 1.0 2.0") never
    satisfies - it always yields three, so the default path could not fire at all (#30).

    Both failures were the same shape: an interface that looks like it has a working default.
    Naming the headline cell is also the honest contract - which arm is the headline is a
    pre-registration decision, not something a plotting script should guess from whichever
    directories it happened to be handed.
    """
    c = next((x for x in cells if x["cell"] == cand), None)
    b = next((x for x in cells if x["cell"] == base), None)
    if not c or not b:
        raise SystemExit(
            f"fig_paired: missing cell(s) - candidate '{cand}' "
            f"{'found' if c else 'MISSING'}, baseline '{base}' "
            f"{'found' if b else 'MISSING'}. Available: "
            f"{[x['cell'] for x in cells]}"
        )
    cs, bs = ttft_p95s(c), ttft_p95s(b)
    # Label with the carried seed number, never with enumerate() position - see
    # analyze.seed_stats. The two diverged, so this figure named the wrong seed
    # on every point but the first, and a mislabelled figure ships into the report.
    seeds = [s["seed"] for s in c["seeds"]]
    if seeds != [s["seed"] for s in b["seeds"]]:
        raise SystemExit(
            f"fig_paired: arms hold different seeds - candidate {seeds}, "
            f"baseline {[s['seed'] for s in b['seeds']]}; a paired figure is meaningless"
        )

    fig, ax = plt.subplots(figsize=(6, 4.5))
    for sid, bv, cv in zip(seeds, bs, cs):
        improved = cv < bv
        ax.plot([0, 1], [bv, cv], "o-",
                color="tab:blue" if improved else "tab:red",
                alpha=0.85, lw=1.6, zorder=2)

    # De-collide the seed labels. At n=20 the candidate values bunch tightly and a fixed
    # offset stacks several labels on one another - legible only if you already know what it
    # says, which is not a figure. Walk them in value order, push each to a minimum gap, and
    # draw a leader back to its point so a displaced label still names its seed.
    span = max(max(cs), max(bs)) - min(min(cs), min(bs))
    gap = span * 0.038
    placed, prev = [], None
    for sid, bv, cv in sorted(zip(seeds, bs, cs), key=lambda t: t[2]):
        y = cv if prev is None else max(cv, prev + gap)
        placed.append((sid, cv, y, cv < bv))
        prev = y
    for sid, cv, y, improved in placed:
        color = "tab:blue" if improved else "tab:red"
        if abs(y - cv) > gap * 0.25:
            ax.plot([1.0, 1.06], [cv, y], color=color, lw=0.6, alpha=0.55, zorder=1)
        ax.annotate(f"seed {sid}", (1.07, y), fontsize=7, va="center", ha="left",
                    color=color, annotation_clip=False)

    ax.set_xticks([0, 1])
    ax.set_xticklabels([base, cand])
    ax.set_xlim(-0.15, 1.45)
    ax.set_ylabel("TTFT p95 (s)")
    ax.set_title(f"Paired per-seed TTFT p95\n{sum(c < b for c, b in zip(cs, bs))}/{len(cs)} "
                 "seeds improve (red = reversal)")
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)


def _panel_grid(cells: List[Dict], out: str, metrics, scale: float,
                unit: str, suptitle: str) -> None:
    """Median-across-seeds bar panels, one per percentile. Shared by the TTFT,
    ITL and throughput figures so all three read identically.

    Each bar is labelled with its OWN seed count. The axis caption used to read
    "median of {max n} seeds", which on a mixed-n figure claimed the 3-seed
    descriptive cells (beta=0.068, roundrobin) carried the 20 seeds of the
    inferential ones - overstating exactly the bars a reader should trust least.
    """
    ordered = sorted(cells, key=lambda c: (c["arm"] != "loadaware", c["beta"] or 0, c["cell"]))
    fig, axes = plt.subplots(1, len(metrics), figsize=(4 * len(metrics), 4), sharey=True)
    axes = axes if len(metrics) > 1 else [axes]
    for ax, (key, label) in zip(axes, metrics):
        vals = [[s[key] for s in c["seeds"] if s[key] == s[key]] for c in ordered]
        meds = [percentile(v, 50) * scale if v else float("nan") for v in vals]
        labels = [f"{c['cell']}\n(n={len(v)})" for c, v in zip(ordered, vals)]
        ax.barh(labels, meds, color="tab:blue", alpha=0.8)
        # error bar = seed spread, so a reader sees whether a gap is meaningful
        for i, v in enumerate(vals):
            if len(v) > 1:
                lo, hi = percentile(v, 5) * scale, percentile(v, 95) * scale
                ax.plot([lo, hi], [i, i], color="k", lw=1)
        ax.set_xlabel(f"{label} ({unit}), median across seeds")
        ax.grid(alpha=0.3, axis="x")
    axes[0].invert_yaxis()
    fig.suptitle(suptitle)
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)


def fig_percentiles(cells: List[Dict], out: str) -> None:
    """p50 / p90 / p95 / p99 side by side: where each arm's cost actually sits.

    p90 is on this figure because the 2026-08-03 sweep showed the policy shifts
    the whole TTFT body (~7% at p50 and p90) while p95/p99 are dominated by
    bursty engine stalls - reading only p95/p99 hides the effect in noise.
    """
    _panel_grid(
        cells, out,
        [("ttft_p50", "TTFT p50"), ("ttft_p90", "TTFT p90"),
         ("ttft_p95", "TTFT p95"), ("ttft_p99", "TTFT p99")],
        1.0, "s",
        "TTFT percentiles by arm - bars are seed medians, whiskers the p5-p95 seed spread",
    )


def fig_itl(cells: List[Dict], out: str) -> None:
    """Inter-token latency percentiles - the 92% of E2E that TTFT does not cover.

    Pooled over every inter-token gap in the cell. ITL is where engine-side
    load actually lands: a router that overfills one engine grows its decode
    batch, and every sequence on that engine decodes slower.
    """
    _panel_grid(
        cells, out,
        [("itl_p50", "ITL p50"), ("itl_p95", "ITL p95"), ("itl_p99", "ITL p99")],
        1000.0, "ms",
        "Inter-token latency by arm - the decode-side cost of imbalance",
    )


def fig_throughput(cells: List[Dict], out: str) -> None:
    """Sustained throughput per arm - the rubric's throughput metric.

    Under an open loop at a fixed offered rate, all arms are *given* the same
    work; achieved throughput therefore only separates once an arm cannot keep
    up. A flat panel here is itself the finding: the offered rate is below the
    engines' knee and no arm is being stressed.
    """
    _panel_grid(
        cells, out,
        [("throughput_tok_s", "output tokens/s"), ("throughput_req_s", "requests/s")],
        1.0, "per seed",
        "Sustained throughput by arm - flat bars mean the offered rate is below the knee",
    )


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
        # job=vllm-engines only. The router exports the same metric per backend
        # under a single shared `instance`, so including it merges both engines
        # into one synthetic series - see export_summary.per_seed_imbalance.
        # The parse lives in utilization.read_series; this was the third verbatim
        # copy of it in the repo.
        per_pod = utilization.read_series(
            c["dir"], "vllm_num_requests_running", utilization.ENGINE_JOB)
        means = sorted(sum(y for _, y in v) / len(v) for v in per_pod.values() if v)
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


def fig_utilization(cells: List[Dict], out: str) -> None:
    """§3 utilization in one figure: GPU, GPU memory, CPU, host memory.

    Four panels because §3 asks for four different resources and they live on
    different scales; forcing them onto shared axes would make three of them
    unreadable to flatter the fourth.

    The GPU-memory panel is the load-bearing one. KV cache occupancy is the
    resource the policy contends for, and it separates the arms monotonically in
    beta: kvaware spreads 1.70x across the two engines, b0.5 1.18x, b2.0 1.11x.

    SM% is the weaker read and is here because §3 asks for GPU utilization.
    Both arms serve the identical model at the identical offered rate, so the
    GPUs are near-saturated in every cell and SM% mostly cannot discriminate -
    though it is not perfectly flat either (b2.0 came back 72% vs 93%), so the
    panel is drawn without a claim attached to it.

    Router CPU and memory are the extension's overhead, measured: the router is
    the only component the policy changes.
    """
    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    ordered = sorted(cells, key=lambda c: (c["arm"] != "loadaware", c["beta"] or 0, c["cell"]))
    stats = [(c, utilization.cell_utilization(c["dir"])) for c in ordered]
    labels = [c["cell"].split("-", 2)[-1] for c, _ in stats]
    y = list(range(len(stats)))

    # (a) GPU memory: idlest vs busiest engine, the imbalance in KV occupancy
    ax = axes[0][0]
    lows, highs = [], []
    for _, u in stats:
        sp = utilization.spread(u["engines"].get("vllm_kv_cache_usage_perc", {}))
        lows.append(sp[0] if sp else float("nan"))
        highs.append(sp[1] if sp else float("nan"))
    ax.barh([i - 0.2 for i in y], highs, height=0.38, color="tab:red", alpha=0.8,
            label="busiest engine")
    ax.barh([i + 0.2 for i in y], lows, height=0.38, color="tab:blue", alpha=0.8,
            label="idlest engine")
    for i, (lo, hi) in enumerate(zip(lows, highs)):
        if lo and lo == lo:
            ax.annotate(f"{hi / lo:.2f}x", (max(hi, lo), i), textcoords="offset points",
                        xytext=(6, 0), fontsize=8, va="center")
    ax.set_xlabel("mean KV cache occupancy (fraction)")
    ax.set_title("GPU memory - the resource the policy contends for")
    # Headroom for the spread annotations, which sit past the longest bar and
    # otherwise collide with the legend.
    ax.set_xlim(0, max((h for h in highs if h == h), default=1) * 1.55)
    ax.legend(fontsize=8, loc="lower right")

    # (b) GPU SM% from DCGM, one bar per GPU. Power and MEM_COPY_UTIL are in
    # `utilization.py report`, not here - four resources already fill the grid.
    ax = axes[0][1]
    per_cell = [dict(u["gpu"].get("DCGM_FI_DEV_GPU_UTIL", {})) for _, u in stats]
    # GPUs are named from their own labels, never by bar position. The keys are
    # host/index and this cluster is two NODES with one GPU each, so a
    # positional "GPU 0"/"GPU 1" legend names two different nodes' gpu0 - and
    # silently re-binds if a cell's host set differs. Also derived from the data
    # rather than hardcoded to two, so a third GPU cannot be dropped in silence.
    gpu_keys = sorted({k for d in per_cell for k in d})
    sm_cols = [[d.get(k, {}).get("mean", float("nan")) for d in per_cell] for k in gpu_keys]
    width = 0.76 / max(1, len(gpu_keys))
    for j, (key, vals) in enumerate(zip(gpu_keys, sm_cols)):
        offset = (j - (len(gpu_keys) - 1) / 2) * width
        ax.barh([i + offset for i in y], vals, height=width, alpha=0.8,
                label=utilization.short_gpu(key))
    ax.set_xlabel("mean SM utilization (%)")
    ax.set_title("GPU SM utilization (DCGM)")
    if gpu_keys:
        ax.legend(fontsize=8, loc="lower right")

    # (c) router CPU - the extension's cost, if it has one
    ax = axes[1][0]
    cpu = [u["router"].get("process_cpu_seconds_total", {}).get("rate", float("nan"))
           for _, u in stats]
    ax.barh(y, cpu, height=0.6, color="tab:purple", alpha=0.8)
    ax.set_xlabel("router CPU (core-seconds per second)")
    ax.set_title("CPU - routing overhead")

    # (d) host memory: router RSS, plus the cache's own footprint where recorded
    ax = axes[1][1]
    rss = [u["router"].get("process_resident_memory_bytes", {}).get("mean", float("nan")) / 1e9
           for _, u in stats]
    ax.barh([i - 0.2 for i in y], rss, height=0.38, color="tab:brown", alpha=0.8,
            label="router RSS")
    # lmcache:local_cache_usage is scraped only from #35 onward, so cells
    # predating it draw the router bar alone rather than a misleading zero.
    cache = []
    for _, u in stats:
        per_pod = u["engines"].get("lmcache_local_cache_usage", {})
        means = [v["mean"] for v in per_pod.values() if v["n"]]
        cache.append(sum(means) / len(means) / 1e9 if means else float("nan"))
    if any(x == x for x in cache):
        ax.barh([i + 0.2 for i in y], cache, height=0.38, color="tab:cyan", alpha=0.8,
                label="LMCache host RAM (mean per engine)")
    ax.set_xlabel("memory (GB)")
    ax.set_title("Host memory")
    ax.legend(fontsize=8)

    # Missing data must not read as a measured zero. matplotlib draws NaN as
    # nothing at all, so a cell whose collector died renders as a blank row -
    # visually identical to a real zero, and on the CPU panel that is the exact
    # shape of the §5 claim that the extension costs nothing. Label them.
    for ax, rows in ((axes[0][0], [highs]), (axes[0][1], sm_cols),
                     (axes[1][0], [cpu]), (axes[1][1], [rss])):
        for i in y:
            if all(i >= len(r) or r[i] != r[i] for r in rows):
                ax.annotate("no data", (0, i), textcoords="offset points", xytext=(4, 0),
                            fontsize=7, va="center", style="italic", color="tab:red")

    for ax in axes.flat:
        ax.set_yticks(y)
        ax.set_yticklabels(labels, fontsize=8)
        ax.invert_yaxis()
        ax.grid(alpha=0.3, axis="x")
    fig.suptitle("Resource utilization per arm (§3)")
    fig.tight_layout()
    fig.savefig(out, dpi=160)
    plt.close(fig)


def dump_data(cells: List[Dict], out: str) -> None:
    """Write the series behind the figures as JSON, for `scripts/reproduce.sh` to diff.

    Figures are NOT diffed as PNG bytes. Matplotlib output moves with font availability,
    library version and embedded metadata, so a byte-diff would go red on a fresh runner for
    reasons unrelated to our data - and a check that cries wolf gets muted, which is worse
    than no check at all (the same argument that keeps a threshold off the micro-benchmarks).

    What a reader actually needs to trust is the numbers behind each figure, and those are
    exactly what this dumps: the per-seed stats every latency figure is drawn from, plus the
    per-cell aggregates the rest use. Stable across environments, and it fails loudly when a
    number moves.
    """
    payload = []
    for c in sorted(cells, key=lambda c: c["cell"]):
        payload.append({
            "cell": c["cell"],
            "arm": c["arm"],
            "beta": c["beta"],
            "rate": c["rate"],
            "seeds": [
                {k: (round(v, 6) if isinstance(v, float) else v) for k, v in s.items()}
                for s in sorted(c["seeds"], key=lambda s: s["seed"])
            ],
        })
    with open(out, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"wrote figure data for {len(payload)} cells to {out}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--out", default="docs/figures")
    ap.add_argument("--dump-data", default=None,
                    help="also write the series behind the figures as JSON, for "
                         "scripts/reproduce.sh to diff (PNGs are not byte-comparable)")
    ap.add_argument("--cand", required=True,
                    help="headline loadaware cell, e.g. loadaware-b0.5 - the arm the "
                         "pre-registration names as the comparison of record")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    cells = [load(r) for r in args.runs]

    rates = {c["rate"] for c in cells}
    if len(rates) > 1:
        raise SystemExit(f"cells span multiple rates {rates} - not one experiment")

    fig_p95_vs_beta(cells, os.path.join(args.out, "fig1-ttft-p95-vs-beta.png"))
    fig_ecdf(cells, os.path.join(args.out, "fig2-ttft-ecdf.png"))
    fig_hit_rate(cells, os.path.join(args.out, "fig3-hit-rate.png"))
    fig_paired(cells, os.path.join(args.out, "fig4-paired-seeds.png"), cand=args.cand)
    fig_percentiles(cells, os.path.join(args.out, "fig5-percentiles.png"))
    fig_imbalance(cells, os.path.join(args.out, "fig6-load-balance.png"))
    fig_beta_tradeoff(cells, os.path.join(args.out, "fig7-beta-tradeoff.png"))
    fig_itl(cells, os.path.join(args.out, "fig8-itl-percentiles.png"))
    fig_throughput(cells, os.path.join(args.out, "fig9-throughput.png"))
    fig_utilization(cells, os.path.join(args.out, "fig10-utilization.png"))
    if args.dump_data:
        dump_data(cells, args.dump_data)
    print(f"wrote figures to {args.out}")


if __name__ == "__main__":
    main()

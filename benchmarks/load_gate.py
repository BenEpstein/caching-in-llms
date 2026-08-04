"""The LOAD gate: is there load for a load-aware router to be aware of?

The direct analogue of `scarcity_gate.sh`, and it exists for the same reason.
The 2026-08-03 amended sweep passed every validity rule and still could not test
the hypothesis: `vllm:num_requests_waiting` was 0.00 at every scrape on every
engine, so `loadaware`'s beta term had nothing to act on and the arms differed
only by residual cache locality. A run below the knee is not a measurement of a
load-aware policy - it is a measurement of cache locality wearing its name.

Run this on a short kvaware probe cell BEFORE spending a sweep. It answers two
questions the driver CSVs cannot:

  1. Is the busiest engine actually saturated at this rate?
  2. Is the load ASYMMETRIC - i.e. does kvaware's placement still concentrate?

Both are needed. Symmetric saturation is not an opportunity: if both engines are
equally underwater there is nowhere better to send the request.

### Why saturation is NOT tested with `waiting` or preemption (2026-08-04)

The first version of this gate required `num_preemptions_total > 0` or
`waiting p95 > 0`. **That criterion is unsatisfiable on this workload, not
strict**, and the probe proved it: at offered 16 and 18 the busiest engine ran
59 and 100 mean concurrent requests with `waiting` == 0.00, 0 preemptions, and
queue time 0.0 ms/req at every rate including 10.5.

The cause is prefix caching. Concurrent requests sharing a prefix share its KV
blocks, so a request costs ~530 tokens of KV against a 1578-token prompt.
Against a 104,624-token pool, KV usage tops out near 0.70 even at 100 concurrent
- it never exhausts, so nothing is ever preempted or queued. **This system
saturates on COMPUTE, not memory.** vLLM expresses compute saturation by packing
larger decode batches, and every token in a larger batch is slower - so the
symptom is inter-token latency, never a queue counter.

So the gate tests the CONSEQUENCE instead of a proxy for it: does concentration
actually cost latency? That is **stricter** than "a counter is nonzero" - a
counter can fire without harming anyone, whereas measured degradation cannot.

The per-seed windowing is deliberately the same code path as
`export_summary.per_seed_imbalance`: a mean taken over the raw dump would
include warm-up and engine-restart time, diluting mean in-flight toward zero -
and that number feeds the beta calibration directly, so a diluted delta yields a
beta that is too large.

Usage:
  python3 load_gate.py results/probe/<ts>-kvaware [...]
  python3 load_gate.py results/probe/* --alpha 1.0
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import sys
from typing import Dict, List, Optional, Tuple

from analyze import percentile, read_run

#: Busiest/idlest mean in-flight below this and kvaware is self-balancing at
#: this rate - there is no concentration for the load term to undo.
MIN_ASYMMETRY = 1.5

#: Degradation required over the unloaded baseline before concentration counts
#: as costly. Both are client-observed, same driver, same workload - only the
#: rate differs. 1.25 = "at least 25% worse than an idle system".
MIN_DEGRADATION = 1.25

#: Unloaded reference, measured on this cluster at rate 4 with the fixed driver
#: (2026-08-04): TTFT p95 0.161 s, ITL p95 31.3 ms. A cell at the target rate is
#: compared against these; the sweep is only worth running if concentration has
#: pushed latency materially above an idle system's.
BASELINE_TTFT_P95 = 0.161
BASELINE_ITL_P95 = 0.0313


def _engine_series(run_dir: str, metric: str) -> Dict[str, List[Tuple[float, float]]]:
    """{pod: [(ts, value)]} for one metric, engine job only.

    The `job=vllm-engines` filter is not cosmetic: `vllm:num_requests_running`
    is also exported per-backend under `job=router`, where every series shares
    `instance="stack-router-service:80"` and differs only by the `server` label.
    Keying on `pod or instance` collapses those into a synthetic third "engine"
    that averages the real two - which corrupted 17 of 74 committed seed rows
    before it was caught.
    """
    path = os.path.join(run_dir, "prom", f"{metric}.json")
    if not os.path.exists(path):
        return {}
    series: Dict[str, List[Tuple[float, float]]] = {}
    for s in json.load(open(path))["data"]["result"]:
        if s["metric"].get("job") != "vllm-engines":
            continue
        pod = s["metric"].get("pod") or s["metric"].get("instance", "?")
        series.setdefault(pod, []).extend(
            (float(t), float(v))
            for t, v in s["values"]
            if v not in ("NaN", "+Inf", "-Inf")
        )
    return series


def _window(run_dir: str) -> Optional[Tuple[float, float]]:
    """Measured window: first send to last send across the cell's driver CSVs.

    Warm-up and the engine restart precede the first send, so they fall outside
    it by construction.
    """
    ts: List[float] = []
    for p in sorted(glob.glob(os.path.join(run_dir, "driver-seed*.csv"))):
        ts += [float(r["send_ts"]) for r in csv.DictReader(open(p))]
    return (min(ts), max(ts)) if ts else None


def _in_window(vals: List[Tuple[float, float]], lo: float, hi: float) -> List[float]:
    return [v for t, v in vals if lo <= t <= hi]


def gate(run_dir: str) -> Dict:
    """Evaluate the gate for one probe cell."""
    win = _window(run_dir)
    if win is None:
        return {"run": run_dir, "error": "no driver CSVs"}
    lo, hi = win

    running = _engine_series(run_dir, "vllm_num_requests_running")
    waiting = _engine_series(run_dir, "vllm_num_requests_waiting")
    preempt = _engine_series(run_dir, "vllm_num_preemptions_total")
    if not running:
        return {"run": run_dir, "error": "no vllm_num_requests_running dump"}

    means, maxes = {}, {}
    for pod, vals in running.items():
        xs = _in_window(vals, lo, hi)
        if xs:
            means[pod] = sum(xs) / len(xs)
            maxes[pod] = max(xs)
    if len(means) < 2:
        return {"run": run_dir, "error": f"need 2 engines in window, got {len(means)}"}

    ordered = sorted(means, key=lambda p: means[p])
    idlest, busiest = ordered[0], ordered[-1]
    asym = means[busiest] / means[idlest] if means[idlest] > 0 else float("inf")
    delta_load = means[busiest] - means[idlest]

    # waiting: p95 over the busiest engine's samples in-window
    wait_p95 = 0.0
    if busiest in waiting:
        xs = _in_window(waiting[busiest], lo, hi)
        wait_p95 = percentile(xs, 95) if xs else 0.0

    # preemptions: cumulative counter, so take the in-window delta
    preempt_delta = 0.0
    for pod, vals in preempt.items():
        xs = _in_window(vals, lo, hi)
        if xs:
            preempt_delta += max(xs) - min(xs)

    # The consequence test: is client-observed latency materially worse than on
    # an idle system? Needs a driver that records TTFT - a cell measured with the
    # pre-2026-08-04 driver reports NaN here and cannot be gated on (that bug
    # silently produced 500 rows with no TTFT at all).
    seeds = read_run(run_dir)
    ttft_p95 = _median([s["ttft_p95"] for s in seeds])
    itl_p95 = _median([s.get("itl_p95", float("nan")) for s in seeds])
    ttft_ratio = ttft_p95 / BASELINE_TTFT_P95 if ttft_p95 == ttft_p95 else float("nan")
    itl_ratio = itl_p95 / BASELINE_ITL_P95 if itl_p95 == itl_p95 else float("nan")
    measurable = ttft_ratio == ttft_ratio  # not NaN
    degraded = measurable and max(
        ttft_ratio, itl_ratio if itl_ratio == itl_ratio else 0.0
    ) >= MIN_DEGRADATION

    asymmetric = asym >= MIN_ASYMMETRY
    return {
        "run": run_dir,
        "rate": _rate(run_dir),
        "mean_busiest": means[busiest],
        "mean_idlest": means[idlest],
        "max_busiest": maxes[busiest],
        "asymmetry": asym,
        "delta_load": delta_load,
        "waiting_p95": wait_p95,
        "preemptions": preempt_delta,
        "ttft_p95": ttft_p95,
        "itl_p95": itl_p95,
        "ttft_ratio": ttft_ratio,
        "itl_ratio": itl_ratio,
        "measurable": measurable,
        "degraded": degraded,
        "asymmetric": asymmetric,
        "pass": degraded and asymmetric,
    }


def _median(xs):
    xs = sorted(x for x in xs if x == x)  # drop NaN
    return xs[len(xs) // 2] if xs else float("nan")


def _rate(run_dir: str) -> Optional[float]:
    path = os.path.join(run_dir, "run.json")
    if not os.path.exists(path):
        return None
    return json.load(open(path)).get("rate_req_s")


def beta_from(delta_load: float, alpha: float = 1.0, trigger: float = 0.5) -> float:
    """beta such that a diversion triggers at `trigger` of a full cache hit.

    score(i) = alpha*benefit(i) - beta*load(i), benefit in [0, 1]. A request is
    diverted off its holder when the load gap outweighs the benefit gap:

        beta * delta_load  =  alpha * trigger

    `trigger=0.5` means "divert when at least half the prompt's cache benefit is
    at stake", evaluated at the load asymmetry actually measured at this rate.

    This is why beta cannot be carried over from another rate: benefit is
    normalized to [0, 1] but load is a RAW in-flight count, so beta's meaning is
    tied to absolute concurrency (a known limitation - see the report's
    discussion of a relative-load formulation).
    """
    if delta_load <= 0:
        raise ValueError("delta_load must be positive - no asymmetry to calibrate against")
    return alpha * trigger / delta_load


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_dirs", nargs="+")
    ap.add_argument("--alpha", type=float, default=1.0)
    ap.add_argument("--trigger", type=float, default=0.5)
    a = ap.parse_args()

    results = [gate(d) for d in a.run_dirs]
    passing = []
    for r in results:
        print(f"\n== {r['run']}")
        if "error" in r:
            print(f"   ERROR: {r['error']}")
            continue
        print(f"   rate offered      {r['rate']}")
        print(f"   mean in-flight    busiest {r['mean_busiest']:.2f} / idlest {r['mean_idlest']:.2f}"
              f"   (max busiest {r['max_busiest']:.0f})")
        print(f"   asymmetry         {r['asymmetry']:.2f}x   "
              f"{'OK' if r['asymmetric'] else f'FAIL (< {MIN_ASYMMETRY})'}")
        print(f"   delta_load        {r['delta_load']:.2f} requests")
        print(f"   waiting p95       {r['waiting_p95']:.2f}   preemptions {r['preemptions']:.0f}"
              f"   (both structurally 0 here - compute-bound, see module docstring)")
        if not r["measurable"]:
            print("   degradation       UNMEASURABLE - TTFT is NaN, this cell was "
                  "recorded with the broken pre-2026-08-04 driver")
        else:
            print(f"   TTFT p95          {r['ttft_p95']:.3f}s  = {r['ttft_ratio']:.2f}x idle "
                  f"baseline ({BASELINE_TTFT_P95:.3f}s)")
            print(f"   ITL p95           {r['itl_p95'] * 1000:.1f}ms = {r['itl_ratio']:.2f}x idle "
                  f"baseline ({BASELINE_ITL_P95 * 1000:.1f}ms)")
        print(f"   degraded          {'OK' if r['degraded'] else f'FAIL (< {MIN_DEGRADATION}x idle)'}")
        print(f"   GATE              {'PASS' if r['pass'] else 'FAIL'}")
        if r["pass"]:
            print(f"   -> beta           {beta_from(r['delta_load'], a.alpha, a.trigger):.4f}"
                  f"   (alpha={a.alpha}, trigger={a.trigger})")
            passing.append(r)

    if not passing:
        print("\nNO RATE PASSES THE GATE - do not run the sweep; escalate to Plan B "
              "(workload redesign) or probe a higher rate.", file=sys.stderr)
        return 1

    best = min(passing, key=lambda r: r["rate"] if r["rate"] is not None else float("inf"))
    beta = beta_from(best["delta_load"], a.alpha, a.trigger)
    print(f"\n==> lowest passing rate: {best['rate']}  "
          f"(asymmetry {best['asymmetry']:.2f}x, delta_load {best['delta_load']:.2f})")
    print(f"==> BETA_HEADLINE={beta:.3g}  BETA_HIGH={2 * beta:.3g}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

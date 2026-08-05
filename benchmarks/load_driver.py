"""Async load driver: replays a workload against an OpenAI-compatible endpoint.

Measures per request: TTFT (streaming), end-to-end latency, token counts.
Arrival model: open loop, Poisson at --rate req/s. Open loop is tail-latency honest -
queueing delay shows up in the latency it causes instead of throttling the send rate,
which is the whole point when the metric under test is TTFT p95. A closed-loop mode
existed and was never used by any caller; it was removed in #30 rather than left as
an untested second path through the driver.

Output: one CSV row per request. Analysis/plots live elsewhere; this only records.

Usage:
  python3 load_driver.py --base-url http://localhost:8000 --model Qwen/Qwen2.5-3B-Instruct \
      --workload workload.jsonl --rate 4 --max-tokens 64 --out results.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import dataclasses
import json
import random
import time
from typing import List, Optional, Tuple

import httpx

from analyze import percentile

# Unbounded connection pool: past the saturation knee the in-flight count can
# exceed httpx's default 100, and pool-queueing would be silently counted as
# TTFT (t0 starts before client.stream) - distorting the very tail the rate
# pilot measures.
_LIMITS = httpx.Limits(max_connections=None)


@dataclasses.dataclass
class Result:
    index: int
    prefix_id: int
    send_ts: float  # wall-clock epoch seconds - aligns rows with Prometheus/DCGM windows
    ttft_s: Optional[float]
    e2e_s: Optional[float]
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    status: str  # "ok" | "error"
    error: str = ""
    # Inter-token latencies, ';'-joined milliseconds. Decode is ~92% of E2E at
    # OSL=64, so without this the only thing measured about 92% of the request
    # is an aggregate. Kept as raw gaps, not a per-request percentile: pooled
    # ITL percentiles are the reported quantity and cannot be recovered from
    # per-request summaries.
    itls_ms: str = ""


def load_workload(path: str):
    reqs = []
    with open(path) as f:
        for line in f:
            d = json.loads(line)
            if "config" in d:
                continue
            reqs.append(d)
    return reqs


def classify_chunk(data: str) -> Tuple[Optional[dict], bool]:
    """`(usage_or_None, carries_token)` for one SSE `data:` payload.

    Classify by whether the chunk CARRIES A TOKEN, never by whether a key name
    appears in its serialization. `stream_options={"include_usage": True}` makes
    vLLM put a `usage` key on **every** chunk - `null` on token chunks, populated
    only on the final usage-only chunk - so the substring test `'"usage"' in
    data` matches every chunk and classifies nothing.

    That is not hypothetical: it recorded **zero TTFT values across 500
    requests** in the 2026-08-04 gate probe while still populating token counts,
    because the final chunk set those. Every column looked plausible except the
    primary metric. The token test is `choices` being non-empty - the usage-only
    chunk carries `"choices": []`.

    This parses every chunk, which the previous version was avoiding. At ~64
    chunks x ~14 req/s that is ~900 parses/s of ~200-byte payloads - single-digit
    microseconds each, three orders of magnitude below the measured client
    event-loop lag floor (p50 0.6 ms, printed per seed). Correctness of the
    primary metric outranks that optimization.
    """
    obj = json.loads(data)
    return obj.get("usage"), bool(obj.get("choices"))


async def one_request(
    client: httpx.AsyncClient, base_url: str, model: str, req: dict, max_tokens: int
) -> Result:
    payload = {
        "model": model,
        "prompt": req["prompt"],
        "max_tokens": max_tokens,
        # pin the output length (methodology: OSL is fixed) - without this,
        # greedy decoding can hit EOS early and output-length variance leaks
        # into E2E latency and throughput
        "ignore_eos": True,
        "temperature": 0.0,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    t0 = time.perf_counter()
    wall0 = time.time()
    ttft = None
    last = None
    itls: List[float] = []
    prompt_toks = completion_toks = None
    try:
        async with client.stream(
            "POST", f"{base_url}/v1/completions", json=payload, timeout=300.0
        ) as resp:
            if resp.status_code >= 400:
                # streamed responses arrive body-unread, so raise_for_status()
                # would throw away the server's error detail - the router's
                # traceback summary lives in this body (#21)
                body = (await resp.aread()).decode("utf-8", "replace")
                raise RuntimeError(f"HTTP {resp.status_code}: {body.strip()}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                now = time.perf_counter()
                usage, carries_token = classify_chunk(data)
                if usage:
                    prompt_toks = usage.get("prompt_tokens")
                    completion_toks = usage.get("completion_tokens")
                # The usage-only chunk carries no token, so its arrival is a
                # stream-close artifact and must count as neither TTFT nor an
                # inter-token gap.
                if not carries_token:
                    continue
                if ttft is None:
                    ttft = now - t0
                else:
                    itls.append(now - last)
                last = now
        return Result(
            index=req["index"],
            prefix_id=req["prefix_id"],
            send_ts=wall0,
            ttft_s=ttft,
            e2e_s=time.perf_counter() - t0,
            prompt_tokens=prompt_toks,
            completion_tokens=completion_toks,
            status="ok",
            itls_ms=";".join(f"{g * 1000:.2f}" for g in itls),
        )
    except Exception as e:  # noqa: BLE001 - record, don't crash the run
        return Result(
            index=req["index"],
            prefix_id=req["prefix_id"],
            send_ts=wall0,
            ttft_s=ttft,
            e2e_s=time.perf_counter() - t0,
            prompt_tokens=None,
            completion_tokens=None,
            status="error",
            error=f"{type(e).__name__}: {e}"[:500],
        )


# Every latency here is timestamped inside the client's event loop, so client
# scheduling delay is indistinguishable from server latency. At the offered
# rates that matter (near the engine knee) hundreds of streams share one loop.
# Sample the loop's own lag so a run can be *shown* not to be measuring Python
# instead of vLLM, rather than assumed not to be.
_LOOP_LAG: List[float] = []


async def _loop_lag_probe(interval: float = 0.01) -> None:
    while True:
        t = time.perf_counter()
        await asyncio.sleep(interval)
        _LOOP_LAG.append(time.perf_counter() - t - interval)


async def run_open_loop(args, workload) -> List[Result]:
    rng = random.Random(args.seed)
    probe = asyncio.create_task(_loop_lag_probe())
    async with httpx.AsyncClient(verify=not args.insecure, limits=_LIMITS) as client:
        tasks = []
        for req in workload:
            tasks.append(
                asyncio.create_task(
                    one_request(client, args.base_url, args.model, req, args.max_tokens)
                )
            )
            await asyncio.sleep(rng.expovariate(args.rate))
        results = list(await asyncio.gather(*tasks))
    probe.cancel()
    return results


def summarize(results: List[Result], wall_s: float) -> str:
    ok = [r for r in results if r.status == "ok"]
    e2e = [r.e2e_s for r in ok if r.e2e_s is not None]
    ttft = [r.ttft_s for r in ok if r.ttft_s is not None]
    toks = sum(r.completion_tokens or 0 for r in ok)
    itl = [float(x) for r in ok for x in r.itls_ms.split(";") if x]
    lag = [x * 1000 for x in _LOOP_LAG]
    return (
        f"n={len(results)} ok={len(ok)} err={len(results) - len(ok)} wall={wall_s:.1f}s "
        f"req/s={len(ok) / wall_s:.2f} tok/s={toks / wall_s:.1f}\n"
        f"TTFT  p50={percentile(ttft, 50):.3f}s p95={percentile(ttft, 95):.3f}s "
        f"p99={percentile(ttft, 99):.3f}s\n"
        f"E2E   p50={percentile(e2e, 50):.3f}s p95={percentile(e2e, 95):.3f}s "
        f"p99={percentile(e2e, 99):.3f}s\n"
        f"ITL   p50={percentile(itl, 50):.1f}ms p95={percentile(itl, 95):.1f}ms "
        f"p99={percentile(itl, 99):.1f}ms  (n={len(itl)})\n"
        f"client loop lag p50={percentile(lag, 50):.1f}ms p99={percentile(lag, 99):.1f}ms"
        " - any TTFT effect smaller than this is client noise"
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--workload", required=True, help="JSONL from workload_gen.py")
    p.add_argument("--rate", type=float, required=True,
                   help="open-loop Poisson arrivals (req/s)")
    p.add_argument("--max-tokens", type=int, default=64)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--insecure",
        action="store_true",
        help="skip TLS verification (gapu-2's edge route serves a self-signed cert)",
    )
    p.add_argument("--out", required=True, help="per-request CSV path")
    args = p.parse_args()

    workload = load_workload(args.workload)
    t0 = time.perf_counter()
    results = asyncio.run(run_open_loop(args, workload))
    wall = time.perf_counter() - t0

    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[x.name for x in dataclasses.fields(Result)])
        w.writeheader()
        for r in sorted(results, key=lambda r: r.index):
            w.writerow(dataclasses.asdict(r))
    print(summarize(results, wall))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()

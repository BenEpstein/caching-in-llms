"""Async load driver: replays a workload against an OpenAI-compatible endpoint.

Measures per request: TTFT (streaming), end-to-end latency, token counts.
Two arrival modes:
  --rate R        open loop, Poisson arrivals at R req/s (tail-latency honest:
                  queueing delay shows up in latency instead of throttling send)
  --concurrency N closed loop, N workers back-to-back

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
from typing import List, Optional

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


def load_workload(path: str):
    reqs = []
    with open(path) as f:
        for line in f:
            d = json.loads(line)
            if "config" in d:
                continue
            reqs.append(d)
    return reqs


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
    prompt_toks = completion_toks = None
    try:
        async with client.stream(
            "POST", f"{base_url}/v1/completions", json=payload, timeout=300.0
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                if ttft is None:
                    ttft = time.perf_counter() - t0
                # usage arrives only in the final chunk - don't JSON-parse
                # every token on the measurement path
                if '"usage"' not in data:
                    continue
                usage = json.loads(data).get("usage")
                if usage:
                    prompt_toks = usage.get("prompt_tokens")
                    completion_toks = usage.get("completion_tokens")
        return Result(
            index=req["index"],
            prefix_id=req["prefix_id"],
            send_ts=wall0,
            ttft_s=ttft,
            e2e_s=time.perf_counter() - t0,
            prompt_tokens=prompt_toks,
            completion_tokens=completion_toks,
            status="ok",
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
            error=f"{type(e).__name__}: {e}"[:200],
        )


async def run_open_loop(args, workload) -> List[Result]:
    rng = random.Random(args.seed)
    async with httpx.AsyncClient(verify=not args.insecure, limits=_LIMITS) as client:
        tasks = []
        for req in workload:
            tasks.append(
                asyncio.create_task(
                    one_request(client, args.base_url, args.model, req, args.max_tokens)
                )
            )
            await asyncio.sleep(rng.expovariate(args.rate))
        return list(await asyncio.gather(*tasks))


async def run_closed_loop(args, workload) -> List[Result]:
    queue: asyncio.Queue = asyncio.Queue()
    for req in workload:
        queue.put_nowait(req)
    results: List[Result] = []

    async def worker(client):
        while True:
            try:
                req = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            results.append(
                await one_request(client, args.base_url, args.model, req, args.max_tokens)
            )

    async with httpx.AsyncClient(verify=not args.insecure, limits=_LIMITS) as client:
        await asyncio.gather(*(worker(client) for _ in range(args.concurrency)))
    return results


def summarize(results: List[Result], wall_s: float) -> str:
    ok = [r for r in results if r.status == "ok"]
    e2e = [r.e2e_s for r in ok if r.e2e_s is not None]
    ttft = [r.ttft_s for r in ok if r.ttft_s is not None]
    toks = sum(r.completion_tokens or 0 for r in ok)
    return (
        f"n={len(results)} ok={len(ok)} err={len(results) - len(ok)} wall={wall_s:.1f}s "
        f"req/s={len(ok) / wall_s:.2f} tok/s={toks / wall_s:.1f}\n"
        f"TTFT  p50={percentile(ttft, 50):.3f}s p95={percentile(ttft, 95):.3f}s "
        f"p99={percentile(ttft, 99):.3f}s\n"
        f"E2E   p50={percentile(e2e, 50):.3f}s p95={percentile(e2e, 95):.3f}s "
        f"p99={percentile(e2e, 99):.3f}s"
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--workload", required=True, help="JSONL from workload_gen.py")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--rate", type=float, help="open-loop Poisson arrivals (req/s)")
    mode.add_argument("--concurrency", type=int, help="closed-loop worker count")
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
    if args.rate:
        results = asyncio.run(run_open_loop(args, workload))
    else:
        results = asyncio.run(run_closed_loop(args, workload))
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

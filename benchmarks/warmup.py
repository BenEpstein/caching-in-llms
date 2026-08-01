"""Unmeasured warm-up passes over the frozen prefix pool (methodology, issue #3).

Sends every prefix in the pool `--passes` times so all 20 prefixes are cached
and admitted to the KV registry before measurement starts. Each request gets a
unique suffix (mirrors real traffic; keeps full prompts distinct). Nothing here
is recorded as a measurement.

The layout_info gate lives in run_cell.sh: after this script exits, the runner
greps the router log for "found by (kvaware|loadaware) router" lines emitted
during the warm-up window - proof that lookups returned non-empty layout_info.

Usage:
  python3 warmup.py --base-url https://… --model Qwen/Qwen2.5-3B-Instruct
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

import httpx

from freeze_workloads import frozen_config
from workload_gen import build_prefix_pool


async def send(client, base_url, model, prompt):
    r = await client.post(
        f"{base_url}/v1/completions",
        json={"model": model, "prompt": prompt, "max_tokens": 4, "temperature": 0.0},
        timeout=300.0,
    )
    r.raise_for_status()


async def run(args) -> int:
    pool = build_prefix_pool(frozen_config(seed=0))
    errors = 0
    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(verify=not args.insecure) as client:
        for pass_no in range(1, args.passes + 1):
            t0 = time.time()
            failed = 0

            async def one(i, prompt):
                nonlocal failed
                async with sem:
                    try:
                        await send(client, args.base_url, args.model, prompt)
                    except Exception as e:  # noqa: BLE001
                        failed += 1
                        print(f"  prefix {i}: {type(e).__name__}: {e}", file=sys.stderr)

            await asyncio.gather(
                *(
                    one(i, f"{p}\n[WARMUP-{pass_no}-{i:03d}] summarize:")
                    for i, p in enumerate(pool)
                )
            )
            print(
                f"pass {pass_no}/{args.passes}: {len(pool) - failed}/{len(pool)} ok "
                f"in {time.time() - t0:.1f}s"
            )
            errors = failed
    # only the final pass gates: earlier passes may race cold engines
    return 1 if errors else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--passes", type=int, default=2)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument(
        "--insecure",
        action="store_true",
        help="skip TLS verification (gapu-2's edge route serves a self-signed cert)",
    )
    args = p.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())

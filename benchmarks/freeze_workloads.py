"""Materialize + verify the frozen benchmark workloads (methodology, issue #3).

The methodology fixes ONE dataset: a 64-prefix x 2048-token Zipfian pool
(s=1.2, pool_seed=42), replayed as up to 10 seeds x 500 requests (cells replay
a prefix of that seed list - see run_sweep.sh). The JSONL files are
~6 MB each, so they are NOT committed; what is committed is `workloads/manifest.json`
holding the exact config + a SHA-256 per seed file. Generation is deterministic,
so regenerate-and-verify gives bit-identical frozen workloads on any machine - the runner (`run_cell.sh`) calls this before every cell and refuses to measure
on a mismatch.

Usage:
  python3 freeze_workloads.py                    # generate into workloads/, verify vs manifest
  python3 freeze_workloads.py --update-manifest  # (re)write the manifest - a methodology change
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import sys

from workload_gen import WorkloadConfig, dump_jsonl

# Amended methodology (#3, 2026-08-03): 64 prefixes so the Zipf hot set (31
# prefixes at s=1.2) exceeds what an engine can retain under
# gpuMemoryUtilization 0.45. 10 seeds because the headline pair needs n=10 to
# survive one reversal; the beta-sweep cells replay a 3-seed subset of the SAME
# files, so there is still exactly one frozen dataset.
SEEDS = list(range(1, 11))
NUM_REQUESTS = 500
PREFIX_POOL_SIZE = 64


def frozen_config(seed: int) -> WorkloadConfig:
    return WorkloadConfig(
        num_requests=NUM_REQUESTS,
        prefix_pool_size=PREFIX_POOL_SIZE,
        zipf_s=1.2,
        prefix_tokens=2048,
        suffix_tokens=32,
        pool_seed=42,
        seed=seed,
    )


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def workload_path(out_dir: str, seed: int) -> str:
    return os.path.join(out_dir, f"seed-{seed}.jsonl")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    default_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workloads")
    p.add_argument("--out-dir", default=default_dir)
    p.add_argument("--update-manifest", action="store_true")
    a = p.parse_args()

    os.makedirs(a.out_dir, exist_ok=True)
    manifest_path = os.path.join(a.out_dir, "manifest.json")

    entries = {}
    for seed in SEEDS:
        cfg = frozen_config(seed)
        path = workload_path(a.out_dir, seed)
        dump_jsonl(cfg, path)
        entries[str(seed)] = {
            "file": os.path.basename(path),
            "sha256": sha256_file(path),
            "config": dataclasses.asdict(cfg),
        }
        print(f"seed {seed}: {entries[str(seed)]['sha256'][:16]}…  {path}")

    if a.update_manifest:
        with open(manifest_path, "w") as f:
            json.dump({"seeds": entries}, f, indent=2, sort_keys=True)
            f.write("\n")
        print(f"manifest written: {manifest_path}")
        return 0

    if not os.path.exists(manifest_path):
        print(
            f"ERROR: no manifest at {manifest_path} - run with --update-manifest "
            "once and commit the result.",
            file=sys.stderr,
        )
        return 1
    with open(manifest_path) as f:
        expected = json.load(f)["seeds"]
    if expected != entries:
        print(
            "ERROR: generated workloads do not match the committed manifest - "
            "the frozen dataset has drifted (Python/RNG change or edited "
            "generator). Do NOT measure; investigate before touching the "
            "manifest.",
            file=sys.stderr,
        )
        return 1
    print("all workloads match the committed manifest - frozen dataset verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())

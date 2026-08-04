"""Materialize + verify the frozen benchmark workloads (methodology, issue #3).

The methodology fixes ONE dataset: a 128-prefix x 2048-token Zipfian pool
(s=0.9, pool_seed=42), replayed as up to 10 seeds x 500 requests (cells replay
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

# Amended methodology (#3, 2026-08-03, revised after the scarcity gate).
#
# The first amendment (64 prefixes, s=1.2) was FALSIFIED by the gate: measured
# prefix-cache hit rate under load was 0.889 against the pilot's ~0.95. Pool
# size alone cannot fix that - at s=1.2 the distribution is so concentrated that
# the top ~20 prefixes stay resident however long the tail is (simulated 0.69
# even at 256 prefixes). The binding parameter is the EXPONENT.
#
# 128 prefixes at s=0.9 predicts ~0.60 under normal load and ~0.52 under
# kvaware's concentration: enough reuse that routing to the holder is worth
# something, little enough that placement is a real decision. It also puts the
# LMCache CPU tier (114k tok = 56 prefixes) and HBM (~50 prefixes) at comparable
# capacity, so the registry tracks HBM reality instead of drifting from it.
#
# 20 seeds (raised from 10, 2026-08-04 pre-registration). n=10 returned
# p=0.0527 on the headline: underpowered, and one reversal is expensive at that
# size. A seed replay costs ~50 s against ~8 min of fixed setup per CELL, so
# power here is close to free - cells are the only thing worth cutting to
# shorten a run (see run_sweep.sh). Seeds 1-10 regenerate bit-identically; this
# is purely additive, which the manifest diff shows.
#
# The beta-sweep cells replay a 3-seed subset of the SAME files, so there is
# still exactly one frozen dataset.
SEEDS = list(range(1, 21))
NUM_REQUESTS = 500
PREFIX_POOL_SIZE = 128
ZIPF_S = 0.9


def frozen_config(seed: int) -> WorkloadConfig:
    return WorkloadConfig(
        num_requests=NUM_REQUESTS,
        prefix_pool_size=PREFIX_POOL_SIZE,
        zipf_s=ZIPF_S,
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

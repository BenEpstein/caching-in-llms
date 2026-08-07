"""Materialize + verify the frozen benchmark workloads (methodology, issue #3).

The methodology fixes ONE dataset: a 128-prefix Zipfian pool (s=0.9,
pool_seed=42), replayed as up to 20 seeds x 500 requests (cells replay
a prefix of that seed list - see run_sweep.sh).

`prefix_tokens=2048` below is the generator's REQUEST, not the result: `_filler`
emits `approx_tokens * 0.75` words, so the shared prefix is **1544 tokens** and
the full prompt (ISL) is **1578**, verified against the engine's /tokenize
endpoint and against `usage.prompt_tokens` on every recorded request. The knob
keeps its name so the frozen manifest's checksums stay valid; report 1544/1578.

The JSONL files are
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

from workload_gen import (NovelWorkloadConfig, WorkloadConfig, dump_jsonl,
                          dump_novel_jsonl, generate_novel, reuse_factor)

# Amended methodology (#3, 2026-08-03, revised after the scarcity gate).
#
# The EXPONENT is the binding parameter, not the pool size: at s=1.2 the top
# ~20 prefixes stay resident however long the tail is (simulated 0.69 even at
# 256 prefixes). 128 prefixes at s=0.9 predicts ~0.60 under normal load and
# ~0.52 under kvaware's concentration: enough reuse that routing to the holder
# is worth something, little enough that placement is a real decision. It also
# puts the LMCache CPU tier (114k tok = 56 prefixes) and HBM (~50 prefixes) at
# comparable capacity, so the registry tracks HBM reality instead of drifting.
#
# 20 seeds (raised from 10, 2026-08-04 pre-registration). n=10 returned
# p=0.0527 on the headline: underpowered, and one reversal is expensive at that
# size. A seed replay costs ~50 s against ~8 min of fixed setup per CELL, so
# power here is close to free - cells are the only thing worth cutting to
# shorten a run (see run_sweep.sh).
#
# The beta-sweep cells replay a 3-seed subset of the SAME files, so there is
# still exactly one frozen dataset.
SEEDS = list(range(1, 21))
NUM_REQUESTS = 500
PREFIX_POOL_SIZE = 128
ZIPF_S = 0.9


# Second profile (guidelines §3: "novel long prompts, unlikely to be cached - to measure
# cache overhead"). Fewer seeds than the Zipfian profile on purpose: this arm answers a
# cost question with a large expected effect (cache-on vs cache-off on a workload that
# never hits), not a placement question with a small one, so it does not need n=20.
NOVEL_SEEDS = list(range(1, 7))
NOVEL_NUM_REQUESTS = 500


def novel_frozen_config(seed: int) -> NovelWorkloadConfig:
    return NovelWorkloadConfig(
        num_requests=NOVEL_NUM_REQUESTS,
        prompt_tokens=2048,
        suffix_tokens=32,
        seed=seed,
    )


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


def build_zipfian(out_dir: str) -> dict:
    entries = {}
    for seed in SEEDS:
        cfg = frozen_config(seed)
        path = workload_path(out_dir, seed)
        dump_jsonl(cfg, path)
        entries[str(seed)] = {
            "file": os.path.basename(path),
            "sha256": sha256_file(path),
            "config": dataclasses.asdict(cfg),
        }
        print(f"seed {seed}: {entries[str(seed)]['sha256'][:16]}…  {path}")
    return entries


def build_novel(out_dir: str) -> dict:
    """Materialize the novel-prompt profile and ASSERT it has no reuse.

    A silent regression here would be invisible in the results: a workload that quietly
    started sharing prefixes would measure cache benefit while claiming to measure cache
    cost, and the arm would look like a small overhead instead of a broken experiment.
    """
    entries = {}
    for seed in NOVEL_SEEDS:
        cfg = novel_frozen_config(seed)
        path = workload_path(out_dir, seed)
        dump_novel_jsonl(cfg, path)

        rf = reuse_factor([r.prefix_id for r in generate_novel(cfg)])
        if rf != 1.0:
            raise SystemExit(
                f"novel seed {seed}: reuse factor {rf} != 1.0 - this profile MUST have "
                "no shared prefixes or it is not measuring cache overhead"
            )
        entries[str(seed)] = {
            "file": os.path.basename(path),
            "sha256": sha256_file(path),
            "config": dataclasses.asdict(cfg),
        }
        print(f"novel seed {seed}: {entries[str(seed)]['sha256'][:16]}…  {path}  (reuse 1.0)")
    return entries


# Each profile is a directory that CONTAINS its own manifest.json. That shape is load
# bearing: `verify_dataset.sh` copies the committed manifest into a writable directory and
# runs this with `--out-dir` pointed at it, because /app is read-only under the restricted
# SCC's arbitrary uid. A manifest resolved anywhere other than inside --out-dir breaks the
# in-cluster Job (#27).
PROFILES = {
    "zipfian": ("workloads", build_zipfian),
    "novel": (os.path.join("workloads", "novel"), build_novel),
}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    bench_dir = os.path.dirname(os.path.abspath(__file__))
    p.add_argument("--profile", choices=sorted(PROFILES), default="zipfian",
                   help="zipfian = the frozen shared-prefix dataset (default); "
                        "novel = the no-reuse cache-overhead profile")
    p.add_argument("--out-dir", default=None)
    p.add_argument("--update-manifest", action="store_true")
    a = p.parse_args()

    sub_dir, build = PROFILES[a.profile]
    out_dir = a.out_dir or os.path.join(bench_dir, sub_dir)
    manifest_path = os.path.join(out_dir, "manifest.json")

    os.makedirs(out_dir, exist_ok=True)
    entries = build(out_dir)

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

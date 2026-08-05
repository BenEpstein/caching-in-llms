"""Zipfian prefix workload generator.

Produces the skewed workload that exposes the kvaware router's load-imbalance
pathology: a small set of very popular long prefixes (shared system prompts /
RAG contexts) + a long tail of rare ones. Each request = shared prefix + short
unique suffix.

Split in two layers:
  - pure generation (this module, seedable, unit-testable, no I/O)
  - async load driver (`load_driver.py`) that replays a generated workload
    against an OpenAI-compatible endpoint and records per-request metrics.
"""

from __future__ import annotations

import dataclasses
import json
import random
from typing import Iterator, List, Sequence

# ~4 chars/token is a decent approximation for English filler; the driver
# records *actual* prompt token counts from the API response, so this only
# controls approximate prompt sizing, not measurement.
_WORDS = (
    "system context document retrieval answer question knowledge base cache "
    "policy latency throughput token prefix router instance replica workload "
    "analysis metric benchmark experiment evaluation baseline extension"
).split()


@dataclasses.dataclass(frozen=True)
class WorkloadConfig:
    # These defaults ARE the frozen evaluation workload (see the manifest under
    # benchmarks/workloads/). `test_defaults_match_the_frozen_manifest` pins them
    # to it: they drifted once - 20 prefixes at s=1.2 were exploratory values that
    # outlived the freeze and were still being quoted as the shipped skew.
    num_requests: int = 500
    prefix_pool_size: int = 128         # number of distinct shared prefixes
    zipf_s: float = 0.9                 # skew: 0 = uniform, >1 = heavy head
    prefix_tokens: int = 2048           # approx tokens per shared prefix
    suffix_tokens: int = 32             # approx tokens per unique suffix
    # The pool is THE dataset (issue #3: one frozen prefix pool); `seed` only
    # varies sampling order + suffixes, so N seed replays share one pool and a
    # single warm-up pass covers all of them.
    pool_seed: int = 42
    seed: int = 42


@dataclasses.dataclass(frozen=True)
class Request:
    index: int
    prefix_id: int
    prompt: str


def zipf_weights(n: int, s: float) -> List[float]:
    """Normalized Zipf probabilities for ranks 1..n: p(k) ∝ 1/k^s."""
    if n <= 0:
        raise ValueError("n must be positive")
    raw = [1.0 / (k**s) for k in range(1, n + 1)]
    total = sum(raw)
    return [w / total for w in raw]


def _filler(rng: random.Random, approx_tokens: int, tag: str) -> str:
    # Tag makes each prefix unique from its first characters so prefix caches
    # can't accidentally share blocks between distinct prefixes.
    words = [tag]
    words += rng.choices(_WORDS, k=max(1, int(approx_tokens * 0.75)))
    return " ".join(words)


def build_prefix_pool(cfg: WorkloadConfig) -> List[str]:
    """Deterministic pool of long shared prefixes."""
    rng = random.Random(cfg.pool_seed)
    return [
        _filler(rng, cfg.prefix_tokens, tag=f"[PREFIX-{i:03d}]")
        for i in range(cfg.prefix_pool_size)
    ]


def generate(cfg: WorkloadConfig) -> Iterator[Request]:
    """Yield requests with Zipf-distributed prefix popularity.

    Deterministic for a given config (seeded RNG), so any run is replayable —
    the same workload can be sent to roundrobin / kvaware / loadaware setups.
    """
    prefixes = build_prefix_pool(cfg)
    weights = zipf_weights(cfg.prefix_pool_size, cfg.zipf_s)
    rng = random.Random(cfg.seed + 1)
    for i in range(cfg.num_requests):
        pid = rng.choices(range(cfg.prefix_pool_size), weights=weights, k=1)[0]
        suffix = _filler(rng, cfg.suffix_tokens, tag=f"[Q-{i:06d}]")
        yield Request(index=i, prefix_id=pid, prompt=f"{prefixes[pid]}\n{suffix}")


@dataclasses.dataclass(frozen=True)
class NovelWorkloadConfig:
    """The second profile the guidelines name: *novel long prompts, unlikely to be cached*.

    Every request carries its own 2048-token prompt that no other request shares, so the
    cache can never hit. That makes this the **cost** measurement: run it with LMCache on
    and off and the difference is what lookup, admission and storage charge on the miss
    path, with zero hit benefit to offset it.

    Deliberately a SEPARATE config class rather than a flag on `WorkloadConfig`. The frozen
    Zipfian dataset is pinned by a manifest that stores `dataclasses.asdict(cfg)` per seed,
    so adding a field there would change every committed hash and make `run_cell.sh` refuse
    to measure. The two profiles get two manifests and never interfere.

    Prompt sizing mirrors the Zipfian profile (2048 + 32) on purpose: the only difference
    between the two workloads is reuse, so a difference in overhead is attributable to reuse.
    """

    num_requests: int = 500
    prompt_tokens: int = 2048           # approx tokens in each unique prompt
    suffix_tokens: int = 32             # approx tokens of per-request tail
    seed: int = 42


def generate_novel(cfg: NovelWorkloadConfig) -> Iterator[Request]:
    """Yield requests with NO shared prefix - every prompt is unique from its first bytes.

    `prefix_id` is the request index rather than a pool index: there is no pool, and every
    request is its own singleton "prefix". Analysis code keyed on `prefix_id` therefore
    still works and reports the truth (a reuse factor of exactly 1.0).
    """
    rng = random.Random(cfg.seed + 1)
    for i in range(cfg.num_requests):
        # The tag leads the string, so two prompts diverge at the first token and no
        # prefix-cache block can be shared between them even by accident.
        body = _filler(rng, cfg.prompt_tokens, tag=f"[NOVEL-{cfg.seed:03d}-{i:06d}]")
        suffix = _filler(rng, cfg.suffix_tokens, tag=f"[Q-{i:06d}]")
        yield Request(index=i, prefix_id=i, prompt=f"{body}\n{suffix}")


def reuse_factor(prefix_ids: Sequence[int]) -> float:
    """Requests per distinct prefix. 1.0 means no reuse at all - the novel profile's
    defining property, and worth asserting rather than assuming before a run is funded."""
    if not prefix_ids:
        return 0.0
    return len(prefix_ids) / len(set(prefix_ids))


def dump_novel_jsonl(cfg: NovelWorkloadConfig, path: str) -> None:
    """Materialize a novel-prompt workload to JSONL, same line format as `dump_jsonl`."""
    with open(path, "w") as f:
        f.write(json.dumps({"config": dataclasses.asdict(cfg)}) + "\n")
        for req in generate_novel(cfg):
            f.write(json.dumps(dataclasses.asdict(req)) + "\n")


def dump_jsonl(cfg: WorkloadConfig, path: str) -> None:
    """Materialize a workload to JSONL (one request per line) for replay."""
    with open(path, "w") as f:
        f.write(json.dumps({"config": dataclasses.asdict(cfg)}) + "\n")
        for req in generate(cfg):
            f.write(json.dumps(dataclasses.asdict(req)) + "\n")


def head_share(prefix_ids: Sequence[int], head: int = 1) -> float:
    """Fraction of requests that hit the `head` most popular prefixes.
    Diagnostic for how skewed a generated workload actually is."""
    if not prefix_ids:
        return 0.0
    from collections import Counter

    counts = Counter(prefix_ids).most_common(head)
    return sum(c for _, c in counts) / len(prefix_ids)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description=__doc__)
    # Defaults mirror WorkloadConfig's, i.e. the frozen workload.
    p.add_argument("--num-requests", type=int, default=WorkloadConfig.num_requests)
    p.add_argument("--prefix-pool-size", type=int, default=WorkloadConfig.prefix_pool_size)
    p.add_argument("--zipf-s", type=float, default=WorkloadConfig.zipf_s)
    p.add_argument("--prefix-tokens", type=int, default=2048)
    p.add_argument("--suffix-tokens", type=int, default=32)
    p.add_argument("--pool-seed", type=int, default=42)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", required=True, help="output JSONL path")
    a = p.parse_args()
    cfg = WorkloadConfig(
        num_requests=a.num_requests,
        prefix_pool_size=a.prefix_pool_size,
        zipf_s=a.zipf_s,
        prefix_tokens=a.prefix_tokens,
        suffix_tokens=a.suffix_tokens,
        pool_seed=a.pool_seed,
        seed=a.seed,
    )
    dump_jsonl(cfg, a.out)
    ids = [r.prefix_id for r in generate(cfg)]
    print(
        f"wrote {cfg.num_requests} requests to {a.out}; "
        f"top-1 prefix share = {head_share(ids, 1):.1%}, "
        f"top-3 share = {head_share(ids, 3):.1%}"
    )

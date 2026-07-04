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
    num_requests: int = 1000
    prefix_pool_size: int = 20          # number of distinct shared prefixes
    zipf_s: float = 1.2                 # skew: 0 = uniform, >1 = heavy head
    prefix_tokens: int = 2048           # approx tokens per shared prefix
    suffix_tokens: int = 32             # approx tokens per unique suffix
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
    rng = random.Random(cfg.seed)
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
    p.add_argument("--num-requests", type=int, default=1000)
    p.add_argument("--prefix-pool-size", type=int, default=20)
    p.add_argument("--zipf-s", type=float, default=1.2)
    p.add_argument("--prefix-tokens", type=int, default=2048)
    p.add_argument("--suffix-tokens", type=int, default=32)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--out", required=True, help="output JSONL path")
    a = p.parse_args()
    cfg = WorkloadConfig(
        num_requests=a.num_requests,
        prefix_pool_size=a.prefix_pool_size,
        zipf_s=a.zipf_s,
        prefix_tokens=a.prefix_tokens,
        suffix_tokens=a.suffix_tokens,
        seed=a.seed,
    )
    dump_jsonl(cfg, a.out)
    ids = [r.prefix_id for r in generate(cfg)]
    print(
        f"wrote {cfg.num_requests} requests to {a.out}; "
        f"top-1 prefix share = {head_share(ids, 1):.1%}, "
        f"top-3 share = {head_share(ids, 3):.1%}"
    )

"""Tests for the frozen-workload layer: one pool across seeds, stable hashes."""

import hashlib

from freeze_workloads import SEEDS, frozen_config
from workload_gen import build_prefix_pool, dump_jsonl, generate


def test_pool_is_identical_across_replay_seeds():
    pools = [build_prefix_pool(frozen_config(seed=s)) for s in SEEDS]
    assert all(p == pools[0] for p in pools), (
        "all replay seeds must share ONE frozen prefix pool - a single warm-up "
        "pass has to cover every seed (issue #3)"
    )


def test_sampling_differs_across_replay_seeds():
    a = [r.prefix_id for r in generate(frozen_config(seed=1))]
    b = [r.prefix_id for r in generate(frozen_config(seed=2))]
    assert a != b


def test_frozen_config_matches_methodology():
    """Pins the amended methodology (#3, 2026-08-03).

    64 prefixes, not 20: at s=1.2 the Zipf hot set (90% of traffic) is 31
    prefixes = 63k tokens, which must EXCEED what one engine retains under
    gpuMemoryUtilization 0.45 (~99k pool - ~56k peak in-flight = ~43k = 21
    prefixes). With 20 prefixes every engine held everything and there was no
    placement decision to get right.
    """
    cfg = frozen_config(seed=1)
    assert cfg.num_requests == 500
    assert cfg.prefix_pool_size == 64
    assert cfg.zipf_s == 1.2
    assert cfg.prefix_tokens == 2048
    assert cfg.suffix_tokens == 32


def test_ten_seeds_available_for_the_headline_pair():
    """n=6 cannot survive one reversal (exact Wilcoxon caps at p=0.219) - the
    pilot hit exactly that. The headline cells replay 10; sweep cells replay a
    subset of the same files."""
    assert SEEDS == list(range(1, 11))


def test_dump_is_bit_stable(tmp_path):
    """Same config → byte-identical JSONL → the manifest hash check is meaningful."""
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    dump_jsonl(frozen_config(seed=3), str(a))
    dump_jsonl(frozen_config(seed=3), str(b))
    ha = hashlib.sha256(a.read_bytes()).hexdigest()
    hb = hashlib.sha256(b.read_bytes()).hexdigest()
    assert ha == hb

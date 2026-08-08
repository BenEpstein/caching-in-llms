"""Unit tests for the workload generator (run: pytest benchmarks/)."""

import dataclasses
import math

import pytest

from workload_gen import (
    NovelWorkloadConfig,
    generate_novel,
    reuse_factor,
    WorkloadConfig,
    build_prefix_pool,
    generate,
    head_share,
    zipf_weights,
)


def test_zipf_weights_normalized():
    w = zipf_weights(50, 1.2)
    assert math.isclose(sum(w), 1.0, rel_tol=1e-9)
    assert all(a >= b for a, b in zip(w, w[1:])), "weights must be non-increasing"


def test_zipf_uniform_at_s0():
    w = zipf_weights(10, 0.0)
    assert all(math.isclose(x, 0.1, rel_tol=1e-9) for x in w)


def test_zipf_rejects_bad_n():
    with pytest.raises(ValueError):
        zipf_weights(0, 1.0)


def test_generation_is_deterministic():
    cfg = WorkloadConfig(num_requests=100, seed=7)
    a = [(r.prefix_id, r.prompt) for r in generate(cfg)]
    b = [(r.prefix_id, r.prompt) for r in generate(cfg)]
    assert a == b


def test_different_seeds_differ():
    a = [r.prefix_id for r in generate(WorkloadConfig(num_requests=200, seed=1))]
    b = [r.prefix_id for r in generate(WorkloadConfig(num_requests=200, seed=2))]
    assert a != b


def test_prefixes_are_distinct_and_reused():
    cfg = WorkloadConfig(num_requests=300, prefix_pool_size=5, seed=3)
    pool = build_prefix_pool(cfg)
    assert len(set(pool)) == cfg.prefix_pool_size
    reqs = list(generate(cfg))
    for r in reqs:
        assert r.prompt.startswith(pool[r.prefix_id])
    assert len({r.prompt for r in reqs}) == len(reqs)


def test_skew_increases_head_share():
    n = 2000
    ids_flat = [r.prefix_id for r in generate(WorkloadConfig(n, zipf_s=0.0, seed=5))]
    ids_skew = [r.prefix_id for r in generate(WorkloadConfig(n, zipf_s=1.5, seed=5))]
    assert head_share(ids_skew, 1) > head_share(ids_flat, 1) * 2


def test_prompt_length_scales_with_prefix_tokens():
    short = build_prefix_pool(WorkloadConfig(prefix_tokens=100, seed=9))[0]
    long = build_prefix_pool(WorkloadConfig(prefix_tokens=2000, seed=9))[0]
    assert len(long) > len(short) * 10


def test_defaults_match_the_frozen_manifest():
    """The generator's defaults must BE the frozen evaluation workload.

    These drifted once: the dataclass kept `prefix_pool_size=20, zipf_s=1.2`
    long after the freeze pinned 128 prefixes at s=0.9, so anyone reading the
    code (or instantiating `WorkloadConfig()` directly) got the exploratory
    values and the wrong skew. The manifest is the authority; this pins the
    defaults to it so the two cannot separate again.
    """
    import json
    import os

    manifest_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "workloads", "manifest.json"
    )
    with open(manifest_path) as f:
        manifest = json.load(f)

    frozen = next(iter(manifest["seeds"].values()))["config"]
    defaults = WorkloadConfig()
    for field in (
        "num_requests",
        "prefix_pool_size",
        "zipf_s",
        "prefix_tokens",
        "suffix_tokens",
        "pool_seed",
    ):
        assert getattr(defaults, field) == frozen[field], (
            f"WorkloadConfig.{field} is {getattr(defaults, field)!r} but the frozen "
            f"manifest says {frozen[field]!r} - update the default or re-freeze"
        )


# --- the novel-prompt profile (guidelines §3: "novel long prompts, unlikely to be
# cached - to measure cache overhead"). Its ONLY defining property is that nothing is
# ever reused, so that is what these tests defend. ---


def test_novel_workload_has_no_reuse_at_all():
    cfg = NovelWorkloadConfig(num_requests=200, seed=7)
    ids = [r.prefix_id for r in generate_novel(cfg)]
    assert reuse_factor(ids) == 1.0
    assert len(set(ids)) == 200


def test_novel_prompts_diverge_at_the_first_token():
    """A shared leading block would let the prefix cache hit and turn a cost
    measurement into a benefit measurement without changing any visible number."""
    prompts = [r.prompt for r in generate_novel(NovelWorkloadConfig(num_requests=50))]
    heads = {p[:64] for p in prompts}
    assert len(heads) == 50, "prompts must be distinct from their very first characters"


def test_novel_workload_is_deterministic():
    a = [r.prompt for r in generate_novel(NovelWorkloadConfig(num_requests=30, seed=3))]
    b = [r.prompt for r in generate_novel(NovelWorkloadConfig(num_requests=30, seed=3))]
    assert a == b


def test_novel_seeds_produce_different_prompts():
    a = [r.prompt for r in generate_novel(NovelWorkloadConfig(num_requests=30, seed=1))]
    b = [r.prompt for r in generate_novel(NovelWorkloadConfig(num_requests=30, seed=2))]
    assert set(a).isdisjoint(b)


def test_novel_prompt_length_matches_the_zipfian_profile():
    """The two profiles must differ ONLY in reuse. If the novel prompts were shorter,
    a latency difference could be prompt length rather than cache overhead."""
    zipf = next(iter(generate(WorkloadConfig(num_requests=1, prefix_tokens=2048,
                                             suffix_tokens=32))))
    novel = next(iter(generate_novel(NovelWorkloadConfig(num_requests=1,
                                                         prompt_tokens=2048,
                                                         suffix_tokens=32))))
    assert abs(len(novel.prompt) - len(zipf.prompt)) / len(zipf.prompt) < 0.05


def test_reuse_factor_detects_reuse():
    assert reuse_factor([0, 1, 2, 3]) == 1.0
    assert reuse_factor([0, 0, 1, 1]) == 2.0
    assert reuse_factor([]) == 0.0


def test_zipfian_config_fields_are_frozen():
    """The frozen manifest stores dataclasses.asdict(WorkloadConfig) per seed, so ADDING
    a field here silently invalidates every committed hash and run_cell.sh stops being
    able to measure. If this test fails, you needed a separate config class - which is
    exactly why NovelWorkloadConfig is one."""
    assert [f.name for f in dataclasses.fields(WorkloadConfig)] == [
        "num_requests", "prefix_pool_size", "zipf_s", "prefix_tokens",
        "suffix_tokens", "pool_seed", "seed",
    ]

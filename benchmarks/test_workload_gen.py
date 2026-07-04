"""Unit tests for the workload generator (run: pytest benchmarks/)."""

import math

import pytest

from workload_gen import (
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
    # every request's prompt starts with its assigned pool prefix
    for r in reqs:
        assert r.prompt.startswith(pool[r.prefix_id])
    # all suffixes unique
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

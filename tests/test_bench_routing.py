"""Micro-benchmarks for the router's hot path (issue #24, guidelines §3 Automation).

§3 asks for benchmark scripts *and* for them to rerun in CI on every commit. The cluster
sweep cannot: it needs two A10s, ~40 minutes per cell, and an OpenShift namespace. What
*can* rerun per-commit is the part of this project that is pure computation - the placement
scoring path - and that is the part where a performance regression would be silent and
harmful, because it runs **once per request, over every endpoint**, inside the router's
event loop.

That matters concretely here. The whole system was once limited to 4 req/s by ~245 ms of
per-request event-loop blocking in the router (issue #21); a routing path that quietly got
slower would reproduce that class of failure with no test turning red. These benchmarks put
a number on the path so a regression shows up as a number, not as a bad sweep three days
later.

`relative_loads` deserves its own benchmark rather than being folded into the scoring one.
Since the load term was normalized against the fleet mean, it is the piece whose cost grows
with fleet size *and* runs before any endpoint can be scored - the router recomputes the
fleet's mean load per request rather than inheriting a tuned constant.

They benchmark the tracked patch files themselves, loaded by `conftest.py` - the same bytes
`apply-router-patch.sh` mounts into the pod - not a re-typed copy.

Run standalone:  pytest tests/test_bench_routing.py --benchmark-only
"""

import pytest
from conftest import (
    EndpointInfo,
    RequestStats,
    routing_logic,
)

PROMPT_TOKENS = 2048


def _router(beta=1.0):
    """A LoadAwareRouter with the singleton metaclass sidestepped.

    The class is a singleton in production (one router per process); for benchmarking we
    want a fresh instance, so build it through the metaclass-free path. `beta` defaults to
    the shipped default so the numbers describe the deployed configuration.
    """
    obj = object.__new__(routing_logic.LoadAwareRouter)
    obj.beta = beta
    return obj


def _fleet(n):
    """n endpoints with a spread of in-flight counts, as `relative_loads` expects."""
    urls = [f"http://10.0.0.{i}:8000" for i in range(n)]
    endpoints = [EndpointInfo(url=u) for u in urls]
    stats = {
        u: RequestStats(in_prefill_requests=i % 5, in_decoding_requests=(i * 3) % 11)
        for i, u in enumerate(urls)
    }
    return endpoints, stats


def test_bench_score_endpoint(benchmark):
    """The innermost operation: one endpoint's score. Called once per endpoint per request."""
    r = _router()
    result = benchmark(r.score_endpoint, 1024, PROMPT_TOKENS, 0.5)
    assert result == pytest.approx(0.5 - 1.0 * 0.5)


def test_bench_relative_loads_two_engines(benchmark):
    """Fleet-mean normalization at our deployed size - runs once per request.

    This is the work the policy added over `kvaware`, which reads no load at all.
    """
    r = _router()
    endpoints, stats = _fleet(2)
    out = benchmark(r.relative_loads, stats, endpoints)
    assert len(out) == 2


def test_bench_relative_loads_scales_with_fleet_size(benchmark):
    """16 endpoints: the normalization is O(fleet) and recomputed per request.

    Not a pass/fail threshold - thresholds on shared CI runners are flaky and would get
    muted, which is worse than no test. This records the number so a 10x regression is
    visible in the run log and in pytest-benchmark's comparison output.
    """
    r = _router()
    endpoints, stats = _fleet(16)
    assert len(benchmark(r.relative_loads, stats, endpoints)) == 16


def test_bench_full_placement_arithmetic(benchmark):
    """Normalize the fleet, then score every endpoint and take the argmax.

    The whole per-request cost of the policy, minus the async plumbing and the Controller
    round-trip - which is what a regression in the arithmetic would otherwise hide.
    """
    r = _router()
    endpoints, stats = _fleet(2)
    matched = {e.url: 1024 if i == 0 else 0 for i, e in enumerate(endpoints)}

    def place():
        rel = r.relative_loads(stats, endpoints)
        return max(
            (r.score_endpoint(matched[e.url], PROMPT_TOKENS, rel[e.url]), e.url)
            for e in endpoints
        )

    assert benchmark(place)[1] in matched


def test_bench_matched_tokens_by_url(benchmark):
    """The instance_id -> URL bridge, walked once per request.

    This is the path that has to skip dead instance ids after an engine restart, so it
    does real work rather than a dict lookup, and it grows with fleet size.
    """
    r = _router()
    r.instance_id_to_ip = {f"instance-{i}": f"http://10.0.0.{i}:8000" for i in range(8)}
    layout = {f"instance-{i}": (None, 512 * (i % 4)) for i in range(8)}
    assert benchmark(r.matched_tokens_by_url, layout)["http://10.0.0.3:8000"] == 1536


def test_bench_load_penalty(benchmark):
    """Reading the raw live load for one endpoint out of the router's stats map.

    `request_stats` is the event-driven source, not the scraped `engine_stats` - the
    latter lags by the scrape interval, which is why the policy reads this one.
    """
    r = _router()
    url = "http://10.0.0.1:8000"
    stats = {url: RequestStats(in_prefill_requests=3, in_decoding_requests=9)}
    assert benchmark(r.load_penalty, stats, url) == 12

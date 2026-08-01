"""Unit tests for the `loadaware` placement policy (run: pytest tests/).

Change 2 of the project: the router must place a request by
``alpha * cache_hit_benefit - beta * load_penalty`` over *every* instance,
instead of kvaware's "first instance reported to hold the prefix". See
docs/handoff-core-implementation.md §4 and CONTEXT.md ("Placement Policy").

No cluster, no GPU, no vllm_router install — conftest.py stubs the router's
import surface and loads the tracked patch file itself, so the bytes under test
are the bytes `deploy/dev/apply-router-patch.sh` mounts into the pod.
"""

import asyncio

import pytest
from conftest import (
    EndpointInfo,
    FakeRequest,
    LookupRetMsg,
    QueryInstMsg,
    QueryInstRetMsg,
    RequestStats,
    SingletonABCMeta,
    routing_logic,
)

URL_A = "http://10.0.0.1:8000"
URL_B = "http://10.0.0.2:8000"
INST_A = "instance-a"
INST_B = "instance-b"
SESSION_KEY = "x-user-id"
LOCAL = "LocalCPUBackend"
PROMPT_TOKENS = 2048


def run(coro):
    """Drive one coroutine; avoids a pytest-asyncio dependency."""
    return asyncio.run(coro)


def endpoints(*urls):
    return [EndpointInfo(url=url) for url in urls]


def busy(in_prefill=0, in_decoding=0, qps=0.0):
    return RequestStats(qps=qps, in_prefill_requests=in_prefill, in_decoding_requests=in_decoding)


def make_router(alpha=None, beta=None, threshold=2000, mapped=True):
    """A LoadAwareRouter with the Controller and tokenizer faked out.

    `RoutingInterface` is a singleton, so the registry is cleared first: a test
    that builds a second router with different weights would otherwise silently
    get the first one back.
    """
    SingletonABCMeta._instances.clear()
    router = routing_logic.LoadAwareRouter(9000, SESSION_KEY, threshold, alpha, beta)
    if mapped:
        router.instance_id_to_ip = {INST_A: URL_A, INST_B: URL_B}
    return router


class FakeTokenizer:
    """`encode` returns a prompt of a fixed length; content is irrelevant."""

    def __init__(self, n_tokens=PROMPT_TOKENS):
        self.n_tokens = n_tokens

    def encode(self, _prompt):
        return list(range(self.n_tokens))


# --- the score ---------------------------------------------------------------


def test_all_cold_picks_the_idle_instance():
    """No cache anywhere: the score is pure load penalty."""
    router = make_router(alpha=1.0, beta=0.1)
    stats = {URL_A: busy(in_decoding=4), URL_B: busy(in_decoding=1)}
    url = router.select_url(endpoints(URL_A, URL_B), stats, {}, PROMPT_TOKENS)
    assert url == URL_B


def test_equally_idle_picks_the_warmest_instance():
    """No load anywhere: the score is pure cache-hit benefit."""
    router = make_router(alpha=1.0, beta=0.1)
    layout = {INST_A: (LOCAL, 512), INST_B: (LOCAL, 2048)}
    url = router.select_url(endpoints(URL_A, URL_B), {}, layout, PROMPT_TOKENS)
    assert url == URL_B


def test_warm_but_loaded_loses_to_cold_but_idle():
    """The whole point of the policy: 12 in-flight requests outweigh a full hit.

    benefit(A) = 2048/2048 = 1.0, load(A) = 12  ->  1.0 - 0.1*12 = -0.2
    benefit(B) = 0,               load(B) = 0   ->  0.0
    """
    router = make_router(alpha=1.0, beta=0.1)
    layout = {INST_A: (LOCAL, PROMPT_TOKENS)}
    stats = {URL_A: busy(in_prefill=4, in_decoding=8), URL_B: busy()}
    url = router.select_url(endpoints(URL_A, URL_B), stats, layout, PROMPT_TOKENS)
    assert url == URL_B


def test_warm_but_loaded_wins_when_beta_is_small():
    """Same fixture, smaller beta: the crossover moves. This is the sweep axis.

    1.0 - 0.01*12 = 0.88  >  0.0
    """
    router = make_router(alpha=1.0, beta=0.01)
    layout = {INST_A: (LOCAL, PROMPT_TOKENS)}
    stats = {URL_A: busy(in_prefill=4, in_decoding=8), URL_B: busy()}
    url = router.select_url(endpoints(URL_A, URL_B), stats, layout, PROMPT_TOKENS)
    assert url == URL_A


def test_beta_zero_is_cache_only_placement():
    """beta = 0 degenerates to "most cached wins", however loaded it is."""
    router = make_router(alpha=1.0, beta=0.0)
    layout = {INST_A: (LOCAL, PROMPT_TOKENS)}
    stats = {URL_A: busy(in_decoding=100), URL_B: busy()}
    url = router.select_url(endpoints(URL_A, URL_B), stats, layout, PROMPT_TOKENS)
    assert url == URL_A


def test_alpha_zero_is_load_only_placement():
    """alpha = 0 degenerates to least-loaded, ignoring a full cache hit."""
    router = make_router(alpha=0.0, beta=0.1)
    layout = {INST_A: (LOCAL, PROMPT_TOKENS)}
    stats = {URL_A: busy(in_decoding=2), URL_B: busy()}
    url = router.select_url(endpoints(URL_A, URL_B), stats, layout, PROMPT_TOKENS)
    assert url == URL_B


def test_benefit_is_normalized_so_the_weights_are_prompt_length_invariant():
    """Half a prompt cached scores 0.5 whether the prompt is 200 or 20000 tokens.

    Raw token counts would make the same (alpha, beta) a different policy at
    every prompt length, which is what makes the §5 sweep interpretable.
    """
    router = make_router(alpha=1.0, beta=0.1)
    short = router.score_endpoint(matched_tokens=100, prompt_tokens=200, load=1)
    long = router.score_endpoint(matched_tokens=10000, prompt_tokens=20000, load=1)
    assert short == long == pytest.approx(0.5 - 0.1)


def test_benefit_is_capped_at_one_full_prompt():
    """A match longer than the prompt (chunk rounding) cannot buy extra credit."""
    router = make_router(alpha=1.0, beta=0.0)
    assert router.score_endpoint(matched_tokens=300, prompt_tokens=250, load=0) == 1.0


def test_ties_are_broken_by_url_for_reproducibility():
    """Identical scores must not depend on dict or endpoint-list ordering."""
    router = make_router(alpha=1.0, beta=0.1)
    layout = {INST_A: (LOCAL, 1024), INST_B: (LOCAL, 1024)}
    stats = {URL_A: busy(in_decoding=1), URL_B: busy(in_decoding=1)}
    forward = router.select_url(endpoints(URL_A, URL_B), stats, layout, PROMPT_TOKENS)
    reversed_ = router.select_url(endpoints(URL_B, URL_A), stats, layout, PROMPT_TOKENS)
    assert forward == reversed_ == URL_A


def test_single_endpoint_is_always_the_answer():
    router = make_router(alpha=1.0, beta=1.0)
    stats = {URL_A: busy(in_decoding=50)}
    assert router.select_url(endpoints(URL_A), stats, {}, PROMPT_TOKENS) == URL_A


def test_no_endpoints_returns_none_for_the_caller_to_fall_back():
    router = make_router()
    assert router.select_url([], {}, {}, PROMPT_TOKENS) is None


def test_missing_request_stats_counts_as_no_load():
    """At cold start `request_stats` is empty for every URL — must not KeyError.

    Matches `_qps_routing`, which reads an unseen endpoint as unloaded.
    """
    router = make_router(alpha=1.0, beta=0.1)
    assert router.load_penalty({}, URL_A) == 0
    stats = {URL_A: busy(in_decoding=3)}  # URL_B never seen
    url = router.select_url(endpoints(URL_A, URL_B), stats, {}, PROMPT_TOKENS)
    assert url == URL_B


def test_load_penalty_counts_prefill_and_decode():
    router = make_router()
    stats = {URL_A: busy(in_prefill=2, in_decoding=5)}
    assert router.load_penalty(stats, URL_A) == 7


def test_holder_missing_from_the_instance_map_scores_no_benefit():
    """An instance we cannot translate to a URL cannot be routed to."""
    router = make_router(alpha=1.0, beta=0.1, mapped=False)
    router.instance_id_to_ip = {INST_B: URL_B}
    layout = {INST_A: (LOCAL, PROMPT_TOKENS)}
    stats = {URL_B: busy(in_decoding=1)}
    url = router.select_url(endpoints(URL_A, URL_B), stats, layout, PROMPT_TOKENS)
    assert url == URL_A  # both score 0 benefit; A is idle, B is not


# --- the tunables ------------------------------------------------------------


def test_default_weights_are_the_documented_ones(monkeypatch):
    monkeypatch.delenv("LOADAWARE_ALPHA", raising=False)
    monkeypatch.delenv("LOADAWARE_BETA", raising=False)
    router = make_router()
    assert router.alpha == routing_logic.DEFAULT_LOADAWARE_ALPHA == 1.0
    assert router.beta == routing_logic.DEFAULT_LOADAWARE_BETA == 0.1


def test_weights_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("LOADAWARE_ALPHA", "2.5")
    monkeypatch.setenv("LOADAWARE_BETA", "0.25")
    router = make_router()
    assert (router.alpha, router.beta) == (2.5, 0.25)


def test_explicit_weights_beat_the_environment(monkeypatch):
    monkeypatch.setenv("LOADAWARE_BETA", "0.25")
    router = make_router(beta=0.5)
    assert router.beta == 0.5


def test_zero_from_the_environment_is_honoured_not_treated_as_unset(monkeypatch):
    """beta=0 is a real sweep point (cache-only); it must not fall to the default."""
    monkeypatch.setenv("LOADAWARE_BETA", "0")
    assert make_router().beta == 0.0


def test_garbage_in_the_environment_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("LOADAWARE_BETA", "not-a-number")
    assert make_router().beta == routing_logic.DEFAULT_LOADAWARE_BETA


def test_missing_threshold_kwarg_does_not_poison_the_router():
    """`initialize_routing_logic` passes `kwargs.get(...)`, i.e. None when unset."""
    SingletonABCMeta._instances.clear()
    router = routing_logic.LoadAwareRouter(9000, SESSION_KEY, None)
    assert router.threshold == 2000


# --- end to end through route_request ---------------------------------------


def route(router, layout_info, request_stats, headers=None, replies=None, n_tokens=PROMPT_TOKENS):
    """Drive `route_request` with the Controller's answers scripted."""
    router.tokenizer = FakeTokenizer(n_tokens)
    router.kv_manager.replies = [
        LookupRetMsg(event_id="e", layout_info=layout_info)
    ] + list(replies or [])
    return run(
        router.route_request(
            endpoints(URL_A, URL_B),
            {},
            request_stats,
            FakeRequest(headers or {}),
            {"prompt": "irrelevant"},
        )
    )


def test_route_request_scores_and_routes_to_the_argmax():
    router = make_router(alpha=1.0, beta=0.1, mapped=False)
    replies = [
        QueryInstRetMsg(event_id="q", instance_id=INST_A),
        QueryInstRetMsg(event_id="q", instance_id=INST_B),
    ]
    url = route(
        router,
        {INST_A: (LOCAL, PROMPT_TOKENS)},
        {URL_A: busy(in_decoding=12), URL_B: busy()},
        replies=replies,
    )
    assert url == URL_B
    assert router.instance_id_to_ip == {INST_A: URL_A, INST_B: URL_B}


def test_the_instance_map_is_built_once_not_per_request():
    """Each miss costs an awaited round-trip per endpoint on a blocking path."""
    router = make_router(mapped=False)
    replies = [
        QueryInstRetMsg(event_id="q", instance_id=INST_A),
        QueryInstRetMsg(event_id="q", instance_id=INST_B),
    ]
    layout = {INST_A: (LOCAL, PROMPT_TOKENS)}
    route(router, layout, {}, replies=replies)
    route(router, layout, {})  # no QueryInstRetMsg queued: must not need one
    queries = [m for m in router.kv_manager.messages if isinstance(m, QueryInstMsg)]
    assert len(queries) == 2


def test_no_cached_prefix_anywhere_falls_back_to_qps():
    """Empty layout_info: there is no benefit term, so upstream's route stands."""
    router = make_router()
    url = route(router, {}, {URL_A: busy(qps=9.0), URL_B: busy(qps=1.0)})
    assert url == URL_B


def test_no_cached_prefix_with_a_session_id_falls_back_to_the_hash_ring():
    router = make_router()
    url = route(router, {}, {}, headers={SESSION_KEY: "user-42"})
    assert url in (URL_A, URL_B)
    assert url == route(router, {}, {}, headers={SESSION_KEY: "user-42"})


def test_a_null_lookup_reply_falls_back_instead_of_raising():
    """The Controller can answer None (registry empty after a router restart)."""
    router = make_router()
    router.tokenizer = FakeTokenizer()
    router.kv_manager.replies = []
    url = run(
        router.route_request(
            endpoints(URL_A, URL_B),
            {},
            {URL_A: busy(qps=5.0), URL_B: busy(qps=0.5)},
            FakeRequest({}),
            {"prompt": "irrelevant"},
        )
    )
    assert url == URL_B


def test_short_prompts_are_placed_not_dropped_to_the_qps_fallback():
    """`loadaware` does not apply `kv_aware_threshold`.

    kvaware would send this 300-token prompt down the QPS path (300 < 2000
    threshold band) even though one instance holds all of it; loadaware weighs
    the full hit against the load and keeps it on the warm instance.
    """
    router = make_router(alpha=1.0, beta=0.1, threshold=2000)
    url = route(
        router,
        {INST_A: (LOCAL, 300)},
        {URL_A: busy(qps=9.0), URL_B: busy(qps=0.1)},
        n_tokens=300,
    )
    assert url == URL_A


# --- the baseline arm must not move -----------------------------------------


def test_kvaware_still_takes_the_first_holder_and_ignores_load():
    """Regression: `KvawareRouter` is the experiment's baseline arm.

    Same fixture as `test_warm_but_loaded_loses_to_cold_but_idle`; kvaware must
    still pin the request to the saturated cache holder.
    """
    SingletonABCMeta._instances.clear()
    router = routing_logic.KvawareRouter(9000, SESSION_KEY, 2000)
    router.tokenizer = FakeTokenizer()
    router.instance_id_to_ip = {INST_A: URL_A, INST_B: URL_B}
    router.kv_manager.replies = [
        LookupRetMsg(event_id="e", layout_info={INST_A: (LOCAL, PROMPT_TOKENS)})
    ]
    url = run(
        router.route_request(
            endpoints(URL_A, URL_B),
            {},
            {URL_A: busy(in_prefill=4, in_decoding=8), URL_B: busy()},
            FakeRequest({}),
            {"prompt": "irrelevant"},
        )
    )
    assert url == URL_A


def test_kvaware_below_the_threshold_still_falls_back_to_qps():
    """The band `loadaware` drops is still in place for the baseline arm."""
    SingletonABCMeta._instances.clear()
    router = routing_logic.KvawareRouter(9000, SESSION_KEY, 2000)
    router.tokenizer = FakeTokenizer(5000)
    router.instance_id_to_ip = {INST_A: URL_A, INST_B: URL_B}
    router.kv_manager.replies = [
        LookupRetMsg(event_id="e", layout_info={INST_A: (LOCAL, 256)})
    ]
    url = run(
        router.route_request(
            endpoints(URL_A, URL_B),
            {},
            {URL_A: busy(qps=9.0), URL_B: busy(qps=0.1)},
            FakeRequest({}),
            {"prompt": "irrelevant"},
        )
    )
    assert url == URL_B


# --- wiring ------------------------------------------------------------------


def test_loadaware_is_a_routing_logic_value():
    assert routing_logic.RoutingLogic.LOADAWARE == "loadaware"
    assert routing_logic.RoutingLogic.KVAWARE == "kvaware"


def test_the_factory_builds_a_loadaware_router(monkeypatch):
    started = []
    monkeypatch.setattr(
        routing_logic.LoadAwareRouter,
        "start_kv_manager",
        lambda self: started.append(self),
    )
    SingletonABCMeta._instances.clear()
    router = routing_logic.initialize_routing_logic(
        routing_logic.RoutingLogic.LOADAWARE,
        lmcache_controller_port=9000,
        session_key=SESSION_KEY,
        loadaware_beta=0.3,
    )
    assert isinstance(router, routing_logic.LoadAwareRouter)
    assert router.beta == 0.3
    assert started == [router]


def test_the_registry_finds_and_cleans_up_a_loadaware_router():
    """`get_routing_logic`/`cleanup_routing_logic` walk a hard-coded class list."""
    router = make_router()
    assert routing_logic.get_routing_logic() is router
    routing_logic.cleanup_routing_logic()
    with pytest.raises(ValueError):
        routing_logic.get_routing_logic()

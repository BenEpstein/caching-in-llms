"""Unit tests for the `loadaware` placement policy (run: pytest tests/).

Change 2 of the project: the router must place a request by
``cache_hit_benefit - beta * relative_load`` over *every* instance, instead of
kvaware's "first instance reported to hold the prefix". See
docs/report/report.md, section "Change 2", README.md ("Our changes"), and issue #5.

Both terms are dimensionless - a fraction of this prompt, and a fraction of
this fleet's mean load - so ``beta`` carries no unit from the deployment. The
tests below pin that property directly (see the scale-invariance and
fleet-size cases), because it is the whole reason the policy ships a default
instead of a per-cluster calibration.

No cluster, no GPU, no vllm_router install - conftest.py stubs the router's
import surface and loads the tracked patch file itself, so the bytes under test
are the bytes `deploy/dev/apply-router-patch.sh` mounts into the pod.
"""

import ast
import asyncio

import pytest
from conftest import (
    PARSER_PATCH_FILE,
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
RESTARTED_A = "instance-a2"
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


def make_router(beta=None, threshold=2000, mapped=True):
    """A LoadAwareRouter with the Controller and tokenizer faked out.

    `RoutingInterface` is a singleton, so the registry is cleared first: a test
    that builds a second router with a different beta would otherwise silently
    get the first one back.
    """
    SingletonABCMeta._instances.clear()
    router = routing_logic.LoadAwareRouter(9000, SESSION_KEY, threshold, beta)
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
    router = make_router(beta=0.1)
    stats = {URL_A: busy(in_decoding=4), URL_B: busy(in_decoding=1)}
    url = router.select_url(endpoints(URL_A, URL_B), stats, {}, PROMPT_TOKENS)
    assert url == URL_B


def test_equally_idle_picks_the_warmest_instance():
    """No load anywhere: the score is pure cache-hit benefit."""
    router = make_router(beta=0.1)
    layout = {INST_A: (LOCAL, 512), INST_B: (LOCAL, 2048)}
    url = router.select_url(endpoints(URL_A, URL_B), {}, layout, PROMPT_TOKENS)
    assert url == URL_B


def test_warm_but_loaded_loses_to_cold_but_idle():
    """The whole point of the policy: a lopsided fleet outweighs a full hit.

    loads are 12 and 0, so mean = 6 and the relative loads are +1.0 and -1.0.

    benefit(A) = 2048/2048 = 1.0, rel(A) = +1.0  ->  1.0 - 1.0*(+1.0) =  0.0
    benefit(B) = 0,               rel(B) = -1.0  ->  0.0 - 1.0*(-1.0) = +1.0
    """
    router = make_router(beta=1.0)
    layout = {INST_A: (LOCAL, PROMPT_TOKENS)}
    stats = {URL_A: busy(in_prefill=4, in_decoding=8), URL_B: busy()}
    url = router.select_url(endpoints(URL_A, URL_B), stats, layout, PROMPT_TOKENS)
    assert url == URL_B


def test_warm_but_loaded_wins_when_beta_is_small():
    """Same fixture, smaller beta: the crossover moves. This is the sweep axis.

    1.0 - 0.01*(+1.0) = 0.99  >  0.0 - 0.01*(-1.0) = 0.01
    """
    router = make_router(beta=0.01)
    layout = {INST_A: (LOCAL, PROMPT_TOKENS)}
    stats = {URL_A: busy(in_prefill=4, in_decoding=8), URL_B: busy()}
    url = router.select_url(endpoints(URL_A, URL_B), stats, layout, PROMPT_TOKENS)
    assert url == URL_A


def test_beta_zero_is_cache_only_placement():
    """beta = 0 degenerates to "most cached wins", however loaded it is."""
    router = make_router(beta=0.0)
    layout = {INST_A: (LOCAL, PROMPT_TOKENS)}
    stats = {URL_A: busy(in_decoding=100), URL_B: busy()}
    url = router.select_url(endpoints(URL_A, URL_B), stats, layout, PROMPT_TOKENS)
    assert url == URL_A


def test_the_same_beta_is_the_same_policy_at_ten_times_the_load():
    """Scale invariance - the property that lets `beta` ship a default.

    Absolute counts do not have this. With `beta * load` and beta tuned where
    the fleet ran 4-and-1, the same beta on a fleet running 40-and-10 charges a
    penalty ten times larger against a benefit term that is still capped at
    1.0, and placement silently collapses to least-loaded. Relative load reads
    both fleets as +0.6 / -0.6, so the decision is the same one.
    """
    router = make_router(beta=0.5)
    layout = {INST_A: (LOCAL, PROMPT_TOKENS)}
    small = {URL_A: busy(in_decoding=4), URL_B: busy(in_decoding=1)}
    large = {URL_A: busy(in_decoding=400), URL_B: busy(in_decoding=100)}
    assert router.relative_loads(small, endpoints(URL_A, URL_B)) == pytest.approx(
        router.relative_loads(large, endpoints(URL_A, URL_B))
    )
    assert router.select_url(
        endpoints(URL_A, URL_B), small, layout, PROMPT_TOKENS
    ) == router.select_url(endpoints(URL_A, URL_B), large, layout, PROMPT_TOKENS)


def test_relative_load_is_measured_against_the_whole_fleet_not_a_pair():
    """Four engines: the mean is the fleet's, so one hot engine is +3.0, not +1.0.

    A pairwise or max-minus-min reading would rate C the same in a 2-engine and
    a 4-engine fleet. It is not the same: three idle peers make one busy engine
    further from average, and the policy should divert harder.
    """
    urls = [f"http://10.0.0.{i}:8000" for i in (1, 2, 3, 4)]
    router = make_router(beta=1.0)
    stats = {urls[0]: busy(), urls[1]: busy(), urls[2]: busy(in_decoding=8), urls[3]: busy()}
    relative = router.relative_loads(stats, endpoints(*urls))
    assert relative[urls[2]] == pytest.approx(3.0)  # (8 - 2) / 2
    assert relative[urls[0]] == pytest.approx(-1.0)  # (0 - 2) / 2
    assert sum(relative.values()) == pytest.approx(0.0)  # deviations cancel


def test_a_near_idle_fleet_reports_no_imbalance_to_act_on():
    """The denominator is clamped at 1: one request is not a 400% overload.

    mean load here is 0.25, and an unclamped `(load - mean) / mean` would read
    the single in-flight request as +3.0 - enough at the default beta to divert
    a full cache hit on a fleet doing essentially nothing.
    """
    urls = [f"http://10.0.0.{i}:8000" for i in (1, 2, 3, 4)]
    router = make_router(beta=1.0)
    relative = router.relative_loads({urls[0]: busy(in_decoding=1)}, endpoints(*urls))
    assert relative[urls[0]] == pytest.approx(0.75)  # (1 - 0.25) / max(1, 0.25)
    layout = {INST_A: (LOCAL, PROMPT_TOKENS)}
    stats = {URL_A: busy(in_decoding=1), URL_B: busy()}
    url = router.select_url(endpoints(URL_A, URL_B), stats, layout, PROMPT_TOKENS)
    assert url == URL_A  # full hit survives a one-request imbalance


def test_benefit_is_normalized_so_the_weights_are_prompt_length_invariant():
    """Half a prompt cached scores 0.5 whether the prompt is 200 or 20000 tokens.

    Raw token counts would make the same beta a different policy at every
    prompt length, which is what makes the §5 sweep interpretable.
    """
    router = make_router(beta=0.1)
    short = router.score_endpoint(matched_tokens=100, prompt_tokens=200, relative_load=1)
    long = router.score_endpoint(
        matched_tokens=10000, prompt_tokens=20000, relative_load=1
    )
    assert short == long == pytest.approx(0.5 - 0.1)


def test_benefit_is_capped_at_one_full_prompt():
    """A match longer than the prompt (chunk rounding) cannot buy extra credit."""
    router = make_router(beta=0.0)
    assert (
        router.score_endpoint(matched_tokens=300, prompt_tokens=250, relative_load=0)
        == 1.0
    )


def test_ties_are_broken_by_url_for_reproducibility():
    """Identical scores must not depend on dict or endpoint-list ordering."""
    router = make_router(beta=0.1)
    layout = {INST_A: (LOCAL, 1024), INST_B: (LOCAL, 1024)}
    stats = {URL_A: busy(in_decoding=1), URL_B: busy(in_decoding=1)}
    forward = router.select_url(endpoints(URL_A, URL_B), stats, layout, PROMPT_TOKENS)
    reversed_ = router.select_url(endpoints(URL_B, URL_A), stats, layout, PROMPT_TOKENS)
    assert forward == reversed_ == URL_A


def test_single_endpoint_is_always_the_answer():
    router = make_router(beta=1.0)
    stats = {URL_A: busy(in_decoding=50)}
    assert router.select_url(endpoints(URL_A), stats, {}, PROMPT_TOKENS) == URL_A


def test_no_endpoints_returns_none_for_the_caller_to_fall_back():
    router = make_router()
    assert router.select_url([], {}, {}, PROMPT_TOKENS) is None


def test_missing_request_stats_counts_as_no_load():
    """At cold start `request_stats` is empty for every URL - must not KeyError.

    Matches `_qps_routing`, which reads an unseen endpoint as unloaded.
    """
    router = make_router(beta=0.1)
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
    router = make_router(beta=0.1, mapped=False)
    router.instance_id_to_ip = {INST_B: URL_B}
    layout = {INST_A: (LOCAL, PROMPT_TOKENS)}
    stats = {URL_B: busy(in_decoding=1)}
    url = router.select_url(endpoints(URL_A, URL_B), stats, layout, PROMPT_TOKENS)
    assert url == URL_A  # both score 0 benefit; A is idle, B is not


# --- the tunables ------------------------------------------------------------


def test_the_default_beta_is_the_documented_one(monkeypatch):
    """beta = 1.0: 100% above fleet-average load costs one full cache hit."""
    monkeypatch.delenv("LOADAWARE_BETA", raising=False)
    router = make_router()
    assert router.beta == routing_logic.DEFAULT_LOADAWARE_BETA == 1.0


def test_there_is_no_alpha_because_only_the_ratio_was_ever_free():
    """An argmax is invariant under positive scaling, so alpha was redundant.

    Kept as a test rather than a comment: re-adding alpha would reintroduce a
    parameter that cannot change any placement decision, and the §5 sweep would
    then have a second axis that is pure noise.
    """
    assert not hasattr(routing_logic, "DEFAULT_LOADAWARE_ALPHA")
    assert not hasattr(make_router(), "alpha")


def test_beta_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("LOADAWARE_BETA", "0.25")
    assert make_router().beta == 0.25


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
    """End to end at the default beta: 12-vs-0 in flight outweighs a full hit.

    At beta = 0.1 it would not, and the request would stay on the warm engine
    (see `test_warm_but_loaded_wins_when_beta_is_small`) - the crossover is the
    sweep axis, not an accident of this fixture.
    """
    router = make_router(beta=1.0, mapped=False)
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


def test_an_engine_restart_refreshes_the_bridge_instead_of_scoring_it_cold():
    """A restarted engine registers under a **new** instance_id.

    The bridge only ever grows, so a count-of-entries guard would never notice
    and every holder would read as unmapped - placement silently degenerating
    to least-loaded for the life of the router, an invalidated run rather than
    a visible failure.
    """
    router = make_router(beta=0.1, mapped=False)
    first_boot = [
        QueryInstRetMsg(event_id="q", instance_id=INST_A),
        QueryInstRetMsg(event_id="q", instance_id=INST_B),
    ]
    route(router, {INST_A: (LOCAL, PROMPT_TOKENS)}, {}, replies=first_boot)

    restarted = RESTARTED_A
    after_restart = [
        QueryInstRetMsg(event_id="q", instance_id=restarted),
        QueryInstRetMsg(event_id="q", instance_id=INST_B),
    ]
    url = route(
        router,
        {restarted: (LOCAL, PROMPT_TOKENS)},
        {URL_B: busy(in_decoding=3)},
        replies=after_restart,
    )
    assert url == URL_A  # warm and idle; a cold read would have picked B anyway
    assert router.instance_id_to_ip[restarted] == URL_A
    queries = [m for m in router.kv_manager.messages if isinstance(m, QueryInstMsg)]
    assert len(queries) == 4  # one round per boot, not one per request


def test_the_live_instance_id_wins_when_two_ids_share_a_url():
    """After a restart the bridge holds both ids; the fresh one carries the credit.

    `refresh_instance_map` appends ids as it learns them, so the last id written
    for a URL is the live one.
    """
    router = make_router(beta=0.1)
    router.instance_id_to_ip = {INST_A: URL_A, RESTARTED_A: URL_A, INST_B: URL_B}
    layout = {INST_A: (LOCAL, 256), RESTARTED_A: (LOCAL, PROMPT_TOKENS)}
    assert router.matched_tokens_by_url(layout) == {URL_A: PROMPT_TOKENS}
    url = router.select_url(endpoints(URL_A, URL_B), {}, layout, PROMPT_TOKENS)
    assert url == URL_A


def test_a_dead_instance_id_earns_no_phantom_credit():
    """The restarted engine came back with an empty cache.

    The Controller's `kv_pool` only drops an instance on an explicit deregister,
    so `lookup()` can still name the dead id as a holder of a full prefix. That
    match no longer exists anywhere - crediting it would route the request to a
    cold engine on the strength of a cache that died with the old process.
    """
    router = make_router(beta=0.1)
    router.instance_id_to_ip = {INST_A: URL_A, RESTARTED_A: URL_A, INST_B: URL_B}
    layout = {INST_A: (LOCAL, PROMPT_TOKENS)}  # only the dead id reports
    assert router.matched_tokens_by_url(layout) == {}
    url = router.select_url(
        endpoints(URL_A, URL_B), {URL_A: busy(in_decoding=1)}, layout, PROMPT_TOKENS
    )
    assert url == URL_B  # both cold; B is the idle one


def test_an_unmapped_endpoint_makes_the_bridge_stale():
    """A URL we cannot translate would score 0 benefit whatever it holds."""
    router = make_router(mapped=False)
    router.instance_id_to_ip = {INST_A: URL_A}
    assert router.instance_map_is_stale(endpoints(URL_A, URL_B), {}) is True
    assert router.instance_map_is_stale(endpoints(URL_A), {}) is False


def test_a_fully_mapped_bridge_with_known_holders_is_not_stale():
    router = make_router()
    layout = {INST_A: (LOCAL, PROMPT_TOKENS)}
    assert router.instance_map_is_stale(endpoints(URL_A, URL_B), layout) is False


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
    router = make_router(beta=0.1, threshold=2000)
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


# --- the CLI must accept the new value ---------------------------------------


def routing_logic_choices():
    """The literal `choices=[...]` of `--routing-logic` in the tracked parser.

    Read with `ast` rather than by importing: `parser.py` pulls in the router's
    whole config surface, and only this one list is under test.
    """
    tree = ast.parse(PARSER_PATCH_FILE.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and first.value == "--routing-logic"):
            continue
        for keyword in node.keywords:
            if keyword.arg == "choices":
                return [ast.literal_eval(element) for element in keyword.value.elts]
    raise AssertionError("no --routing-logic argument with choices= in parser.py")


def test_the_cli_accepts_every_routing_logic_value():
    """`choices` is a hard-coded literal list, *not* derived from the enum.

    So adding `LOADAWARE` to `RoutingLogic` is not enough on its own: argparse
    would reject `--routing-logic loadaware` and the router would exit before
    the factory is ever reached. Both files have to move together, in either
    direction - hence the set equality.
    """
    assert set(routing_logic_choices()) == {
        member.value for member in routing_logic.RoutingLogic
    }

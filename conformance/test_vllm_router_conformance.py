"""Conformance: the vllm_router-side test doubles vs the real package (#65).

Same idea as test_lmcache_conformance.py, other half of tests/conftest.py: the
offline routing tests stub `EndpointInfo`, `RequestStats`, `EngineStats` and
`SingletonABCMeta` out of `vllm_router`. Here the real package - production-stack
at the pinned commit, installed editable by the workflow - is asserted against
what those stubs declare, and the patched `routing_logic.py` is loaded with the
real modules (no stubs) to score real `RequestStats`/`EndpointInfo` objects
through `select_url`, which upstream's own tests never do (they don't know
`loadaware` exists).

Field checks are prefix checks, not equality: the stubs declare only the fields
the code under test reads or the tests construct, and upstream's dataclasses
grow trailing optional fields over time (`EndpointInfo` already has
`service_name`, `namespace`, `model_info` beyond the stub). A prefix in the
same positional order is exactly what positional construction and attribute
reads rely on.

CI-only, like the rest of conformance/ - see the module docstring next door.
"""

import importlib.util

import pytest
from conftest import REPO, stub_fields

vllm_router = pytest.importorskip(
    "vllm_router", reason="conformance needs the real vllm_router install (CI-only)"
)

if getattr(vllm_router, "__file__", None) is None:
    pytest.skip(
        "sys.modules holds the offline stubs from tests/conftest.py, not the "
        "real package - run `pytest conformance/` in its own process",
        allow_module_level=True,
    )

pytest.importorskip("lmcache", reason="the patched routing_logic imports lmcache")

from dataclasses import fields as dataclass_fields  # noqa: E402

from vllm_router.service_discovery import EndpointInfo  # noqa: E402
from vllm_router.stats.engine_stats import EngineStats  # noqa: E402
from vllm_router.stats.request_stats import RequestStats  # noqa: E402
from vllm_router.utils import SingletonABCMeta  # noqa: E402

ROUTING_PATCH_FILE = REPO / "patches/vllm_router/routers/routing_logic.py"

#: Free high port for the real LMCacheControllerManager's ZMQ bind - the one
#: side effect of constructing the router that cannot be avoided.
ZMQ_PORT = 19473

LOCAL = "LocalCPUBackend"
URL_A, URL_B = "http://engine-a:8000", "http://engine-b:8000"
INST_A, INST_B = "instance-a", "instance-b"
PROMPT_TOKENS = 2048


# --- 1. the stub dataclasses are positional prefixes of the real ones ---------


@pytest.mark.parametrize("real", [EndpointInfo, RequestStats, EngineStats])
def test_stub_fields_are_a_positional_prefix_of_the_real_class(real):
    expected = stub_fields(real.__name__)
    actual = [f.name for f in dataclass_fields(real)][: len(expected)]
    assert actual == expected, (
        f"tests/conftest.py declares {real.__name__}({', '.join(expected)}) but the "
        f"real class starts ({', '.join(actual)})"
    )


def test_singleton_metaclass_behaves_as_the_stub_claims():
    """The stub copies SingletonABCMeta 'verbatim'; hold the real one to the
    same three behaviours the offline tests lean on: per-class instance reuse,
    independence between classes, and `create=False` returning None before the
    first construction."""

    class A(metaclass=SingletonABCMeta):
        def __init__(self, create=True):
            pass

    class B(metaclass=SingletonABCMeta):
        def __init__(self, create=True):
            pass

    try:
        assert A(create=False) is None
        a1, a2 = A(), A()
        assert a1 is a2
        assert A(create=False) is a1
        assert B() is not a1
    finally:
        SingletonABCMeta._instances.pop(A, None)
        SingletonABCMeta._instances.pop(B, None)


# --- 2. the patched routing_logic against the real package, no stubs ----------


@pytest.fixture(scope="module")
def routing_logic():
    spec = importlib.util.spec_from_file_location(
        "conformance_patched_routing_logic", ROUTING_PATCH_FILE
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def router(routing_logic):
    # Real KvawareRouter.__init__ constructs a real LMCacheControllerManager,
    # which binds a ZMQ PULL socket on ZMQ_PORT - construct once per module.
    SingletonABCMeta._instances.clear()
    router = routing_logic.LoadAwareRouter(ZMQ_PORT, "x-user-id", 2000, 1.0)
    router.instance_id_to_ip = {INST_A: URL_A, INST_B: URL_B}
    yield router
    SingletonABCMeta._instances.clear()


def test_patch_imports_the_real_vllm_router(routing_logic):
    assert routing_logic.EndpointInfo is EndpointInfo
    assert routing_logic.RequestStats is RequestStats
    assert type(routing_logic.RoutingInterface) is SingletonABCMeta


def real_endpoints():
    return [
        EndpointInfo(url, ["test-model"], f"ep-{i}", 0.0, "llm", False)
        for i, url in enumerate((URL_A, URL_B))
    ]


def real_stats(in_prefill=0, in_decoding=0):
    """The real RequestStats has no defaults; construct it in full, as the
    router's stats monitor does in production."""
    return RequestStats(
        qps=0.0,
        ttft=0.0,
        in_prefill_requests=in_prefill,
        in_decoding_requests=in_decoding,
        finished_requests=0,
        uptime=0,
        avg_decoding_length=0.0,
        avg_latency=0.0,
        avg_itl=-1.0,
        num_swapped_requests=0,
    )


def test_load_is_read_off_the_real_request_stats(router):
    """The one field-read the offline suite can't prove: `load_penalty` sums
    `in_prefill_requests + in_decoding_requests` on the real dataclass."""
    stats = {URL_A: real_stats(in_prefill=4, in_decoding=8)}
    assert router.load_penalty(stats, URL_A) == 12
    assert router.load_penalty(stats, URL_B) == 0


def test_warm_but_loaded_loses_to_cold_but_idle_on_real_classes(router):
    """tests/test_loadaware_routing.py's headline scenario, real classes only:
    at beta=1.0, a full cache hit on a +100%-loaded engine scores 0.0 and the
    idle cold engine scores +1.0."""
    layout = {INST_A: (LOCAL, PROMPT_TOKENS)}
    stats = {URL_A: real_stats(in_prefill=4, in_decoding=8), URL_B: real_stats()}
    assert router.select_url(real_endpoints(), stats, layout, PROMPT_TOKENS) == URL_B


def test_equally_idle_picks_the_warmest_instance_on_real_classes(router):
    layout = {INST_A: (LOCAL, 512), INST_B: (LOCAL, PROMPT_TOKENS)}
    assert router.select_url(real_endpoints(), {}, layout, PROMPT_TOKENS) == URL_B

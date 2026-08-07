"""Load the two patched router files offline, with no lmcache/vllm_router install.

The router runs Python 3.12 with lmcache 0.3.9post2 and vllm_router installed;
a laptop has neither. Rather than re-typing the algorithms into the tests (which
would test a copy, not the deliverable), we stub the modules that
`patches/.../kv_controller.py` and `patches/.../routing_logic.py` import and
then load *those exact files* by path. The bytes under test are the bytes
`deploy/dev/apply-router-patch.sh` mounts into the pod.

Stub surface, read from the running router pod 2026-08-01:
  lmcache.logging.init_logger
  lmcache.v1.cache_controller.message.*  (20 message classes)
  lmcache.v1.cache_controller.controller_manager.LMCacheControllerManager
  lmcache.v1.token_database.ChunkedTokenDatabase
  vllm_router.log / .service_discovery / .stats.* / .utils
  requests, fastapi.Request, uhashring.HashRing  (not test dependencies)
"""

import abc
import importlib.util
import logging
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

PATCHES = Path(__file__).resolve().parents[1] / "patches"

PATCH_FILE = PATCHES / "lmcache/v1/cache_controller/controllers/kv_controller.py"

ROUTING_PATCH_FILE = PATCHES / "vllm_router/routers/routing_logic.py"

PARSER_PATCH_FILE = PATCHES / "vllm_router/parsers/parser.py"


# --- the message classes kv_controller actually constructs or reads -----------
# Field names and types are verbatim from lmcache/v1/cache_controller/message.py
# in the pinned router image.


@dataclass
class KVAdmitMsg:
    instance_id: str
    worker_id: int
    key: int
    location: str


@dataclass
class KVEvictMsg:
    instance_id: str
    worker_id: int
    key: int
    location: str


@dataclass
class LookupMsg:
    event_id: str
    tokens: List[int]


@dataclass
class LookupRetMsg:
    event_id: str
    layout_info: Dict[str, Tuple[str, int]]


@dataclass
class BatchedP2PLookupRetMsg:
    layout_info: List[Tuple[str, str, int, str]] = field(default_factory=list)


@dataclass
class QueryInstMsg:
    """Router -> Controller: "which instance serves this engine ip?"."""

    event_id: str
    ip: str


@dataclass
class QueryInstRetMsg:
    event_id: str
    instance_id: str


#: Imported by kv_controller but never constructed in the code paths under test.
_UNUSED_MESSAGES = (
    "BatchedP2PLookupMsg",
    "CheckFinishMsg",
    "CheckFinishRetMsg",
    "ClearMsg",
    "ClearRetMsg",
    "CompressMsg",
    "CompressRetMsg",
    "DecompressMsg",
    "DecompressRetMsg",
    "MoveMsg",
    "MoveRetMsg",
    "PinMsg",
    "PinRetMsg",
)


class ChunkedTokenDatabase:
    """Placeholder for the real chunker.

    `KVController.__init__` constructs one, so it must exist and take no
    arguments. Every test replaces `controller.token_database` with
    `PrefixHashTokenDatabase` (see test_kv_controller_lookup.py), which
    reimplements the real `process_tokens` contract deterministically.
    """

    def process_tokens(self, *args, **kwargs):  # pragma: no cover - never called
        raise NotImplementedError(
            "tests must inject a PrefixHashTokenDatabase; the real chunker "
            "needs a torch install (conformance/ runs it in CI)"
        )


class FakeControllerManager:
    """Placeholder for `LMCacheControllerManager` (ZMQ sockets in the real one).

    `KvawareRouter.__init__` constructs one with a socket-address dict, and
    `query_manager()` awaits `handle_orchestration_message`. Tests set
    `router.kv_manager.replies` to the messages the Controller should return.
    """

    def __init__(self, sockets=None):
        self.sockets = sockets
        self.replies = []
        self.messages = []

    async def handle_orchestration_message(self, msg):
        self.messages.append(msg)
        return self.replies.pop(0) if self.replies else None

    async def start_all(self):  # pragma: no cover - never started in tests
        raise NotImplementedError


def _install_lmcache_stubs() -> None:
    """Put fake `lmcache.*` modules in sys.modules before the patch is loaded."""
    message = types.ModuleType("lmcache.v1.cache_controller.message")
    for cls in (
        KVAdmitMsg,
        KVEvictMsg,
        LookupMsg,
        LookupRetMsg,
        BatchedP2PLookupRetMsg,
        QueryInstMsg,
        QueryInstRetMsg,
    ):
        setattr(message, cls.__name__, cls)
    for name in _UNUSED_MESSAGES:
        setattr(message, name, type(name, (), {}))

    token_database = types.ModuleType("lmcache.v1.token_database")
    token_database.ChunkedTokenDatabase = ChunkedTokenDatabase

    lmcache_logging = types.ModuleType("lmcache.logging")
    lmcache_logging.init_logger = logging.getLogger

    # `routing_logic.py` constructs an LMCacheControllerManager in
    # `KvawareRouter.__init__`; the real one opens ZMQ sockets.
    controller_manager = types.ModuleType(
        "lmcache.v1.cache_controller.controller_manager"
    )
    controller_manager.LMCacheControllerManager = FakeControllerManager

    cache_controller = types.ModuleType("lmcache.v1.cache_controller")
    cache_controller.controller_manager = controller_manager
    cache_controller.message = message

    modules = {
        "lmcache": types.ModuleType("lmcache"),
        "lmcache.logging": lmcache_logging,
        "lmcache.v1": types.ModuleType("lmcache.v1"),
        "lmcache.v1.cache_controller": cache_controller,
        "lmcache.v1.cache_controller.controller_manager": controller_manager,
        "lmcache.v1.cache_controller.message": message,
        "lmcache.v1.token_database": token_database,
    }
    for name, module in modules.items():
        sys.modules.setdefault(name, module)


def _load_patched_module() -> types.ModuleType:
    _install_lmcache_stubs()
    spec = importlib.util.spec_from_file_location("patched_kv_controller", PATCH_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


#: The patched controller module, loaded from the tracked file on disk.
kv_controller = _load_patched_module()


# --- routing_logic.py: the second patched file ------------------------------
# Same trick, bigger import surface. `vllm_router` is only installed in the
# router image, and `requests`/`fastapi`/`uhashring` are not test dependencies
# (requirements.txt is httpx + pytest), so all of them get stubbed.


@dataclass
class EndpointInfo:
    """Verbatim field order from vllm_router/service_discovery.py."""

    url: str
    model_names: List[str] = field(default_factory=lambda: ["test-model"])
    Id: str = "endpoint"
    added_timestamp: float = 0.0
    model_label: str = "llm"
    sleep: bool = False
    pod_name: str = None


@dataclass
class EngineStats:
    """Scraped stats. `loadaware` ignores these (stale); shape only.

    Field names are a positional prefix of the real class - conformance/
    holds this to the pinned upstream (a `gpu_cache_hit_rate` misnaming
    was caught exactly that way, #65)."""

    num_running_requests: int = 0
    num_queuing_requests: int = 0
    gpu_prefix_cache_hit_rate: float = 0.0


@dataclass
class RequestStats:
    """Verbatim from vllm_router/stats/request_stats.py in the pinned image."""

    qps: float = 0.0
    ttft: float = 0.0
    in_prefill_requests: int = 0
    in_decoding_requests: int = 0
    finished_requests: int = 0
    uptime: int = 0
    avg_decoding_length: float = 0.0
    avg_latency: float = 0.0
    avg_itl: float = -1.0
    num_swapped_requests: int = 0


class SingletonABCMeta(abc.ABCMeta):
    """Verbatim from vllm_router/utils.py in the pinned router image."""

    _instances = {}

    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            if kwargs.get("create") is False:
                return None
            instance = super().__call__(*args, **kwargs)
            cls._instances[cls] = instance
        return cls._instances[cls]


class HashRing:
    """Deterministic stand-in for `uhashring.HashRing`.

    Only the four methods the routers call. `get_node` picks by a stable hash
    of the key over the sorted nodes, which is enough to assert "the session
    fallback returns one of the endpoints, and the same one every time".
    """

    def __init__(self):
        self._nodes = []

    def add_node(self, node):
        if node not in self._nodes:
            self._nodes.append(node)

    def remove_node(self, node):
        if node in self._nodes:
            self._nodes.remove(node)

    def get_nodes(self):
        return list(self._nodes)

    def get_node(self, key):
        if not self._nodes:
            return None
        ordered = sorted(self._nodes)
        return ordered[sum(ord(c) for c in str(key)) % len(ordered)]


class FakeRequest:
    """Stands in for `fastapi.Request` — only `.headers.get` is ever called."""

    def __init__(self, headers=None):
        self.headers = headers or {}


def _install_router_stubs() -> None:
    """Put fake `vllm_router.*` / third-party modules in sys.modules."""
    requests_stub = types.ModuleType("requests")

    def _post(*_args, **_kwargs):  # pragma: no cover - remote tokenize fallback
        raise AssertionError("tests must not reach the remote /tokenize endpoint")

    requests_stub.post = _post

    fastapi = types.ModuleType("fastapi")
    fastapi.Request = FakeRequest

    uhashring = types.ModuleType("uhashring")
    uhashring.HashRing = HashRing

    router_log = types.ModuleType("vllm_router.log")
    router_log.init_logger = logging.getLogger

    service_discovery = types.ModuleType("vllm_router.service_discovery")
    service_discovery.EndpointInfo = EndpointInfo

    engine_stats = types.ModuleType("vllm_router.stats.engine_stats")
    engine_stats.EngineStats = EngineStats

    request_stats = types.ModuleType("vllm_router.stats.request_stats")
    request_stats.RequestStats = RequestStats

    utils = types.ModuleType("vllm_router.utils")
    utils.SingletonABCMeta = SingletonABCMeta

    modules = {
        "requests": requests_stub,
        "fastapi": fastapi,
        "uhashring": uhashring,
        "vllm_router": types.ModuleType("vllm_router"),
        "vllm_router.log": router_log,
        "vllm_router.service_discovery": service_discovery,
        "vllm_router.stats": types.ModuleType("vllm_router.stats"),
        "vllm_router.stats.engine_stats": engine_stats,
        "vllm_router.stats.request_stats": request_stats,
        "vllm_router.utils": utils,
    }
    for name, module in modules.items():
        sys.modules.setdefault(name, module)


def _load_patched_routing_logic() -> types.ModuleType:
    _install_lmcache_stubs()
    _install_router_stubs()
    spec = importlib.util.spec_from_file_location(
        "patched_routing_logic", ROUTING_PATCH_FILE
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


#: The patched router module, loaded from the tracked file on disk.
routing_logic = _load_patched_routing_logic()


@pytest.fixture(autouse=True)
def _clear_router_singletons():
    """`RoutingInterface` is a singleton — one test's router must not leak."""
    SingletonABCMeta._instances.clear()
    yield
    SingletonABCMeta._instances.clear()

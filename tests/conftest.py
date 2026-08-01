"""Load the patched LMCache controller offline, with no lmcache install.

The router runs Python 3.12 with lmcache 0.3.9post2 installed; a laptop has
neither. Rather than re-typing the algorithm into the test (which would test a
copy, not the deliverable), we stub the handful of `lmcache.*` modules that
`patches/.../kv_controller.py` imports and then load *that exact file* by path.
The bytes under test are the bytes `deploy/dev/apply-router-patch.sh` mounts
into the pod.

Stub surface, read from the running router pod 2026-08-01:
  lmcache.logging.init_logger
  lmcache.v1.cache_controller.message.*  (18 message classes)
  lmcache.v1.token_database.ChunkedTokenDatabase
"""

import importlib.util
import logging
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

PATCH_FILE = (
    Path(__file__).resolve().parents[1]
    / "patches/lmcache/v1/cache_controller/controllers/kv_controller.py"
)


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
            "needs torch and an lmcache config"
        )


def _install_lmcache_stubs() -> None:
    """Put fake `lmcache.*` modules in sys.modules before the patch is loaded."""
    message = types.ModuleType("lmcache.v1.cache_controller.message")
    for cls in (
        KVAdmitMsg,
        KVEvictMsg,
        LookupMsg,
        LookupRetMsg,
        BatchedP2PLookupRetMsg,
    ):
        setattr(message, cls.__name__, cls)
    for name in _UNUSED_MESSAGES:
        setattr(message, name, type(name, (), {}))

    token_database = types.ModuleType("lmcache.v1.token_database")
    token_database.ChunkedTokenDatabase = ChunkedTokenDatabase

    lmcache_logging = types.ModuleType("lmcache.logging")
    lmcache_logging.init_logger = logging.getLogger

    modules = {
        "lmcache": types.ModuleType("lmcache"),
        "lmcache.logging": lmcache_logging,
        "lmcache.v1": types.ModuleType("lmcache.v1"),
        "lmcache.v1.cache_controller": types.ModuleType("lmcache.v1.cache_controller"),
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

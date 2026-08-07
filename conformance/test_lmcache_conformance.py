"""Conformance suite: prove the offline test doubles match the real lmcache (#65).

The offline suites (`pytest benchmarks/ tests/`) load the tracked patch files
against stubs (`tests/conftest.py`) and a hand-written chunker
(`PrefixHashTokenDatabase` in `tests/test_kv_controller_lookup.py`). They prove
the algorithms, not the fakes' fidelity: if a stub misdeclares a field or the
chunker contract, every offline test passes and the patch is wrong in
production. This suite closes that gap against the real
``lmcache==0.3.9.post2`` - the exact version in the measured router image:

1. the message classes carry the fields ``tests/conftest.py`` declares, in the
   same positional order (the stubs are dataclasses, the real ones msgspec
   Structs; both take positional args in field order, so order is part of the
   contract). The expected surface is read out of ``tests/conftest.py`` by AST
   parse - no duplication, and no import of the stub-installing conftest;
2. the real ``ChunkedTokenDatabase`` honours the ``process_tokens`` contract
   ``PrefixHashTokenDatabase`` reimplements: constructible with no arguments
   (``KVController.__init__`` does exactly that), chunks of ``chunk_size`` with
   the short final chunk kept, exclusive ``end`` clamped to the prompt length,
   ``make_key=False`` yielding raw int hashes, and the rolling *prefix* hash
   property (same prefix => same keys, divergence => different keys from the
   divergent chunk onward);
3. the patched ``kv_controller.py`` imports and runs against the real package
   with no stubs, and multi-instance lookup behaves on the real chunker
   exactly as the offline suite says it does.

CI-only: neither lmcache nor its torch dependency installs on macOS, so this
directory is deliberately NOT part of ``pytest benchmarks/ tests/`` - the
offline suites must stay laptop-runnable with no install. The
``upstream-conformance`` workflow runs this on ubuntu-latest; see
``.github/workflows/upstream-conformance.yml``.
"""

import asyncio
import importlib.util

import pytest
from stub_surface import REPO, stub_fields, stubbed_message_names, unused_message_names

lmcache = pytest.importorskip(
    "lmcache", reason="conformance needs the real lmcache install (CI-only)"
)

if getattr(lmcache, "__file__", None) is None:
    pytest.skip(
        "sys.modules holds the offline stubs from tests/conftest.py, not the "
        "real package - run `pytest conformance/` in its own process",
        allow_module_level=True,
    )

from lmcache.v1.cache_controller import message as real_message  # noqa: E402
from lmcache.v1.token_database import ChunkedTokenDatabase  # noqa: E402

PATCH_FILE = REPO / "patches/lmcache/v1/cache_controller/controllers/kv_controller.py"

LOCAL = "LocalCPUBackend"


# --- 1. message classes: real fields == the fields the stubs declare ---------


#: Derived from tests/conftest.py itself (see conformance/stub_surface.py), so the
#: parametrization can never drift from what the offline suite actually stubs.
@pytest.mark.parametrize("name", stubbed_message_names())
def test_message_fields_match_the_stub_exactly(name):
    """Names AND order: both sides accept positional args in field order."""
    real = getattr(real_message, name)
    expected = stub_fields(name)
    assert list(real.__struct_fields__) == expected, (
        f"tests/conftest.py declares {name}({', '.join(expected)}) but the real "
        f"lmcache has {name}({', '.join(real.__struct_fields__)})"
    )


def test_every_stubbed_but_unused_message_exists_upstream():
    """conftest fabricates empty classes for these; they must at least exist."""
    missing = [n for n in unused_message_names() if not hasattr(real_message, n)]
    assert not missing, f"not in lmcache.v1.cache_controller.message: {missing}"


def test_controller_manager_honours_the_fake_managers_contract():
    """FakeControllerManager stands in for this class in the routing tests:
    KvawareRouter awaits `handle_orchestration_message(msg)` and (in prod)
    calls `start_all()`. Both must exist on the real thing, and the handler
    must be awaitable."""
    from lmcache.v1.cache_controller.controller_manager import (
        LMCacheControllerManager,
    )

    assert asyncio.iscoroutinefunction(
        LMCacheControllerManager.handle_orchestration_message
    )
    assert callable(LMCacheControllerManager.start_all)


# --- 2. the chunker: real ChunkedTokenDatabase vs the PrefixHashTokenDatabase
#        contract (tests/test_kv_controller_lookup.py) ------------------------


@pytest.fixture(scope="module")
def db():
    # No arguments, exactly as the (upstream) KVController.__init__ constructs
    # it - defaults chunk_size=256, save_unfull_chunk=True.
    return ChunkedTokenDatabase()


def triples(db, tokens):
    return list(db.process_tokens(tokens, make_key=False))


def test_default_chunk_size_is_256(db):
    assert db.chunk_size == 256
    assert db.save_unfull_chunk is True


def test_yields_start_end_hash_with_exclusive_clamped_end(db):
    out = triples(db, list(range(600)))
    assert [(s, e) for s, e, _ in out] == [(0, 256), (256, 512), (512, 600)]
    assert all(isinstance(k, int) for _, _, k in out), "make_key=False => raw hash"


def test_exact_multiple_has_no_tail_chunk(db):
    assert [(s, e) for s, e, _ in triples(db, list(range(512)))] == [
        (0, 256),
        (256, 512),
    ]


def test_short_prompt_is_one_short_chunk(db):
    out = triples(db, list(range(10)))
    assert [(s, e) for s, e, _ in out] == [(0, 10)]


def test_keys_are_deterministic_within_a_process(db):
    tokens = list(range(1000))
    assert [k for _, _, k in triples(db, tokens)] == [
        k for _, _, k in triples(db, tokens)
    ]


def test_key_is_a_rolling_prefix_hash(db):
    """Same prefix => same keys; divergence changes every key from there on.

    This is the property the whole Lookup Extension stands on: a chunk's key
    identifies the entire prefix up to and including it, so equal keys mean
    equal prefixes and an instance's credit can be read off the shared walk.
    """
    base = list(range(768))  # 3 chunks
    same_prefix = base[:512] + [9999] * 256  # diverges in chunk 3
    early_diverge = [7] + base[1:]  # diverges in chunk 1

    base_keys = [k for _, _, k in triples(db, base)]
    sp_keys = [k for _, _, k in triples(db, same_prefix)]
    ed_keys = [k for _, _, k in triples(db, early_diverge)]
    assert sp_keys[:2] == base_keys[:2]
    assert sp_keys[2] != base_keys[2]
    assert all(k != b for k, b in zip(ed_keys, base_keys))


# --- 3. the patched kv_controller against the real package, no stubs ---------


@pytest.fixture(scope="module")
def kv_controller_module():
    spec = importlib.util.spec_from_file_location(
        "conformance_patched_kv_controller", PATCH_FILE
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_patch_imports_the_real_lmcache(kv_controller_module):
    assert kv_controller_module.ChunkedTokenDatabase is ChunkedTokenDatabase
    assert kv_controller_module.LookupRetMsg is real_message.LookupRetMsg


def make_controller(kv_controller_module):
    controller = kv_controller_module.KVController()
    assert isinstance(controller.token_database, ChunkedTokenDatabase)
    return controller


def admit(controller, tokens, instance_id, chunk_ids):
    keys = [
        k
        for _, _, k in controller.token_database.process_tokens(tokens, make_key=False)
    ]
    for idx, key in enumerate(keys):
        if idx in chunk_ids:
            asyncio.run(
                controller.admit(
                    real_message.KVAdmitMsg(instance_id, 0, key, LOCAL)
                )
            )


def lookup(controller, tokens):
    ret = asyncio.run(
        controller.lookup(real_message.LookupMsg(event_id="evt", tokens=tokens))
    )
    assert isinstance(ret, real_message.LookupRetMsg)
    assert ret.event_id == "evt"
    return ret.layout_info


def test_multi_instance_lookup_on_the_real_chunker(kv_controller_module):
    """The offline suite's core scenarios, replayed with real 256-token chunks."""
    tokens = list(range(1024))  # 4 chunks
    c = make_controller(kv_controller_module)
    admit(c, tokens, "instance-a", {0, 1, 2, 3})
    admit(c, tokens, "instance-b", {0, 1})
    admit(c, tokens, "instance-c", {0})

    assert lookup(c, tokens) == {
        "instance-a": (LOCAL, 1024),
        "instance-b": (LOCAL, 512),
        "instance-c": (LOCAL, 256),
    }


def test_gap_stops_credit_on_the_real_chunker(kv_controller_module):
    tokens = list(range(1024))
    c = make_controller(kv_controller_module)
    admit(c, tokens, "instance-a", {0, 1, 2, 3})
    admit(c, tokens, "instance-b", {0, 2, 3})  # hole at chunk 1

    assert lookup(c, tokens) == {
        "instance-a": (LOCAL, 1024),
        "instance-b": (LOCAL, 256),
    }


def test_short_tail_credits_actual_tokens_on_the_real_chunker(kv_controller_module):
    tokens = list(range(600))  # 256 + 256 + 88
    c = make_controller(kv_controller_module)
    admit(c, tokens, "instance-a", {0, 1, 2})

    assert lookup(c, tokens) == {"instance-a": (LOCAL, 600)}


def test_eviction_still_drives_lookup_on_the_real_messages(kv_controller_module):
    tokens = list(range(512))
    c = make_controller(kv_controller_module)
    admit(c, tokens, "instance-a", {0, 1})
    admit(c, tokens, "instance-b", {0, 1})

    second_key = [
        k
        for _, _, k in c.token_database.process_tokens(tokens, make_key=False)
    ][1]
    asyncio.run(c.evict(real_message.KVEvictMsg("instance-b", 0, second_key, LOCAL)))

    assert lookup(c, tokens) == {
        "instance-a": (LOCAL, 512),
        "instance-b": (LOCAL, 256),
    }

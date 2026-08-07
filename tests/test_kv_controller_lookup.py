"""Unit tests for the multi-instance lookup extension (run: pytest tests/).

Change 1 of the project: `KVController.lookup()` must report how much of a
request's prefix *every* Instance holds, not just the first holder of each
chunk. See CONTEXT.md ("Lookup Extension", "Cache-Hit Benefit") and issue #4.

No cluster, no GPU, no lmcache install — conftest.py loads the tracked patch
file with the lmcache import surface stubbed out.
"""

import asyncio

import pytest
from conftest import KVAdmitMsg, LookupMsg, kv_controller

CHUNK = 4  # real chunk size is 256; 4 keeps the fixtures readable
LOCAL = "LocalCPUBackend"
NONE_HASH = 0


class PrefixHashTokenDatabase:
    """Test double for `ChunkedTokenDatabase` with the same yield contract.

    Verbatim semantics from lmcache/v1/token_database.py in the pinned image:
    tokens are cut into `chunk_size` chunks (a short final chunk is kept), each
    chunk's key is a *rolling prefix hash* of everything up to and including it,
    and `process_tokens(make_key=False)` yields `(start, end, hash)` with `end`
    the exclusive token offset — i.e. the matched-token count at that chunk.
    """

    def __init__(self, chunk_size=CHUNK):
        self.chunk_size = chunk_size

    def process_tokens(self, tokens=None, make_key=True, **_kwargs):
        assert make_key is False, "the controller always asks for raw hashes"
        prefix_hash = NONE_HASH
        total = len(tokens)
        for start in range(0, total, self.chunk_size):
            chunk = tuple(tokens[start : start + self.chunk_size])
            prefix_hash = hash((prefix_hash, chunk))
            yield start, min(start + self.chunk_size, total), prefix_hash


def run(coro):
    """Drive one coroutine; avoids a pytest-asyncio dependency."""
    return asyncio.run(coro)


def make_controller(chunk_size=CHUNK):
    controller = kv_controller.KVController()
    controller.token_database = PrefixHashTokenDatabase(chunk_size)
    return controller


def chunk_keys(controller, tokens):
    return [key for _s, _e, key in controller.token_database.process_tokens(
        tokens, make_key=False
    )]


def admit(controller, tokens, instance_id, chunk_ids, location=LOCAL, worker_id=0):
    """Admit the given chunk indices of `tokens` as held by `instance_id`."""
    for idx, key in enumerate(chunk_keys(controller, tokens)):
        if idx in chunk_ids:
            run(controller.admit(KVAdmitMsg(instance_id, worker_id, key, location)))


def lookup(controller, tokens):
    return run(controller.lookup(LookupMsg(event_id="evt", tokens=tokens))).layout_info


def upstream_lookup(controller, tokens):
    """Reference implementation of the *stock* lookup, for regression checks.

    Verbatim from lmcache 0.3.9post2 `kv_controller.py:159-170`, read out of the
    running router pod on 2026-08-01.
    """
    layout_info = {}
    for _start, end, key in controller.token_database.process_tokens(
        tokens, make_key=False
    ):
        if key not in controller.kv_pool:
            break
        matched_instance = controller.kv_pool[key][0].instance_id
        matched_location = controller.kv_pool[key][0].location
        layout_info[matched_instance] = (matched_location, end)
    return layout_info


TOKENS = list(range(16))  # 4 chunks of 4


# --- the defect this ticket fixes -------------------------------------------


def test_all_holders_are_reported_not_just_the_first():
    """The whole defect: upstream credited kv_pool[key][0] and dropped the rest."""
    c = make_controller()
    admit(c, TOKENS, "instance-a", {0, 1, 2, 3})
    admit(c, TOKENS, "instance-b", {0, 1, 2, 3})

    assert lookup(c, TOKENS) == {
        "instance-a": (LOCAL, 16),
        "instance-b": (LOCAL, 16),
    }
    assert upstream_lookup(c, TOKENS) == {"instance-a": (LOCAL, 16)}


def test_divergent_prefix_depths_are_credited_per_instance():
    """Instances that diverge at different depths each get their own count."""
    c = make_controller()
    admit(c, TOKENS, "instance-a", {0, 1, 2, 3})  # holds all 16 tokens
    admit(c, TOKENS, "instance-b", {0, 1})  # holds the first 8
    admit(c, TOKENS, "instance-c", {0})  # holds the first 4

    assert lookup(c, TOKENS) == {
        "instance-a": (LOCAL, 16),
        "instance-b": (LOCAL, 8),
        "instance-c": (LOCAL, 4),
    }


def test_gap_stops_credit_at_the_gap_not_after_it():
    """A cache match is a *prefix* match: credit is contiguous from token 0.

    instance-b holds chunks 0 and 2 but not 1. Those chunk-2 tokens are
    unusable — the engine cannot skip a hole in the prefix — so b must be
    credited 4 tokens, not 12.
    """
    c = make_controller()
    admit(c, TOKENS, "instance-a", {0, 1, 2, 3})
    admit(c, TOKENS, "instance-b", {0, 2, 3})

    assert lookup(c, TOKENS) == {
        "instance-a": (LOCAL, 16),
        "instance-b": (LOCAL, 4),
    }


def test_walk_stops_when_no_instance_holds_a_chunk():
    c = make_controller()
    admit(c, TOKENS, "instance-a", {0, 1, 3})
    admit(c, TOKENS, "instance-b", {0, 1, 3})

    # chunk 2 is held by nobody, so nothing past token 8 can count
    assert lookup(c, TOKENS) == {
        "instance-a": (LOCAL, 8),
        "instance-b": (LOCAL, 8),
    }


def test_many_holders():
    c = make_controller()
    for n in range(5):
        admit(c, TOKENS, f"instance-{n}", set(range(n + 1)))

    assert lookup(c, TOKENS) == {
        "instance-0": (LOCAL, 4),
        "instance-1": (LOCAL, 8),
        "instance-2": (LOCAL, 12),
        "instance-3": (LOCAL, 16),
        "instance-4": (LOCAL, 16),  # only 4 chunks exist; chunk 4 is never admitted
    }


# --- regression: kvaware's baseline behaviour must not move -------------------


def test_single_holder_matches_upstream_exactly():
    """The baseline arm: with one holder per chunk the fix is a no-op."""
    c = make_controller()
    admit(c, TOKENS, "instance-a", {0, 1, 2})

    assert lookup(c, TOKENS) == upstream_lookup(c, TOKENS)
    assert lookup(c, TOKENS) == {"instance-a": (LOCAL, 12)}


def test_selected_instance_is_unchanged_even_with_several_holders():
    """kvaware picks `list(layout_info.keys())[0]`, and that key does not move.

    Both implementations insert `kv_pool[key0][0]` first, and Python keeps a
    key's original position when it is re-assigned — so the *selected instance*
    is invariant under the patch.
    """
    c = make_controller()
    admit(c, TOKENS, "instance-a", {0})
    admit(c, TOKENS, "instance-b", {0, 1})
    admit(c, TOKENS, "instance-a", {1})

    assert list(lookup(c, TOKENS))[0] == list(upstream_lookup(c, TOKENS))[0]


def test_matched_tokens_of_the_selected_instance_can_grow():
    """...but its `matched_tokens` does change, and that is what kvaware bands.

    kvaware compares `matched_tokens` against `kv_aware_threshold`
    (`routing_logic.py:354-369`) to choose the cache path over the QPS
    fallback, so a larger count can flip the branch even though the instance
    picked from `layout_info` is the same. This is why the baseline arm must be
    measured with the patch reverted rather than mounted.
    """
    c = make_controller()
    admit(c, TOKENS, "instance-a", {0})  # kv_pool[key0] == [a, b]
    admit(c, TOKENS, "instance-b", {0, 1})  # kv_pool[key1] == [b, a]
    admit(c, TOKENS, "instance-a", {1})

    # stock credits instance-a only on the chunk where it happens to be [0]
    assert upstream_lookup(c, TOKENS) == {
        "instance-a": (LOCAL, 4),
        "instance-b": (LOCAL, 8),
    }
    # the patch credits it on every chunk it actually holds
    assert lookup(c, TOKENS) == {
        "instance-a": (LOCAL, 8),
        "instance-b": (LOCAL, 8),
    }


# --- edges -------------------------------------------------------------------


def test_empty_pool_returns_no_match():
    c = make_controller()
    assert lookup(c, TOKENS) == {}


def test_miss_on_the_first_chunk_returns_no_match():
    c = make_controller()
    admit(c, TOKENS, "instance-a", {1, 2, 3})  # everything but the first chunk
    assert lookup(c, TOKENS) == {}


def test_single_chunk_prompt():
    c = make_controller()
    tokens = [1, 2, 3, 4]
    admit(c, tokens, "instance-a", {0})
    admit(c, tokens, "instance-b", {0})
    assert lookup(c, tokens) == {
        "instance-a": (LOCAL, 4),
        "instance-b": (LOCAL, 4),
    }


def test_short_final_chunk_counts_actual_tokens():
    """`end` is clamped to the prompt length, so a 14-token prompt reports 14."""
    c = make_controller()
    tokens = list(range(14))  # 3 full chunks + a 2-token tail
    admit(c, tokens, "instance-a", {0, 1, 2, 3})
    assert lookup(c, tokens) == {"instance-a": (LOCAL, 14)}


def test_same_instance_on_several_workers_counts_once():
    """kv_pool holds one entry per instance-*worker*; the Instance is the unit."""
    c = make_controller()
    admit(c, TOKENS, "instance-a", {0, 1}, location=LOCAL, worker_id=0)
    admit(c, TOKENS, "instance-a", {0, 1}, location="LocalDiskBackend", worker_id=1)

    # first metadata wins for the location, matching upstream's `[0]` choice
    assert lookup(c, TOKENS) == {"instance-a": (LOCAL, 8)}


def test_location_is_reported_per_instance():
    c = make_controller()
    admit(c, TOKENS, "instance-a", {0, 1}, location=LOCAL)
    admit(c, TOKENS, "instance-b", {0, 1}, location="LocalDiskBackend")

    assert lookup(c, TOKENS) == {
        "instance-a": (LOCAL, 8),
        "instance-b": ("LocalDiskBackend", 8),
    }


def test_eviction_removes_an_instance_from_the_match():
    """admit/evict bookkeeping still drives lookup after the change."""
    from conftest import KVEvictMsg

    c = make_controller()
    admit(c, TOKENS, "instance-a", {0, 1})
    admit(c, TOKENS, "instance-b", {0, 1})

    second_chunk_key = chunk_keys(c, TOKENS)[1]
    run(c.evict(KVEvictMsg("instance-b", 0, second_chunk_key, LOCAL)))

    assert lookup(c, TOKENS) == {
        "instance-a": (LOCAL, 8),
        "instance-b": (LOCAL, 4),
    }


@pytest.mark.parametrize("chunk_size", [1, 2, 4, 8])
def test_holds_for_any_chunk_size(chunk_size):
    c = make_controller(chunk_size)
    tokens = list(range(24))
    admit(c, tokens, "instance-a", set(range(len(tokens) // chunk_size)))
    admit(c, tokens, "instance-b", {0})

    assert lookup(c, tokens) == {
        "instance-a": (LOCAL, 24),
        "instance-b": (LOCAL, min(chunk_size, 24)),
    }

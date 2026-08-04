"""Unit tests for the driver's SSE chunk classification.

Regression cover for the bug that made the 2026-08-04 gate probe record **zero
TTFT values across 500 requests** while still populating token counts.

`stream_options={"include_usage": True}` makes vLLM put a `usage` key on EVERY
chunk - `null` on token-carrying chunks, populated only on the final usage-only
chunk. The driver classified chunks with a substring test, `'"usage"' in data`,
which therefore matched every chunk and `continue`d past the TTFT/ITL recording
on all of them. Token counts still landed because the final chunk set them, so
the CSV looked plausible at a glance: every column populated except the primary
metric.

The lesson encoded here: classify a chunk by whether it CARRIES A TOKEN
(`choices` non-empty), never by whether a key name appears in its serialization.
"""

import json

import pytest

from load_driver import classify_chunk

# A real token-carrying chunk from vLLM with include_usage on. Note `usage: null`
# - this is the shape the substring test could not distinguish.
_TOKEN_CHUNK = json.dumps({
    "id": "cmpl-1",
    "object": "text_completion",
    "created": 1785791059,
    "model": "Qwen/Qwen2.5-3B-Instruct",
    "choices": [{"index": 0, "text": " world", "logprobs": None, "finish_reason": None}],
    "usage": None,
})

# The final chunk: carries usage, carries NO token, and has an empty choices list.
_USAGE_CHUNK = json.dumps({
    "id": "cmpl-1",
    "object": "text_completion",
    "created": 1785791059,
    "model": "Qwen/Qwen2.5-3B-Instruct",
    "choices": [],
    "usage": {"prompt_tokens": 1578, "completion_tokens": 64, "total_tokens": 1642},
})

# Older builds omit `usage` entirely on token chunks rather than nulling it.
_TOKEN_CHUNK_NO_USAGE_KEY = json.dumps({
    "choices": [{"index": 0, "text": " world", "finish_reason": None}],
})


def test_token_chunk_with_null_usage_carries_a_token():
    """THE regression. This chunk contains the substring '"usage"', so the old
    check skipped it - and every token chunk looks like this."""
    usage, carries_token = classify_chunk(_TOKEN_CHUNK)
    assert usage is None
    assert carries_token is True


def test_final_usage_chunk_carries_no_token():
    """It must not count as an inter-token gap: its arrival is a stream-close
    artifact, not a decode step."""
    usage, carries_token = classify_chunk(_USAGE_CHUNK)
    assert usage == {"prompt_tokens": 1578, "completion_tokens": 64, "total_tokens": 1642}
    assert carries_token is False


def test_token_chunk_without_a_usage_key_still_carries_a_token():
    usage, carries_token = classify_chunk(_TOKEN_CHUNK_NO_USAGE_KEY)
    assert usage is None
    assert carries_token is True


def test_substring_test_would_have_failed_here():
    """Pins the actual defect, so a future 'optimization' back to a substring
    prefilter fails loudly instead of silently voiding the primary metric."""
    assert '"usage"' in _TOKEN_CHUNK          # the old check matched...
    assert classify_chunk(_TOKEN_CHUNK)[1]    # ...but the chunk does carry a token


@pytest.mark.parametrize("chunk", [_TOKEN_CHUNK, _USAGE_CHUNK, _TOKEN_CHUNK_NO_USAGE_KEY])
def test_classification_is_pure_and_repeatable(chunk):
    assert classify_chunk(chunk) == classify_chunk(chunk)


def test_ttft_is_taken_from_the_first_token_chunk_not_the_first_chunk():
    """End-to-end shape of the loop's contract: replaying a realistic chunk
    sequence must yield one TTFT and (n_tokens - 1) inter-token gaps."""
    stream = [_TOKEN_CHUNK, _TOKEN_CHUNK, _TOKEN_CHUNK, _USAGE_CHUNK]
    ttft_idx, gaps, usage_seen = None, 0, None
    for i, data in enumerate(stream):
        usage, carries_token = classify_chunk(data)
        if usage:
            usage_seen = usage
        if not carries_token:
            continue
        if ttft_idx is None:
            ttft_idx = i
        else:
            gaps += 1
    assert ttft_idx == 0          # first TOKEN chunk
    assert gaps == 2              # 3 tokens -> 2 gaps
    assert usage_seen["completion_tokens"] == 64

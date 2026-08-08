"""End-to-end harness test: real driver -> real CSV -> real analysis, no GPU (#28).

Every other test in this repo tests a function. This one tests the *path* - because every
serious measurement bug this project shipped lived in the path, not in a function, and each
was catchable on a laptop:

| bug | what it looked like |
|---|---|
| `include_usage` classification | **zero TTFT across 500 requests**, token counts fine, CSV plausible |
| ITL never recorded | 92% of each request unmeasured except as an aggregate |
| `compare` matched seed *count*, not seed *set* | paired test silently pairing the wrong seeds |
| `fig_paired` returned silently on a missing cell | figure set quietly missing its centerpiece |

None of them needed a GPU. None of them was caught. This test runs `load_driver.py` as a
subprocess - the actual CLI a cell invokes, not an imported function - against
`fake_vllm.py`, then feeds the result through `analyze.py`.

The delays are *known*, so the assertions are on values rather than on non-emptiness.
"non-zero TTFT" would also have passed for a driver timestamping the wrong chunk.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

from fake_vllm import running
from load_driver import Result

BENCH = Path(__file__).resolve().parent
TTFT_MS = 60.0
ITL_MS = 8.0
MAX_TOKENS = 6
N_REQUESTS = 4


def _workload(tmp_path: Path, n: int = N_REQUESTS) -> Path:
    p = tmp_path / "wl.jsonl"
    with open(p, "w") as f:
        f.write(json.dumps({"config": {"note": "harness test"}}) + "\n")
        for i in range(n):
            f.write(json.dumps({"index": i, "prefix_id": i % 2,
                                "prompt": f"prompt {i} with a few words"}) + "\n")
    return p


def _drive(tmp_path: Path, base_url: str, **extra) -> list[dict]:
    """Run the driver exactly as a cell does, and return the CSV rows."""
    out = tmp_path / "driver.csv"
    cmd = [sys.executable, str(BENCH / "load_driver.py"),
           "--base-url", base_url, "--model", "fake",
           "--workload", str(_workload(tmp_path, extra.pop("n", N_REQUESTS))),
           "--rate", "50", "--max-tokens", str(MAX_TOKENS),
           "--seed", "1", "--out", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, f"driver failed:\n{r.stdout}\n{r.stderr}"
    with open(out) as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="module")
def rows(tmp_path_factory):
    with running(ttft_ms=TTFT_MS, itl_ms=ITL_MS) as url:
        return _drive(tmp_path_factory.mktemp("ok"), url)


def test_every_request_records_a_ttft(rows):
    """THE regression. A `usage` key on every chunk once zeroed all of these."""
    assert len(rows) == N_REQUESTS
    ttfts = [r["ttft_s"] for r in rows]
    assert all(t not in ("", None) for t in ttfts), f"empty TTFT in {ttfts}"
    assert all(float(t) > 0 for t in ttfts)


def test_ttft_matches_the_injected_first_token_delay(rows):
    """Not just non-zero: the right chunk. A driver timestamping the *first* chunk
    instead of the first *token* chunk would still produce a non-zero TTFT."""
    for r in rows:
        assert float(r["ttft_s"]) == pytest.approx(TTFT_MS / 1000.0, abs=0.05)


def test_itl_count_is_max_tokens_minus_one(rows):
    """N tokens produce N-1 gaps. The usage-only chunk must contribute none - if it did,
    this would be MAX_TOKENS, which is exactly how a stream-close artifact hides."""
    for r in rows:
        gaps = [g for g in r["itls_ms"].split(";") if g]
        assert len(gaps) == MAX_TOKENS - 1, f"expected {MAX_TOKENS - 1} gaps, got {gaps}"


def test_itl_values_match_the_injected_gap(rows):
    for r in rows:
        for g in (float(x) for x in r["itls_ms"].split(";") if x):
            assert g == pytest.approx(ITL_MS, abs=25.0)


def test_token_counts_come_from_the_usage_chunk(rows):
    for r in rows:
        assert int(r["completion_tokens"]) == MAX_TOKENS
        assert int(r["prompt_tokens"]) > 0


def test_csv_schema_matches_the_result_dataclass(rows):
    """Analysis code reads these by name; a silent rename breaks every downstream reader."""
    assert list(rows[0].keys()) == [f.name for f in dataclasses.fields(Result)]


def test_all_requests_report_ok(rows):
    assert {r["status"] for r in rows} == {"ok"}


def test_errors_are_counted_but_excluded_from_latency(tmp_path):
    """Validity rule 1, exercised through the real driver rather than asserted in prose."""
    with running(ttft_ms=TTFT_MS, itl_ms=ITL_MS, fail_every=2) as url:
        rows = _drive(tmp_path, url, n=4)
    errors = [r for r in rows if r["status"] == "error"]
    ok = [r for r in rows if r["status"] == "ok"]
    assert errors, "injected failures did not surface as error rows"
    assert all(r["ttft_s"] in ("", None) for r in errors), "errored rows must carry no latency"
    assert all(r["error"] for r in errors), "errored rows must carry the server's message"
    assert ok and all(float(r["ttft_s"]) > 0 for r in ok)


def test_analyze_reads_what_the_driver_wrote(tmp_path):
    """The join that matters: the analysis path must consume the driver's own output.

    `read_run` is what every reported statistic is built on, so a schema or parsing drift
    between the two halves is exactly the failure this file exists to catch.
    """
    import analyze

    with running(ttft_ms=TTFT_MS, itl_ms=ITL_MS) as url:
        run_dir = tmp_path / "20260101-000000-fake"
        run_dir.mkdir()
        out = run_dir / "driver-seed1.csv"
        cmd = [sys.executable, str(BENCH / "load_driver.py"),
               "--base-url", url, "--model", "fake",
               "--workload", str(_workload(tmp_path)),
               "--rate", "50", "--max-tokens", str(MAX_TOKENS),
               "--seed", "1", "--out", str(out)]
        assert subprocess.run(cmd, capture_output=True, timeout=120).returncode == 0

    seeds = analyze.read_run(str(run_dir))
    assert len(seeds) == 1
    s = seeds[0]
    assert s["seed"] == 1
    assert s["ok"] == N_REQUESTS
    assert s["ttft_p95"] == pytest.approx(TTFT_MS / 1000.0, abs=0.05)
    assert s["itl_p95"] > 0, "pooled ITL must survive the driver -> analyze join"

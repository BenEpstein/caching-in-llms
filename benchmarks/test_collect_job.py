"""collect_job.py is the boundary where a bad Job log has to be caught (#27).

Every test here is a way the log can lie: truncated mid-blob, truncated after the last
blob, a checksum that does not match, a seed that never arrived. The contract under test is
all-or-nothing - parse() either returns a complete cell or raises, never a partial one.
"""

import base64
import gzip
import hashlib

import pytest

from collect_job import CollectError, parse

CSV = b"index,prefix_id,send_ts,ttft_s\n0,7,1785885686.1,0.184\n1,3,1785885686.9,0.201\n"


def blob(name: str, data: bytes = CSV) -> str:
    b64 = base64.encodebytes(gzip.compress(data)).decode()  # wrapped, like base64(1)
    return (
        f"BEGIN {name} sha256={hashlib.sha256(data).hexdigest()} bytes={len(data)}\n"
        f"{b64}END {name}\n"
    )


def good_log(seeds=(1, 2), start=1785885686, end=1785886383) -> str:
    out = f"NODE gapu-2-worker1\nCELL_START {start}\n"
    for s in seeds:
        out += f"==> seed {s}\nn=500 ok=500 err=0 wall=31.2s\n"
    out += f"CELL_END {end}\n"
    for s in seeds:
        out += blob(f"driver-seed{s}.csv")
    return out + "ALL_DONE\n"


def parse_str(text: str, seeds=("1", "2")):
    return parse(text.splitlines(keepends=True), seeds)


def test_round_trip():
    marks, blobs = parse_str(good_log())
    assert marks["NODE"] == "gapu-2-worker1"
    assert marks["CELL_START"] == "1785885686"
    assert blobs["driver-seed1.csv"] == CSV
    assert blobs["driver-seed2.csv"] == CSV


def test_progress_lines_between_markers_are_ignored():
    # load_driver prints a multi-line summary per seed; none of it may reach the parser's
    # output, and none of it may be mistaken for a frame.
    assert parse_str(good_log())[1].keys() == {"driver-seed1.csv", "driver-seed2.csv"}


def test_truncated_mid_blob():
    # the VPN drops while the pod is still emitting seed 2
    log = good_log()
    cut = log.index("END driver-seed2.csv")
    with pytest.raises(CollectError, match="truncated mid-blob"):
        parse_str(log[: cut - 40])


def test_truncated_after_last_end():
    # everything arrived, but the log stops before ALL_DONE - we cannot tell whether the pod
    # finished, so the cell is not trusted
    log = good_log().replace("ALL_DONE\n", "")
    with pytest.raises(CollectError, match="no ALL_DONE"):
        parse_str(log)


def test_corrupted_blob_body_fails_checksum():
    # a flipped payload that still decodes and gunzips must not slip through
    other = CSV.replace(b"0.184", b"9.999")
    # the frame keeps the ORIGINAL sha while the payload carries `other`
    bad = (
        f"BEGIN driver-seed2.csv sha256={hashlib.sha256(CSV).hexdigest()} bytes={len(CSV)}\n"
        f"{base64.encodebytes(gzip.compress(other)).decode()}END driver-seed2.csv\n"
    )
    log = good_log().replace(blob("driver-seed2.csv"), bad)
    with pytest.raises(CollectError, match="sha256"):
        parse_str(log)


def test_byte_count_mismatch():
    bad = (
        f"BEGIN driver-seed2.csv sha256={hashlib.sha256(CSV).hexdigest()} bytes={len(CSV) + 1}\n"
        f"{base64.encodebytes(gzip.compress(CSV)).decode()}END driver-seed2.csv\n"
    )
    with pytest.raises(CollectError, match="declared"):
        parse_str(good_log().replace(blob("driver-seed2.csv"), bad))


def test_missing_seed():
    with pytest.raises(CollectError, match="missing seeds: driver-seed3.csv"):
        parse_str(good_log(), seeds=("1", "2", "3"))


def test_unexpected_seed():
    with pytest.raises(CollectError, match="unexpected blobs"):
        parse_str(good_log(seeds=(1, 2, 3)))


def test_missing_window_marker():
    with pytest.raises(CollectError, match="no CELL_END"):
        parse_str(good_log().replace("CELL_END 1785886383\n", ""))


def test_missing_node():
    with pytest.raises(CollectError, match="no NODE"):
        parse_str(good_log().replace("NODE gapu-2-worker1\n", ""))


def test_window_not_a_window():
    # a pod clock that went backwards would silently produce an empty Prometheus window
    with pytest.raises(CollectError, match="not a window"):
        parse_str(good_log(start=1785886383, end=1785885686))


def test_interleaved_frames():
    log = good_log().replace("END driver-seed1.csv", "END driver-seed9.csv")
    with pytest.raises(CollectError, match="interleaved"):
        parse_str(log)

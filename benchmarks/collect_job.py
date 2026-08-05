"""Parse a bench-driver Job log into per-seed CSVs plus the measurement window (#27).

The Job's only channel out is stdout (see benchmarks/in_pod.sh for the format), so this is
the boundary where a truncated or garbled log has to be caught. It is therefore all-or-
nothing: nothing is written unless every check passes. A partially-collected cell that looks
complete is worse than no cell at all - it would enter the paired stats as a real
observation.

Checked, in order:
  - NODE / CELL_START / CELL_END present, the two timestamps integers, END > START
  - every blob framed by a matching BEGIN/END pair (a log that stops mid-blob fails here)
  - base64 decodes and gunzips
  - decompressed length matches the `bytes=` field
  - SHA-256 of the decompressed CSV matches the `sha256=` field
  - exactly the expected seeds, no more and no fewer
  - ALL_DONE present, which is what catches a log truncated after the last END

stdlib-only, like the rest of the harness.

Usage:
  python3 collect_job.py --log job.log --out results/<run> --seeds "1 2 3"
"""

from __future__ import annotations

import argparse
import base64
import binascii
import gzip
import hashlib
import os
import re
import sys
from typing import Dict, Iterable, Tuple

BEGIN_RE = re.compile(r"^BEGIN (\S+) sha256=([0-9a-f]{64}) bytes=(\d+)$")
END_RE = re.compile(r"^END (\S+)$")
MARK_RE = re.compile(r"^(NODE|CELL_START|CELL_END) (\S+)$")


class CollectError(Exception):
    """The log cannot be trusted. Never partially recovered from."""


def _decode(name: str, b64: str, want_sha: str, want_bytes: int) -> bytes:
    try:
        # validate=True so stray progress text inside a blob is an error rather than being
        # silently skipped. The caller strips newlines before joining, which is why the
        # wrapped base64 passes.
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise CollectError(f"{name}: base64 did not decode ({e}) - log corrupted") from e
    try:
        data = gzip.decompress(raw)
    except (OSError, EOFError) as e:
        raise CollectError(f"{name}: gunzip failed ({e}) - blob truncated") from e
    if len(data) != want_bytes:
        raise CollectError(
            f"{name}: decompressed to {len(data)} bytes, header declared {want_bytes}"
        )
    got = hashlib.sha256(data).hexdigest()
    if got != want_sha:
        raise CollectError(f"{name}: sha256 {got[:16]}… != declared {want_sha[:16]}…")
    return data


def parse(lines: Iterable[str], expected_seeds: Iterable[str]) -> Tuple[Dict[str, str], Dict[str, bytes]]:
    marks: Dict[str, str] = {}
    blobs: Dict[str, bytes] = {}
    all_done = False
    name = None
    meta: Tuple[str, int] = ("", 0)
    body: list = []

    for raw in lines:
        line = raw.rstrip("\n")
        if name is None:
            m = BEGIN_RE.match(line)
            if m:
                name, meta, body = m.group(1), (m.group(2), int(m.group(3))), []
                continue
            m = MARK_RE.match(line)
            if m:
                marks[m.group(1)] = m.group(2)
            elif line == "ALL_DONE":
                all_done = True
            continue
        # inside a blob. A base64 line contains no space, so it can never match END_RE.
        m = END_RE.match(line)
        if m:
            if m.group(1) != name:
                raise CollectError(f"END {m.group(1)} closes BEGIN {name} - log interleaved")
            blobs[name] = _decode(name, "".join(body), *meta)
            name = None
            continue
        body.append(line)

    if name is not None:
        raise CollectError(f"log ends inside {name}: no END marker - truncated mid-blob")

    for key in ("NODE", "CELL_START", "CELL_END"):
        if key not in marks:
            raise CollectError(f"no {key} line in the log")
    try:
        start, end = int(marks["CELL_START"]), int(marks["CELL_END"])
    except ValueError as e:
        raise CollectError(f"non-integer window marker: {marks} ({e})") from e
    if end <= start:
        raise CollectError(f"CELL_END {end} <= CELL_START {start} - window is not a window")

    want = {f"driver-seed{s}.csv" for s in expected_seeds}
    missing = sorted(want - set(blobs))
    extra = sorted(set(blobs) - want)
    if missing:
        raise CollectError(f"missing seeds: {', '.join(missing)}")
    if extra:
        raise CollectError(f"unexpected blobs: {', '.join(extra)}")

    # Last, so a mid-blob truncation reports the more specific error above.
    if not all_done:
        raise CollectError("no ALL_DONE - the log is truncated after the last END")

    return marks, blobs


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--log", required=True, help="output of `oc logs job/<name>`")
    p.add_argument("--out", required=True, help="the cell's results directory")
    p.add_argument("--seeds", required=True, help='space-separated, e.g. "1 2 3"')
    p.add_argument("--bench-image", default="unknown", help="recorded in window.env for run.json")
    a = p.parse_args()

    seeds = a.seeds.split()
    with open(a.log, encoding="utf-8", errors="replace") as f:
        try:
            marks, blobs = parse(f, seeds)
        except CollectError as e:
            print(f"COLLECT FAILED: {e}", file=sys.stderr)
            print("nothing written - do not measure on this cell", file=sys.stderr)
            return 1

    os.makedirs(a.out, exist_ok=True)
    for name, data in sorted(blobs.items()):
        with open(os.path.join(a.out, name), "wb") as f:
            f.write(data)
        # header + one row per request, so the DoD's "500 rows/seed" is visible here
        rows = data.count(b"\n") - 1
        print(f"  {name}: {rows} rows, {len(data)} bytes")

    with open(os.path.join(a.out, "window.env"), "w") as f:
        f.write(
            f"CELL_START={marks['CELL_START']}\n"
            f"CELL_END={marks['CELL_END']}\n"
            f"DRIVER_NODE={marks['NODE']}\n"
            f"BENCH_IMAGE={a.bench_image}\n"
        )
    span = int(marks["CELL_END"]) - int(marks["CELL_START"])
    print(
        f"collected {len(blobs)} seeds into {a.out}; window {marks['CELL_START']}.."
        f"{marks['CELL_END']} ({span}s) on node {marks['NODE']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

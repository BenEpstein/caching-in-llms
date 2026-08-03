"""Poll the DCGM exporter into a CSV (issue #3: GPU util / power / mem-copy).

The stack's Prometheus does NOT scrape DCGM (no ServiceMonitor added - no
cluster mutation), so this polls the exporter directly through port-forwards.
The exporter is a DaemonSet (one pod per GPU node) and a Service port-forward
pins to ONE pod, silently dropping the other node's GPU - so pass one --url
per exporter POD (run_cell.sh forwards each pod on its own local port).

Runs until SIGINT/SIGTERM (run_cell.sh starts it in the background and kills it
when the cell ends) or for --duration seconds. Appends one row per GPU per
metric per sample: ts,metric,gpu,hostname,value.
"""

from __future__ import annotations

import argparse
import csv
import re
import signal
import sys
import time
import urllib.request

FIELDS = [
    "DCGM_FI_DEV_GPU_UTIL",
    "DCGM_FI_DEV_POWER_USAGE",
    "DCGM_FI_DEV_MEM_COPY_UTIL",
]

_LINE = re.compile(r'^(\w+)\{([^}]*)\}\s+([0-9eE.+-]+)$')
_LABEL = re.compile(r'(\w+)="([^"]*)"')


def parse(text: str):
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m or m.group(1) not in FIELDS:
            continue
        labels = dict(_LABEL.findall(m.group(2)))
        yield m.group(1), labels.get("gpu", ""), labels.get("Hostname", ""), float(
            m.group(3)
        )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--url",
        action="append",
        required=True,
        help="exporter /metrics URL; repeat once per exporter pod",
    )
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--duration", type=float, help="stop after N seconds (default: run until signal)")
    p.add_argument("--out", required=True, help="CSV path (appended)")
    a = p.parse_args()

    stop = False

    def on_signal(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    t_end = time.time() + a.duration if a.duration else None
    with open(a.out, "a", newline="") as f:
        w = csv.writer(f)
        if f.tell() == 0:
            w.writerow(["ts", "metric", "gpu", "hostname", "value"])
        while not stop and (t_end is None or time.time() < t_end):
            round_start = time.time()
            for url in a.url:
                try:
                    with urllib.request.urlopen(url, timeout=10) as r:
                        text = r.read().decode()
                    ts = round(time.time(), 3)
                    for metric, gpu, host, value in parse(text):
                        w.writerow([ts, metric, gpu, host, value])
                except Exception as e:  # noqa: BLE001 - keep polling through blips
                    print(f"poll error ({url}): {e}", file=sys.stderr)
            f.flush()
            # sleep to the deadline so slow fetches don't stretch the interval
            time.sleep(max(0.0, a.interval - (time.time() - round_start)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

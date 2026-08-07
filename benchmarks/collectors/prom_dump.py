"""Dump the Prometheus series for a run window to JSON files (issue #3).

One file per metric under <out>/: the raw /api/v1/query_range response.
Analysis reads these files, never the TSDB - Prometheus storage is an emptyDir
(deploy/prometheus.yaml), so the exported files ARE the experiment record.

Metric roles (methodology):
  vllm:num_requests_running/waiting   per-engine load → load-balance CV
  vllm:request_queue_time_seconds_*   queueing (mechanism metric)
  lmcache:*hit*                       hit-rate deltas over the window
  vllm:kv_cache_usage_perc            KV memory pressure = GPU memory (§3)
  lmcache:local_cache_usage           engine host RAM held by the cache (§3)
  process_cpu_seconds_total,
  process_resident_memory_bytes       router CPU/mem utilization (§3; router
                                      ONLY - the engines export no process_*)

Usage (run_cell.sh port-forwards Prometheus to localhost:9090):
  python3 prom_dump.py --start 1722500000 --end 1722503600 --out results/run/prom
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

METRICS = [
    "vllm:num_requests_running",
    "vllm:num_requests_waiting",
    "vllm:request_queue_time_seconds_sum",
    "vllm:request_queue_time_seconds_count",
    "vllm:kv_cache_usage_perc",
    # Validity rule 6 (#3 amendment): preemption is RECORDED and reported per
    # arm, never used to void a run - under concentration it is a genuine
    # consequence of the baseline's placement, so gating on it would discard the
    # baseline arm systematically and yield no result.
    # Not collected for the pre-amendment cells (the dump postdates them):
    # load_gate reports 0 preemptions there because the file is absent, not
    # because there were none.
    "vllm:num_preemptions_total",
    # vLLM's own HBM prefix-cache counters - the pair the scarcity gate reads.
    # The lmcache:* hit metrics below are ENGINE-LOCAL (job=vllm-engines) and
    # saturate regardless of routing, so they cannot stand in for these.
    "vllm:prefix_cache_queries_total",
    "vllm:prefix_cache_hits_total",
    # ENGINE-SIDE latency: the TTFT of record for any cross-cell comparison.
    # The driver's client-side ttft_s carries the laptop->cluster WAN as a
    # per-cell constant offset, so it stays in the CSVs as user-observed
    # latency but cannot compare arms measured at different times. See
    # benchmarks/README.md, "Which latency source is trustworthy".
    "vllm:time_to_first_token_seconds_bucket",
    "vllm:time_to_first_token_seconds_sum",
    "vllm:time_to_first_token_seconds_count",
    "vllm:request_prefill_time_seconds_sum",
    "vllm:request_prefill_time_seconds_count",
    "lmcache:num_hit_tokens_total",
    "lmcache:lookup_hit_rate",
    # request_cache_hit_rate is a HISTOGRAM in this lmcache build - there is no
    # plain gauge under the bare name
    "lmcache:request_cache_hit_rate_sum",
    "lmcache:request_cache_hit_rate_count",
    # §3 utilization (#35). Every series from here down is READ by
    # utilization.py; which number comes from where is in benchmarks/README.md,
    # "Utilization (§3)" (the process_* pair only ever returns the router).
    # Deliberately not a wider
    # net: #35 exists because series were collected and read by nothing, and
    # adding more unread ones reproduces the thing it fixed.
    # Rejected candidates: local_storage_usage / remote_cache_usage (flat zero in
    # this config); cache_config_info (no bytes-per-block figure).
    "lmcache:local_cache_usage",
    "lmcache:active_memory_objs_count",
    "process_cpu_seconds_total",
    "process_resident_memory_bytes",
    "router_cpu_usage_percent",
    "router_memory_usage_percent",
]


def query_range(prom_url: str, metric: str, start: float, end: float, step: str):
    qs = urllib.parse.urlencode(
        {"query": metric, "start": start, "end": end, "step": step}
    )
    with urllib.request.urlopen(f"{prom_url}/api/v1/query_range?{qs}", timeout=60) as r:
        return json.load(r)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prom-url", default="http://localhost:9090")
    p.add_argument("--start", type=float, required=True, help="epoch seconds")
    p.add_argument("--end", type=float, required=True, help="epoch seconds")
    p.add_argument("--step", default="5s")
    p.add_argument("--out", required=True, help="output directory")
    a = p.parse_args()

    os.makedirs(a.out, exist_ok=True)
    empty = []
    for metric in METRICS:
        try:
            resp = query_range(a.prom_url, metric, a.start, a.end, a.step)
        except Exception as e:  # noqa: BLE001
            print(f"ERROR querying {metric}: {e}", file=sys.stderr)
            return 1
        fname = metric.replace(":", "_") + ".json"
        with open(os.path.join(a.out, fname), "w") as f:
            json.dump(resp, f)
        n = len(resp.get("data", {}).get("result", []))
        if n == 0:
            empty.append(metric)
        print(f"{metric}: {n} series")
    if empty:
        # warn, don't fail: e.g. lmcache gauges are legitimately absent on roundrobin
        print(f"WARNING: no series for: {', '.join(empty)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

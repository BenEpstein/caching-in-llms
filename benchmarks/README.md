# Benchmark harness

> status: live · 2026-08-01 · implements the locked methodology (issue #3); the resolution
> comment there is the spec - this README is the operator's manual.

Measures **client-observed** latency for the routing-policy comparison on the gapu-2
cluster (2×A10, vLLM Production Stack + LMCache). Pre-registered headline: *loadaware as
shipped (β=1.0) reduces TTFT p95 vs kvaware* under a Zipfian shared-prefix
workload at fixed load.

## Layout

| Piece | Role |
|---|---|
| `workload_gen.py` | Zipfian prefix workload generator (pure, seeded, unit-tested) |
| `freeze_workloads.py` | Materialize + verify the 6 frozen seed files against `workloads/manifest.json` |
| `load_driver.py` | Async open-loop (Poisson) / closed-loop driver; per-request CSV; streaming TTFT |
| `in_pod.sh` | The measured replay, run **inside the cluster** by the bench image (#27) |
| `verify_dataset.sh` | Regenerate + verify the frozen workload; called by `in_pod.sh` and by CI |
| `bench_job.sh` | Laptop side: emit/apply the driver Job, wait it out, pull the log back |
| `collect_job.py` | Parse that log into per-seed CSVs + the measurement window; all-or-nothing |
| `warmup.py` | Unmeasured passes over the prefix pool before each cell |
| `collectors/prom_dump.py` | Prometheus `query_range` dump for the run window |
| `collectors/dcgm_poll.py` | GPU util/power/mem-copy CSV via the DCGM exporter |
| `run_cell.sh` | Per-cell choreography (deploy → gates → warm-up → 6 seeds → collect) |
| `run_sweep.sh` | All 6 cells, one unattended batch (~3 h) |
| `rate_pilot.sh` | Step 0: find the TTFT-p95 knee on kvaware; freeze ~75% of it |
| `analyze.py` | Per-seed summaries, validity gate, pre-registered Wilcoxon + bootstrap CI |

## The frozen workload

ONE dataset (issue #3, as amended): **128 prefixes**, Zipf **s=0.9** (`pool_seed=42`),
replayed as **20 seeds × 500 requests**.

Sequence lengths, **measured on the engine's own tokenizer** rather than taken from the
config, because the generator's knobs are approximate: `_filler` emits `approx_tokens *
0.75` words, so `prefix_tokens: 2048` yields a **1544-token** shared prefix, not 2048.

| | tokens | source |
|---|---|---|
| shared prefix (cacheable) | **1544** | `/tokenize` on the prefix substring |
| unique suffix | **34** | difference |
| **ISL** (full prompt) | **1578** | `/tokenize`, and `usage.prompt_tokens` on every request |
| **OSL** | **64** | `--max-tokens 64` with `ignore_eos: true`, so it is exact |

ISL is constant across every request in every seed (min = median = max = 1578), so prompt
length can never confound an arm. 97.8% of each prompt is cacheable. Quote these numbers,
not the config knobs.

Seeds share the prefix pool and
vary only sampling order + suffixes, so one warm-up covers every seed. The JSONL files
(~6 MB each) are not committed; `workloads/manifest.json` **is** - it pins the exact
config + SHA-256 per seed file, and generation is deterministic, so
`python3 freeze_workloads.py` reproduces them bit-identically and fails loudly on drift.
`run_cell.sh` runs that check before every cell.

## Sweep design

6 cells × 6 seeds × 500 requests, identical frozen workload and fixed Poisson rate in
every cell:

| Cell | Arm | Router image |
|---|---|---|
| `loadaware-b0` | loadaware β=0 (cache-only ablation) | CI-built, SHA-tagged |
| `loadaware-b0.25` | loadaware β=0.25 | CI-built, SHA-tagged |
| `loadaware-b1.0` | loadaware β=1.0 (**shipped default - headline**) | CI-built, SHA-tagged |
| `loadaware-b4.0` | loadaware β=4.0 | CI-built, SHA-tagged |
| `kvaware` | baseline (headline comparator) | pinned stock |
| `roundrobin` | baseline | pinned stock |

Built images only - the dev-loop ConfigMap overlay (`deploy/dev/`) is **never** measured.
No mock or simulation anywhere: every reported number comes from the real cluster.

## Running it

```bash
# 0. once: freeze + commit the workload manifest (already committed)
python3 benchmarks/freeze_workloads.py

# 1. step 0 - rate pilot on a warmed kvaware deployment (~30-60 min, human picks the knee)
benchmarks/rate_pilot.sh

# 2. the sweep. Two SHA-tagged CI-built images, both required:
#    LOADAWARE_TAG - the router image under test (loadaware cells only)
#    BENCH_TAG     - the in-cluster driver image (EVERY cell, both arms)
LOADAWARE_TAG=<sha> BENCH_TAG=<sha> benchmarks/run_sweep.sh <rate>

# 3. headline comparison + summaries
python3 benchmarks/analyze.py summary results/*
python3 benchmarks/analyze.py compare results/<...loadaware-b0.1> results/<...kvaware>
```

Per-cell choreography (`run_cell.sh`): `helm upgrade` with the cell's values (chart
**pinned to 0.1.11** - the installed version; 0.1.12+ has schema drift) → dev-overlay
check (a mounted `router-patch` ConfigMap is auto-reverted - validity rule 2) → β via
`oc set env` (chart 0.1.11 ignores `routerSpec.env`; baselines get the vars *removed* so
a stale β can't leak through the three-way merge) → router-Service controller-port patch
(deploy/README gotcha #0) → **router image asserted against the cell's label** (validity
rule 2) → **engine restart** so every cell starts from identical empty caches (#13) →
wait for 2 `Registered instance-worker` router-log lines since the restart (this router
build exposes no registered-workers gauge) → `registry-probe.sh` with a fresh seed (#13
gate) → warm-up passes gated on non-empty `layout_info` (router log must show
`found by … router` cache-path routings); both lookup gates are skipped on roundrobin
(`USES_LOOKUP=0`), whose routing ignores the registry and never emits either signal →
**the measured replay, as a Job inside the cluster** (see below), seeds back-to-back with
no reset between them (steady-state) → Prometheus dump + DCGM CSV (one port-forward **per
exporter pod** - the DaemonSet Service would pin to one node's GPU) + `run.json` manifest →
validity check.

### The measured replay runs in-cluster (#27)

Everything above and below this step is still laptop-side. Only the replay moved, and
`load_driver.py` itself is unchanged - the metric is the same, the instrument is not:

```
laptop:  helm → cold start → warm-up gate → [apply Job] → prom dump → run.json
cluster:                                     Job pod: verify dataset
                                                   → seeds → svc ClusterIP
                                                   → CSVs out through the pod log
```

- **Target** is `http://stack-router-service.<ns>.svc.cluster.local:80`, the same endpoint
  Prometheus scrapes. No route, no TLS, no `--insecure`, no WAN.
- **The dataset is regenerated in the pod, never copied.** The image carries only
  `manifest.json`; `verify_dataset.sh` rebuilds all 20 seeds (0.84 s) and fails hard on any
  SHA-256 drift. No PVC, no ConfigMap, no 126 MB transfer - and every cell re-proves the
  frozen dataset is reconstructible from source.
- **Results come back through the pod log**, one gzip+base64 frame per seed carrying the
  plaintext CSV's SHA-256. `collect_job.py` writes nothing unless every frame decodes and
  checksums: a partially-recovered cell would enter the paired stats as a real observation.
  ~2.4 MB per 20-seed cell, against kubelet's 10 Mi rotation size.
- **The measurement window comes from the pod's clock** (`CELL_START`/`CELL_END`), not the
  laptop's. Image pull plus dataset verification sit between warm-up and the first request,
  so a laptop-clock window would pull warm-up traffic into the Prometheus dump and
  contaminate the imbalance co-primary.
- **`backoffLimit: 0`, no `ttlSecondsAfterFinished`.** A crashed cell must fail loudly
  rather than silently replaying seeds into the same window; the pod must outlive the run
  because its log *is* the results channel.
- **Disconnects are survivable.** `oc logs -f` is progress only; collection is a plain
  `oc logs` at the end, which re-reads the whole log and is idempotent. Reconnecting is
  "run the command again", not "resume a stream" - and the Job keeps running regardless,
  which two VPN-destroyed sweeps are the reason for.
- **The driver pod shares a node with an engine, unavoidably**: gapu-2 has two schedulable
  nodes and an engine on each, and worker0 is at 93% CPU / 99% memory requested, so the
  driver lands on worker1. There is no CPU limit, on purpose - CFS throttling would inflate
  client TTFT exactly the way the WAN did. The control is the engine-side TTFT cross-check,
  not a scheduling rule; `run.json` records the node.

## What a run directory contains

```
results/<ts>-<cell>/
  driver-seed{1..6}.csv     client-observed per-request TTFT/E2E/tokens (percentile source of
                            truth; send_ts is wall-clock epoch, so per-seed windows derive from it)
  prom/*.json               vllm:num_requests_running/waiting, request_queue_time, kv_cache_usage,
                            lmcache hit metrics, process + router CPU/mem - per engine, 5 s resolution
  dcgm.csv                  GPU_UTIL / POWER_USAGE / MEM_COPY_UTIL per GPU, ~5 s samples
  run.json                  arm, β, rate, image + imageID, git commit, workload manifest, window,
                            and `driver` - where the replay ran (node, bench image, target URL)
  window.env                CELL_START/CELL_END from the POD's clock, plus node + bench image;
                            sourced by run_cell.sh, written by collect_job.py
  job.log                   raw Job log (untracked): the ~2.4 MB base64 TRANSPORT for the CSVs
                            above, kept on disk for debugging only
```

`analyze.py compare` refuses to pair two runs whose `run.json` rate or workload manifest
differ - the methodology's "identical workload across arms" is enforced, not assumed.

### What is committed vs. what stays local

**`results/` is tracked in git** (changed 2026-08-03) - 1069 files, ~67 MB. Every number in the
report has to be checkable by a reader who cannot rerun the cluster, and a derived table alone
asks that reader to take the derivation on trust, so the raw artifacts are committed too:

| Committed | Why |
|---|---|
| `results/<run>/driver-seed*.csv` | client-observed per-request TTFT / E2E / ITL / tokens |
| `results/<run>/prom/*.json`, `results/<run>/dcgm.csv` | engine + router Prometheus series and GPU utilization over the run window |
| `results/<run>/run.json` | arm, β, rate, router image + imageID, git commit, workload manifest with per-seed SHA-256 - the provenance of every cell |
| `results/summary-per-seed.csv` | the derived per-seed table: latency percentiles, throughput, error counts, and load imbalance |

Two exclusions. `results/**/*.jsonl` - the frozen workload replay files, regenerable
bit-identically from `benchmarks/workloads/manifest.json`, megabytes per run for no
reviewability. And `results/**/job.log` (#27) - the base64 transport that the tracked
`driver-seed*.csv` beside it decodes to, so committing it would double the repo to store the
same bytes twice. Regenerate the derived table with:

```bash
python3 benchmarks/export_summary.py results/<run>... --out results/summary-per-seed.csv
```

The `run` column is the sort key and comes first on purpose: `cell` alone is ambiguous, since
the same cell name appears in the 7.5 req/s pilot and the 10.5 req/s amended sweep, and
grouping by it silently merges two different experiments.

### Which latency source is trustworthy

Driver CSVs are the only **per-request** latency source: the router exposes average-latency
gauges only, and its `gpu_prefix_cache_*` gauges are dead (0.0) in this build - both ignored.
Per-request is not the same as trustworthy, though. The driver has so far run on a laptop
against the cluster's public route, and **45-59% of every recorded `ttft_s` is that network**.
Measured RTT to the route host: avg 44.4 ms (min 18.7, max 132, sd 39.7). Over one evening the
non-engine component of client TTFT moved **258 -> 478 ms while engine-side TTFT stayed flat at
0.168 -> 0.180 s** - a per-cell systematic offset larger than the effect under study, so more
seeds cannot average it away. The signature is a floor shift (p10 rose 2.0x), which is what a
constant network offset looks like and not what a routing policy does.

Two consequences for reading anything under `results/`:

- **Load imbalance is server-side**, derived from Prometheus over the run window, and therefore
  immune to all of this.
- Engine-side `vllm:time_to_first_token_seconds_bucket` is the clean latency cross-check. It is
  windowable per seed - seeds replay sequentially and each seed's window derives from `send_ts`
  in its `driver-seed<N>.csv` - so the paired design survives on it. It is also coarse: fixed
  histogram buckets against a small effect, with p99 sitting where little of the mass lives.
  A cross-check, not a substitute for per-request p95/p99.

The fix is the instrument, not the metric: the driver now runs **in-cluster** (#27), so
client-observed per-request TTFT is measured with no WAN in it. Switching to engine-side data
instead would have been metric-switching after seeing a null, which is indefensible however
good the reasoning; re-running the *originally pre-registered* metric on a *fixed instrument*
is not. Subtracting a measured RTT baseline was also considered and rejected - a per-request
correction that cannot be verified.

**This divides `results/` in two.** Every cell whose `run.json` has no `driver` block was
recorded over the WAN and its `ttft_s` carries that offset; cells with `driver.location:
in-cluster` do not. Do not pool the two, and read any pre-#27 client TTFT as an upper bound
with a per-cell constant in it. Load imbalance is unaffected in both, being server-side.

## Statistics (pre-registered)

- One seed replay = **one observation** (n=6 per cell). Per-request samples are
  queue-correlated and never treated as independent evidence.
- Headline test: one-sided **exact Wilcoxon signed-rank** on the 6 paired per-seed TTFT
  p95 differences (loadaware-b0.1 − kvaware), p < 0.05.
- Effect size: median relative reduction with a bootstrap 95% CI over the paired
  differences (seeded, 10 000 resamples).
- Any pairwise cell comparison is significance-capable (all cells get all 6 seeds).

## Validity rules (pre-registered - no post-hoc exceptions)

1. Error requests are **excluded from latency stats but counted**; a seed with **> 1%
   errors invalidates the run** (`analyze.py validate` enforces this).
2. Wrong image/config state (unexpected router image, patch overlay mounted, β not as
   labeled) = **discard the run, never "correct" it**. `run.json` records image + imageID
   for the audit.
3. A run without a passing registry probe + warm-up gate is not a measurement (#13:
   with an empty KV registry both arms silently degrade to QPS routing and look identical
   for the wrong reason).
4. The workload manifest check must pass before every cell - a drifted dataset is not the
   frozen dataset.

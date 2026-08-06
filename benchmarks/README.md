# Benchmark harness

> status: live · 2026-08-05 · implements the locked methodology (issue #3); the resolution
> comment there is the spec - this README is the operator's manual.

Measures **client-observed** latency for the routing-policy comparison on the gapu-2
cluster (2×A10, vLLM Production Stack + LMCache). Pre-registered headline: *loadaware as
shipped (β=1.0) reduces TTFT p95 vs kvaware* under a Zipfian shared-prefix
workload at fixed load.

## Layout

| Piece | Role |
|---|---|
| `workload_gen.py` | Zipfian prefix workload generator (pure, seeded, unit-tested) |
| `freeze_workloads.py` | Materialize + verify the frozen seed files against their manifest (`--profile zipfian` or `novel`) |
| `load_driver.py` | Async open-loop (Poisson) / closed-loop driver; per-request CSV; streaming TTFT |
| `in_pod.sh` | The measured replay, run **inside the cluster** by the bench image (#27) |
| `verify_dataset.sh` | Regenerate + verify the frozen workload; called by `in_pod.sh` and by CI |
| `bench_job.sh` | Laptop side: emit/apply the driver Job, wait it out, pull the log back |
| `collect_job.py` | Parse that log into per-seed CSVs + the measurement window; all-or-nothing |
| `warmup.py` | Unmeasured passes over the prefix pool before each cell |
| `collectors/prom_dump.py` | Prometheus `query_range` dump for the run window |
| `collectors/dcgm_poll.py` | GPU util/power/mem-copy CSV via the DCGM exporter |
| `run_cell.sh` | Per-cell choreography (deploy → gates → warm-up → 20 seeds → collect); `WORKLOAD_PROFILE` picks the dataset |
| `run_sweep.sh` | All 5 cells, one unattended batch (~3 h) |
| `rate_pilot.sh` | Step 0: find the TTFT-p95 knee on kvaware; freeze ~75% of it |
| `analyze.py` | Per-seed summaries, validity gate, pre-registered Wilcoxon + bootstrap CI |
| `export_summary.py` | Derives `results/summary-per-seed.csv` — the table every figure and test reads |
| `plot_results.py` | **Generates every committed figure** in `docs/figures/` |
| `utilization.py` | §3 utilization (GPU, GPU memory, CPU, host memory) with a series-coverage gate |
| `load_gate.py` | Is the offered rate actually degrading anything? Run before a sweep is funded |
| `scarcity_gate.sh` | One-shot precondition probe. **Historical**: it produced the 128-prefix / s=0.9 amendment; its `PROBE_RATE` default is the retired 7.5 |
| `cold_start.sh` | Cold-cache variant of a cell |
| `utilization.py` | §3 utilization readout (GPU / GPU memory / CPU / memory) + the coverage gate |

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

## The second profile: novel prompts (cache overhead)

The guidelines name **two** workload profiles. The Zipfian dataset above is the first
(repetitive prompts, stressing hit/miss). The second is *novel long prompts, unlikely to be
cached* — there to measure what the cache **costs**, not what it saves.

`workloads/novel/` holds 6 seeds × 500 requests where **every prompt is unique from its first
token**, pinned by its own `workloads/novel/manifest.json`. Prompt sizing mirrors the Zipfian
profile so the only difference between the two workloads is reuse.

```bash
python3 benchmarks/freeze_workloads.py --profile novel      # verify against the manifest
WORKLOAD_PROFILE=novel benchmarks/run_cell.sh <cell> <arm> <rate>
```

`WORKLOAD_PROFILE` threads all the way through: `run_cell.sh` pre-flights it, `bench_job.sh`
puts it in the pod environment, `verify_dataset.sh` reconstructs the right dataset in-cluster,
and it lands in `run.json` as provenance. Each profile is a **directory containing its own
`manifest.json`**, which is what lets `verify_dataset.sh` keep copying the manifest into a
writable directory — `/app` is read-only under the restricted SCC.

Two manifests rather than one config with a flag: the Zipfian manifest stores
`dataclasses.asdict(WorkloadConfig)` per seed, so adding a field to that dataclass would change
every committed hash and the runner would refuse to measure. `NovelWorkloadConfig` is a separate
class for exactly that reason, and a unit test pins the field list of the original.

`freeze_workloads.py` **asserts a reuse factor of exactly 1.0** on every novel seed before it
writes the manifest. A workload that quietly started sharing prefixes would measure cache
benefit while claiming to measure cache cost — and nothing in the resulting numbers would look
wrong.

The cache-off comparator and its pre-registered expectation are in
[`deploy/nocache-arm.md`](../deploy/nocache-arm.md). Ticket: #25.

## Sweep design

**5 cells × 20 seeds × 500 requests**, identical frozen workload and a fixed Poisson rate in
every cell. The grid is `BETA_GRID` in `run_sweep.sh:82`, default `0 0.5 1.0 2.0`:

| Cell | Arm | Router image |
|---|---|---|
| `kvaware` | baseline (headline comparator) | pinned stock |
| `loadaware-b0` | loadaware β=0 (cache-only ablation) | CI-built, SHA-tagged |
| `loadaware-b0.5` | loadaware β=0.5 (**configuration of record — headline**) | CI-built, SHA-tagged |
| `loadaware-b1.0` | loadaware β=1.0 (shipped default) | CI-built, SHA-tagged |
| `loadaware-b2.0` | loadaware β=2.0 | CI-built, SHA-tagged |

`roundrobin` is run separately as a descriptive framing cell, not as part of the sweep. Cell
names outside this grid (`b0.1`, `b0.25`, `b0.034`, `b4.0`) appear in older `results/`
directories and **can no longer be generated** — they came from the retired per-rate β
calibration, before the load term was normalized against the fleet mean.

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
python3 benchmarks/analyze.py compare results/<...loadaware-b0.5> results/<...kvaware>

# 4. figures. --cand names the headline arm and is REQUIRED: the 4-point beta grid
#    always yields three non-b0 cells, so nothing can infer it (#30).
python3 benchmarks/export_summary.py results/<...> --out results/summary-per-seed.csv
python3 benchmarks/plot_results.py results/<...> --cand loadaware-b0.5 --out docs/figures
python3 benchmarks/utilization.py report results/<...>

# 4. §3 utilization: GPU, GPU memory, CPU, host memory
python3 benchmarks/utilization.py report results/*
```

### Utilization (§3): where each number comes from

Decided on [#35](https://github.com/BenEpstein/caching-in-llms/issues/35).

| §3 requirement | Source | Scope |
|---|---|---|
| GPU utilization | `DCGM_FI_DEV_GPU_UTIL` / `_POWER_USAGE` / `_MEM_COPY_UTIL` (`dcgm.csv`) | per GPU |
| GPU memory | `vllm:kv_cache_usage_perc` | per engine |
| Memory | `lmcache:local_cache_usage` (engines) + `process_resident_memory_bytes`, `router_memory_usage_percent` (router) | per engine + router |
| CPU | `process_cpu_seconds_total`, `router_cpu_usage_percent` | **router only** |

DCGM is the GPU source of record because vLLM's `/metrics` exposes 113 metric names and
none of them is SM% or power - there is no Prometheus substitute. Promoting DCGM into
Prometheus would need a `RoleBinding` in `nvidia-gpu-operator`, which costs the property
that `oc apply -f deploy/` works in any namespace without cluster-admin.

**Engine host-CPU and engine RSS are not available.** vLLM registers no `process_*`
collector at all (verified at the endpoint, not inferred from absence), so those two
numbers are reported as missing rather than substituted. GPU SM% is the engines'
utilization number; the router is the only component the extension changes, so the
router's CPU is the one that answers "what does this policy cost".

**Coverage gate.** `run_cell.sh` records `utilization_coverage` in each `run.json`: the
fraction of `[CELL_START, CELL_END]` that each series actually spans. Below 0.95 it
**warns and never fails** - the driver CSVs are the primary measurement and a cell with
good latency data must not be discarded over utilization sampling. It exists because
`dcgm.csv` once stopped 171 s before a 712 s window closed (24% of the cell, as a clean
tail truncation) and nothing noticed. The DCGM port-forwards now run under supervisors
that reconnect, which narrows the window but cannot close it - nothing laptop-side can.

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
                            `driver` - where the replay ran (node, bench image, target URL) - and
                            `utilization_coverage`, the fraction of the window each utilization
                            series actually spans (#35)
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

- One seed replay = **one observation** (**n=20 per cell**, every cell). Per-request samples
  are queue-correlated and never treated as independent evidence.
- Headline test: one-sided **exact Wilcoxon signed-rank** on the 20 paired per-seed TTFT
  p95 differences (`loadaware-b0.5` − `kvaware`), threshold **0.025** (Bonferroni for two
  co-primaries; the second is load imbalance). Pre-registered on #31.
- Effect size: median relative reduction with a bootstrap 95% CI over the paired
  differences (seeded, 10 000 resamples).
- Any pairwise cell comparison is significance-capable (all cells get all 6 seeds).

## Goodput (`ttft_slo_miss`) - EXPLORATORY, not pre-registered

```bash
python3 benchmarks/analyze.py compare --metric ttft_slo_miss [--slo 0.15] <cand-dir> <base-dir>
```

**Goodput** is the fraction of requests *sent* whose first token arrived under a TTFT
objective. It was computed for the first time **after** the pre-registered `ttft_p95` test
returned null on the 2026-08-06 confirmatory sweep, so every number it produces on that data
is exploratory and `compare` prints that caveat beside the p-value. A performance claim needs
a fresh run whose pre-registration fixes the metric and the objective in advance.

- **The tunable is `--slo`**, in SECONDS, defaulting to `analyze.TTFT_SLO_S` = 0.150. The
  default is provisional: on the confirmatory sweep the effect is broad rather than peaked
  (7.4 points at 150 ms, 8.2 at 124 ms), so the value is a choice to justify on service
  grounds, not to read off the data.
- **The tested quantity is the MISS rate**, `1 − goodput`. Lower-is-better, so it runs
  through the same committed Wilcoxon and the same relative-reduction bootstrap as
  `ttft_p95`, with no inverted test and no new statistics. Figures plot the complement.
- **Errors count as misses.** This is the one statistic here whose denominator is requests
  sent rather than requests answered: percentiles describe service delivered, goodput
  describes service promised. At the 0.2-0.5% error floor on the committed runs the two
  denominators differ by well under a point.
- **`compare` refuses an objective the baseline never misses** on some seed - a relative
  reduction against zero is undefined, and a seed the baseline already passes perfectly
  cannot show an improvement.
- Deliberately **absent from `results/summary-per-seed.csv`**: that table is the evidence a
  reader checks the report against, and baking one provisional objective into it would read
  as an objective already chosen. The driver CSVs are committed, so any objective is
  recomputable. It does appear in `results/expected/figure-data.json`, which backs
  `fig12-goodput.png`.

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

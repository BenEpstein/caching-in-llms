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
| `load_driver.py` | Async open-loop (Poisson) driver; per-request CSV; streaming TTFT |
| `in_pod.sh` | The measured replay, run **inside the cluster** by the bench image (#27) |
| `verify_dataset.sh` | Regenerate + verify the frozen workload; called by `in_pod.sh` and by CI |
| `bench_job.sh` | Laptop side: emit/apply the driver Job, wait it out, pull the log back |
| `collect_job.py` | Parse that log into per-seed CSVs + the measurement window; all-or-nothing |
| `warmup.py` | Unmeasured passes over the prefix pool before each cell |
| `collectors/prom_dump.py` | Prometheus `query_range` dump for the run window |
| `collectors/dcgm_poll.py` | GPU util/power/mem-copy CSV via the DCGM exporter |
| `run_cell.sh` | Per-cell choreography (deploy → gates → warm-up → 20 seeds → collect); `WORKLOAD_PROFILE` picks the dataset |
| `run_sweep.sh` | All 5 cells, one unattended batch (~3 h) |
| `rate_pilot.sh` | Step 0: find the TTFT-p95 knee on kvaware; freeze at or just under it (see "Picking the rate") |
| `analyze.py` | Per-seed summaries, validity gate, pre-registered Wilcoxon + bootstrap CI |
| `export_summary.py` | Derives `results/summary-per-seed.csv` — the table every figure and test reads |
| `plot_results.py` | **Generates every committed figure** in `docs/figures/` |
| `utilization.py` | §3 utilization (GPU, GPU memory, CPU, host memory) with a series-coverage gate |
| `load_gate.py` | Is the offered rate actually degrading anything? Run before a sweep is funded |
| `scarcity_gate.sh` | One-shot precondition probe. **Historical**: it produced the 128-prefix / s=0.9 amendment; its `PROBE_RATE` default is the retired 7.5 |
| `cold_start.sh` | Cold, stale-free restart before each cell: registration ordering (#13) + stale-id assertion |

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

A dedicated cache-OFF comparator arm was considered and retired without running: #25 closed
as premise-wrong - §3 offers the two workload profiles as examples of a test suite, not as
requirements, and the novel profile above already measures what the cache costs.

## Sweep design

**7 cells × 20 seeds × 500 requests**, identical frozen workload and a fixed Poisson rate in
every cell. The grid is `BETA_GRID` in `run_sweep.sh`, default `0.5 1.0 2.0 0.25 0` - the
pre-registered order: kvaware first, b0.5 adjacent to it, **b0 last of the loadaware cells** as
the drift sentinel (#31), with `roundrobin` trailing:

| Cell | Arm | Router image |
|---|---|---|
| `kvaware` | baseline (headline comparator) | pinned stock |
| `loadaware-b0` | loadaware β=0 (cache-only ablation) | CI-built, SHA-tagged |
| `loadaware-b0.25` | loadaware β=0.25 (descriptive, low end) | CI-built, SHA-tagged |
| `loadaware-b0.5` | loadaware β=0.5 (**configuration of record - headline**) | CI-built, SHA-tagged |
| `loadaware-b1.0` | loadaware β=1.0 (shipped default) | CI-built, SHA-tagged |
| `loadaware-b2.0` | loadaware β=2.0 | CI-built, SHA-tagged |
| `roundrobin` | cache-blind comparator (descriptive) | pinned stock |

`roundrobin` and `b0.25` are **descriptive** cells: they carry no p-value, so the pre-registered
alpha=0.025 pair is unaffected and no multiplicity adjustment is owed for them. `roundrobin`
saturates at the sweep rate and is therefore not at the same operating point as the other arms -
report its throughput shortfall, never its latency ratio, and pass it to `plot_results.py` as
`--comparator`.

Cell names outside this grid (`b0.1`, `b0.034`, `b4.0`) came from the retired per-rate β
calibration, before the load term was normalized against the fleet mean, and **can no longer be
generated**. Their run dirs were pruned from the working tree by #57 and are in git history.

Built images only - the dev-loop ConfigMap overlay (`deploy/dev/`) is **never** measured.
No mock or simulation anywhere: every reported number comes from the real cluster.

### Why the operating point is rate 16 / OSL 64

OSL 64 (`run_cell.sh` `MAX_TOKENS`) is a deliberate choice, not the driver default it used
to be - and it is passed explicitly, so `run.json` records it. **Caveat for older runs:**
`osl_tokens` postdates the 2026-08-04 cells, so earlier run dirs omit the key entirely and
ran at the driver default of 64. Piloted 2026-08-05 under `results/osl-pilot/`
(`20260805-002149-kvaware` at OSL 128, `20260805-003128-kvaware` at 256, both at rate 16);
those dirs were pruned by #57 and are in git history, so the outcome is recorded here rather
than recomputable:

| OSL | outcome |
|---|---|
| **64** | achieved tracks offered; imbalance 2.99× |
| 128 | fleet saturates (65% of offered achieved); imbalance 3.98× |
| 256 | breaches the 10% catastrophic error gate (validity rule 1) at 11.4%, KV pegged at 1.000, 231 preemptions; imbalance 1.89× |

Imbalance is **non-monotonic in load**: once both engines pin at capacity there is nowhere
better to send anything, so the window in which load-aware routing can help closes at
**both** ends - too little load and there is no queue to route around, too much and both
engines are equally saturated. On this 2×A10 fleet, rate 16 / OSL 64 sits inside that
window; moving either knob moves the experiment out of it. `roundrobin` **saturates** here -
10.40 req/s achieved against 16 offered (65%) in `20260806-144135-roundrobin` - which is the
point: it is reported as the cache-blind capacity floor, not as a tuned arm.

β=0.25 was excluded from the grid on n=3 evidence (imbalance 2.257 against a same-hour kvaware
control of 2.113 - no effect); that cell was pruned by #57 and the numbers are recorded here.
It is back in the grid as a descriptive cell, and at n=20 the picture is stronger than "no
effect": β=0.25 is **worse than the baseline** on both metrics (imbalance -21.0%, TTFT p95
-20.7% with a bootstrap CI of [-43.4%, -2.8%], entirely below zero). The useful range still
starts at 0.5, and the response to β is non-monotonic at the low end, not merely flat.

### Two eras of `beta`

`beta` named two different policies. **Every run dir now in the tree is relative-era** - the
absolute-era cells were pruned by #57 - so this matters only when reading git history or the
CHANGELOG, never when comparing what is on disk. The split is `git_commit` in each `run.json`:

| `git_commit` | Load term |
|---|---|
| before `7e2dffb` | `beta ×` **absolute** in-flight count |
| `7e2dffb` onward | `beta × (load − fleet_mean) / max(1, mean)` |

The same cell name means a different policy on each side, and the values do not convert by a
constant: the relative form self-adjusts per request while the absolute one does not, so
`beta_rel = beta_abs × mean_load` holds only at the mean and mispredicted the observed values
by ~2× when checked (#22). Never pool across the boundary without saying which side a cell is
on.

- Absolute-era values seen: 0, 0.034, 0.068, 0.1, 0.5, 1.0
- Relative-era values seen: 0, 0.25, 0.5, 1.0, 2.0
- `beta=0` is the one value that means the same thing on both sides (the load term
  vanishes), which is why the ablation cells are poolable and the rest are not.

The relative-era grid (`0.5 1.0 2.0`) brackets the absolute-era optimum converted by
`beta_rel = beta_abs × mean_load` (#22): the TTFT optimum lands at 0.90-0.93 and the ITL
optimum at 1.34, so the whole measured tradeoff lives between 0.9 and 1.35 with the default
between them.

## Deploying the stack (OpenShift, cluster `gapu-2`)

Target topology: 1 CPU router pod → 2 vLLM+LMCache replicas, one per A10 GPU.
Prereqs: `oc` logged in with rights to create namespaces/SCC bindings, `helm` v3, and a
HuggingFace token if the model is gated (the Llama family is).

```bash
oc new-project cache-llm
oc create secret generic hf-token-secret \
  --from-literal=HF_TOKEN=<your token> -n cache-llm        # gated models only
helm repo add vllm https://vllm-project.github.io/production-stack
helm install stack vllm/vllm-stack -n cache-llm \
  -f deploy/values-baseline-kvaware.yaml \
  --set "servingEngineSpec.modelSpec[0].modelURL=<MODEL>" \
  --set "servingEngineSpec.modelSpec[0].hf_token.secretName=hf-token-secret" \
  --set "servingEngineSpec.modelSpec[0].hf_token.secretKey=HF_TOKEN"
oc get pods -n cache-llm -w
```

Pin the chart version you actually used (`--version`); the committed runs used 0.1.11.
Access without port-forward (VPN required, self-signed cert → `curl -k`):

```bash
oc create route edge llm --service=stack-router-service --port=router-sport -n cache-llm
oc create route edge grafana --service=stack-grafana --port=http-web -n cache-llm
```

Sanity check after install: send two completions sharing a long prefix; the second must land
on the same pod (router logs, `grep -i routing`).

### Cluster gotchas (each bit once; the fix lives in code, this list is the index)

- **The chart's router Service is missing the LMCache controller ports** (upstream bug):
  workers register on reply port 9001 and heartbeat on 9002, the chart exposes only 9000,
  and registration hangs **silently** - every lookup misses and kvaware degrades to QPS
  routing with no error anywhere. `run_cell.sh` re-applies the port patch before every
  cell. Diagnose via `Registered instance-worker` router log lines (this router build
  exposes no registered-workers gauge). Upstream-PR candidate.
- **lmcache version skew router↔engine fails silently** (msgspec ZMQ schema drift): both
  images are digest-pinned to the same lmcache minor - see `Dockerfile` and the image pins
  in `deploy/values-baseline-kvaware.yaml`. Check live:
  `oc exec <pod> - /opt/venv/bin/python3 -c "from importlib.metadata import version; print(version('lmcache'))"`.
- **The router image has no writable HF cache, so the tokenizer load fails on every
  request** (#21): arbitrary uid under the restricted SCC, `HF_HOME` unset, no `/.cache`.
  `AutoTokenizer.from_pretrained` raises, and because the except path never assigns
  `self.tokenizer` it is retried *per request* (reaching huggingface.co before failing) -
  ~245 ms of event-loop blocking per request, which caps a single-event-loop router at
  ~4 req/s, and eventually the liveness SIGKILL. `/tmp` is the only writable path, and chart 0.1.11 gives the router neither a
  `routerSpec.env` passthrough nor a volume hook, so the fix cannot live in the values
  files: `run_cell.sh` (also `rate_pilot.sh`, `scarcity_gate.sh`) sets `HF_HOME=/tmp/hf`
  via `oc set env` on **both arms**. `KvawareRouter.route_request` carries the identical
  try/except, so an arm-only fix would flatter loadaware and void the comparison.
- **A router restart re-registers workers only because `workerHeartbeatTime` is set**, and
  the KV registry stays blind ~40 s afterwards (#13) - admits in that window are lost for
  the life of the engine process. Measurements gate on `deploy/dev/registry-probe.sh`;
  `cold_start.sh` owns the stale-free ordering. Broken-state symptom: router logs show
  `Routing request ... with session id None` and no `found by kvaware router` lines;
  recovery is an engine rollout restart.
- **Four more, each pinned as a comment beside the setting in
  `deploy/values-baseline-kvaware.yaml`:** RollingUpdate deadlocks on full GPUs
  (`strategy: Recreate`), arbitrary-UID pods need `HOME=/tmp`, the router startup probe
  needs a relaxed `failureThreshold`, and the shared model PVC needs RWX (CephFS, not
  ceph-rbd RWO).
- Router discovers engines via the K8s API; the chart's RBAC handles it. Verify:
  `oc auth can-i list pods --as=system:serviceaccount:cache-llm:stack-router-service-account`.

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

# 5. §3 utilization: GPU, GPU memory, CPU, host memory
python3 benchmarks/utilization.py report results/*
```

### Picking the rate (step 0)

`rate_pilot.sh` prints offered vs **achieved** req/s per rate; the knee is where they
diverge and TTFT p95 elbows, and the frozen sweep rate is at or just under it. **Read the
achieved column, not only the latencies.** The rate range must bracket the knee on both
sides: a dead-flat TTFT p95 across the whole range means the pilot never reached the knee,
not that there is no knee. A rate frozen from inside the flat region leaves
`vllm:num_requests_waiting` at 0.00 on every engine - there is no load for a load-aware
router to be aware of, and the sweep measures cache locality alone. On gapu-2 the knee is
**14–16 req/s** (20 offered yields 14.9 achieved); the committed confirmatory sweeps run at 16.

### Reproducing the reported numbers

`scripts/reproduce.sh` regenerates every number in §5/§6 from committed data - no cluster,
no GPU, no network - and exits non-zero on drift (#28). CI runs it on every push.

```bash
./scripts/reproduce.sh            # verify
./scripts/reproduce.sh --update   # accept current output as the new results/expected/ baseline
```

`--update` refreshes `results/expected/` only. It never writes
`results/summary-per-seed.csv`: that file is committed evidence, and the regenerated table
is a strict subset whenever a run directory is missing, so "updating" it would silently
delete real rows.

**If you regenerate `docs/figures/` from a new sweep, repoint the script's cell defaults in
the same commit** (`HEADLINE`, `BASELINE`, `ABLATION`, `BETA1`, `BETA2`, `COMPARATOR` - all
env-overridable). They currently name the confirmatory sweep (#31). Leave them behind and
the script diffs a superseded experiment against its own superseded baseline and passes
while the committed figures go unchecked - a check that verifies the wrong run reads
exactly like a check that passes.

### Utilization (§3): where each number comes from

Decided on [#35](https://github.com/BenEpstein/caching-in-llms/issues/35).

| §3 requirement | Source | Scope |
|---|---|---|
| GPU utilization | `DCGM_FI_DEV_GPU_UTIL` / `_POWER_USAGE` / `_MEM_COPY_UTIL` (`dcgm.csv`) | per GPU |
| GPU memory | `vllm:kv_cache_usage_perc` | per engine |
| Memory | `lmcache:local_cache_usage` (engines) + `process_resident_memory_bytes`, `router_memory_usage_percent` (router) | per engine + router |

`vllm:kv_cache_usage_perc` is scraped in-cluster and so is immune to the WAN that truncates
`dcgm.csv`; it is also the resource the policy contends for. `lmcache:local_cache_usage` is
the host RAM the LMCache CPU backend holds.
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

### Per-cell choreography (`run_cell.sh`)

`helm upgrade` with the cell's values (chart
**pinned to 0.1.11** - the installed version; 0.1.12+ has schema drift) → dev-overlay
check (a mounted `router-patch` ConfigMap is auto-reverted - validity rule 2) → β via
`oc set env` (chart 0.1.11 ignores `routerSpec.env`; baselines get the vars *removed* so
a stale β can't leak through the three-way merge) → router-Service controller-port patch
(the upstream chart bug above) → **router image asserted against the cell's label** (validity
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
  driver-seed{1..20}.csv    client-observed per-request TTFT/E2E/tokens (percentile source of
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

**`results/` is tracked in git** (changed 2026-08-03) - 496 files, ~54 MB across 11 run dirs in
two generations; `results/README.md` is the index. Every number in the
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
Per-request is not the same as trustworthy, though. The driver **used to** run on a laptop
against the cluster's public route, and **45-59% of every recorded `ttft_s` was that network**.
Measured RTT to the route host: avg 44.4 ms (min 18.7, max 132, sd 39.7). On the retained WAN
cells (`results/20260805-0*`) the non-engine component of client TTFT swings **121 -> 195 ms
between two cells an hour apart** while engine-side TTFT stays in a 133-159 ms band - a per-cell
systematic offset larger than the effect under study, so more seeds cannot average it away. The
signature is a floor shift, which is what a constant network offset looks like and not what a
routing policy does.

**The driver now runs in-cluster** and every reported number comes from that instrument: same
decomposition on the confirmatory cells is 48.5 ms non-engine (25.8%) for `kvaware` and 45.2 ms
(27.2%) for β=0.5. `results/README.md` carries the full per-cell table for both generations.

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
- Any pairwise cell comparison is significance-capable (all cells get all 20 seeds).
- **Why n=20.** The exact one-sided Wilcoxon's resolution is bounded by the pair count, not
  by the effect size: the smallest attainable p is **0.125 at n=3** and **0.031 at n=5**,
  both above the 0.025 threshold however large the effect. **n=10** is the first that
  survives a single reversal (p=0.0107), and the published imbalance headline was 19/20 -
  exactly one reversal. n=20 buys margin for two or three. Do not trim seeds to save
  cluster time: below n=10 a real effect cannot reach the threshold at all.

## Goodput (`ttft_slo_miss`) - secondary metric

```bash
python3 benchmarks/analyze.py compare --metric ttft_slo_miss [--slo 0.15] <cand-dir> <base-dir>
```

**Goodput** is the fraction of requests *sent* whose first token arrived under a TTFT
objective. It is computed from the same committed driver CSVs as every other statistic here,
over the same cells, and reported alongside the two co-primaries rather than in place of
them.

- **The tunable is `--slo`**, in SECONDS, defaulting to `analyze.TTFT_SLO_S` = 0.150. **Report
  the sweep, not one point.** The result does not rest on the default: on the confirmatory
  sweep the effect is a broad plateau rather than a peak (7.4 points at 150 ms, 8.2 at 124 ms)
  and `fig12-goodput.png` draws the whole 50-400 ms range. Quoting a single objective without
  the curve beside it overstates how much the choice matters.
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
  reader checks the report against, and baking one objective into it would read
  as an objective already chosen. The driver CSVs are committed, so any objective is
  recomputable. The per-seed miss rate at the default objective, and the full 50-400 ms
  curve `fig12-goodput.png` draws, both appear in `results/expected/figure-data.json`, so
  `reproduce.sh` checks the numbers the committed figure actually plots.

## Validity rules (pre-registered - no post-hoc exceptions)

1. Error requests are **excluded from latency stats but counted**. **Amended (#3,
   pre-registered before the run)**: a seed above 1% errors is **reported, not fatal** - the
   observed error floor is arm-independent (`aiohttp ServerDisconnectedError` raised *after*
   the routing decision, present in every arm including `roundrobin`), and a floor that hits
   both arms equally is noise, not bias. What voids a comparison is errors **differing
   between the arms** (`analyze.py compare`: ratio > 2× **and** absolute gap > 1 pp). A
   single seed above **10%** is catastrophic and voids unilaterally (`analyze.py validate`).
   Rule of record: the block above `ALPHA` in `benchmarks/analyze.py`. Probe evidence
   behind the amendment: issue #3.
2. Wrong image/config state (unexpected router image, patch overlay mounted, β not as
   labeled) = **discard the run, never "correct" it**. `run.json` records image + imageID
   for the audit.
3. A run without a passing registry probe + warm-up gate is not a measurement (#13:
   with an empty KV registry both arms silently degrade to QPS routing and look identical
   for the wrong reason).
4. The workload manifest check must pass before every cell - a drifted dataset is not the
   frozen dataset.
5. The **realised KV pool**, read from the engine's own startup log (`GPU KV cache size`),
   must match the prediction for the configured `gpuMemoryUtilization` (`0.45` → ~99k
   tokens; the prediction is pinned beside the setting in
   `deploy/values-baseline-kvaware.yaml`). A cell whose realised pool does not match is
   discarded. **This is a manual check** - `scarcity_gate.sh` prints the realised pool but
   is a one-shot off the sweep path, and nothing enforces the comparison automatically.
6. **Preemption is recorded and reported per arm, never used to void a run** (#3
   amendment): under concentration it is a genuine consequence of the baseline's placement,
   so gating on it would discard the baseline arm systematically. Caveat: the
   `vllm:num_preemptions_total` dump postdates the nine pre-amendment 2026-08-03 cells, where
   the file is absent and `load_gate` reports 0 preemptions because there is no data, not
   because there were none. Those dirs were pruned by #57, so every run now in the tree carries
   the dump and the caveat applies only to git history.

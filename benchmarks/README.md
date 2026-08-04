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
| `warmup.py` | Unmeasured passes over the prefix pool before each cell |
| `collectors/prom_dump.py` | Prometheus `query_range` dump for the run window |
| `collectors/dcgm_poll.py` | GPU util/power/mem-copy CSV via the DCGM exporter |
| `run_cell.sh` | Per-cell choreography (deploy → gates → warm-up → 6 seeds → collect) |
| `run_sweep.sh` | All 6 cells, one unattended batch (~3 h) |
| `rate_pilot.sh` | Step 0: find the TTFT-p95 knee on kvaware; freeze ~75% of it |
| `analyze.py` | Per-seed summaries, validity gate, pre-registered Wilcoxon + bootstrap CI |

## The frozen workload

ONE dataset (issue #3): 20 prefixes × 2048 tokens (+32-token unique suffix), Zipf s=1.2
(`pool_seed=42`), replayed as **6 seeds × 500 requests**. Seeds share the prefix pool and
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

# 2. the sweep (LOADAWARE_TAG = git short SHA of the CI-built router image on Quay)
LOADAWARE_TAG=<sha> benchmarks/run_sweep.sh <rate>

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
6 seeds replayed back-to-back, no reset between seeds
(steady-state) → Prometheus dump + DCGM CSV (one port-forward **per exporter pod** - the
DaemonSet Service would pin to one node's GPU) + `run.json` manifest → validity check.

## What a run directory contains

```
results/<ts>-<cell>/
  driver-seed{1..6}.csv     client-observed per-request TTFT/E2E/tokens (percentile source of
                            truth; send_ts is wall-clock epoch, so per-seed windows derive from it)
  prom/*.json               vllm:num_requests_running/waiting, request_queue_time, kv_cache_usage,
                            lmcache hit metrics, process + router CPU/mem - per engine, 5 s resolution
  dcgm.csv                  GPU_UTIL / POWER_USAGE / MEM_COPY_UTIL per GPU, ~5 s samples
  run.json                  arm, β, rate, image + imageID, git commit, workload manifest, window
```

`analyze.py compare` refuses to pair two runs whose `run.json` rate or workload manifest
differ - the methodology's "identical workload across arms" is enforced, not assumed.

### What is committed vs. what stays local

`results/` is gitignored - driver CSVs and Prometheus dumps run to megabytes. But every number
in the report has to be checkable by a reader who cannot rerun the cluster, so two derived
artifacts are **force-added** to git (`git add -f`, rather than punching holes in `.gitignore`):

| Committed | Why |
|---|---|
| `results/<run>/run.json` | arm, β, rate, router image + imageID, git commit, workload manifest with per-seed SHA-256 - the provenance of every cell |
| `results/summary-per-seed.csv` | the derived per-seed table: latency percentiles, throughput, error counts, and load imbalance |

Between them a reader can reproduce every figure, every percentile, and both co-primary
statistical tests without the raw per-request data. Regenerate with:

```bash
python3 benchmarks/export_summary.py results/<run>... --out results/summary-per-seed.csv
```

The `run` column is the sort key and comes first on purpose: `cell` alone is ambiguous, since
the same cell name appears in the 7.5 req/s pilot and the 10.5 req/s amended sweep, and
grouping by it silently merges two different experiments.

Driver CSVs are the only percentile-capable latency source (the router exposes only
average-latency gauges; engine TTFT histograms start their clock at the engine and miss
router overhead). Router `gpu_prefix_cache_*` gauges are dead (0.0) in this build - ignored.

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

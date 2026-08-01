---
branch: claude/wayfinder-3-e533a3
date: 2026-08-01 (evening)
status: blocked
---

# Handoff: benchmark methodology grilling (ticket #3), paused on the workload decision

## Current State

Mid-grilling on [issue #3 "Benchmark methodology: workload, sweep grid, stats"](https://github.com/BenEpstein/caching-in-llms/issues/3)
(claimed by Ben, part of wayfinder map #1). Three decisions are locked; the session paused at the
**workload/prompt-profile decision** - Ben wants a dedicated exploration session on the options
before answering, then returns to this grilling with an answer.

## Modified Files

None - grilling ticket, no code changes. Branch is even with `origin/main` (merged during session).

## Test Status

- Tests: passing (`pytest` at repo root: 8 passed, `benchmarks/` only).
- Note: `tests/` (18 offline tests) + `patches/` exist only on `feat/multi-instance-lookup`
  (PR #15, **still open**), not on main yet.

## What's Done (decisions locked in this grilling, in order)

- **Q1 - Primary metric: TTFT p95**, loadaware vs kvaware. All rubric metrics (p99, mean, E2E,
  hit rate, throughput, GPU/CPU/mem util) emitted as secondary.
- **Q2 - Arms: roundrobin + kvaware + loadaware** at the main operating point; only
  kvaware-vs-loadaware across sweep grids. kvaware baseline **must run unpatched**
  (`deploy/dev/revert-router-patch.sh` first): Change 1 alters `matched_tokens` of the selected
  instance, which can flip kvaware's threshold branch (see PR #15 correction comment on #4).
- **Q2b - Mechanism metric: per-instance load-balance CV** (coefficient of variation). Report
  shape: headline (TTFT p95 down), mechanism (load CV down, GPU util balanced), cost
  (hit-rate delta ~ 0, reported honestly even if slightly negative).

## What's Left (updated after the 2026-08-01 evening continuation)

Locked since the first version of this handoff (Q4-Q5 decided, Q6 proposed):

- **Q4 LOCKED - built image, stock baseline.** Every reported number comes from Helm-deployed
  images: baselines on the pinned *stock* router image (no revert scripts involved), loadaware
  on our SHA-tagged Quay image (#16). Overlay = dev/smoke only. Makes #16 a hard blocker for #7.
- **Also LOCKED - no mock, no simulation.** No `--mock` harness mode, no offline α/β
  pre-screening in the report; every number from the real cluster.
- **Q5 LOCKED - metric sources (verified live on gapu-2, Option B for GPU):**
  - Driver CSV = client-observed TTFT/E2E/throughput (router exposes only averages, engine
    TTFT is a coarse bucketed histogram that starts at the engine - both unusable for p95).
  - Prometheus dump per run = `vllm:num_requests_running/waiting` per engine (load CV),
    `vllm:request_queue_time_seconds` (queueing mechanism), `lmcache:num_hit_tokens_total` /
    `lookup_hit_rate` / `request_cache_hit_rate` deltas (hit rate), `vllm:kv_cache_usage_perc`.
    Router's `gpu_prefix_cache_*` gauges are dead (0.0) in this build - ignore.
  - DCGM polling CSV = collector `oc port-forward`s to `nvidia-dcgm-exporter.nvidia-gpu-operator:9400`
    (verified reachable; stack Prometheus does NOT scrape it), samples GPU_UTIL/POWER/MEM_COPY_UTIL
    every ~5s. No ServiceMonitor, no cluster mutation.
  - Everything lands in `results/<run>/` with a `run.json` manifest (arm, image SHA, workload
    config+seed, rates, window timestamps). Analysis reads run dirs only.

Pending Ben's answers (being explored in his separate handoff session):

1. **Workload/prompt profile** (see Blockers below) - synthetic Zipfian vs real dataset,
   ISL/OSL, prefix pool rationale.
2. **Sweep grids** - proposed s ∈ {0.6, 1.2, 1.8}, rates ~50/75/90% of pilot-measured
   saturation, α/β grid still unproposed.
3. **Q6 statistics - proposed, awaiting confirmation:** one run = one observation; seeds
   pair arms; headline cell N=6 paired reps, Wilcoxon signed-rank one-sided p<0.05 on per-run
   p95 diffs, bootstrap CI on median relative reduction; roundrobin N=3 context only; sweeps
   N=3 trend-evidence only; 100 warm-up + 1000 measured requests, pilot stability check;
   validity rules pre-registered (>1% errors invalidates a run). ~60 runs ≈ 13-15 h.

Then: resolution comment on #3 + close + map update + CHANGELOG entry.

## Blockers

**The workload decision.** Ben's framing: the prompt profile must "best represent our usecase
to be part of the story" - fair, explainable, defensible in §5/§6. The exploration session
should produce a recommendation across:

- **Synthetic vs real dataset.** Claude's standing recommendation: synthetic Zipfian
  hot-prefix mix (already implemented in `benchmarks/workload_gen.py`, seeded/replayable),
  story anchored as "N RAG/system-prompt contexts of ~2K tokens, Zipf-distributed popularity".
  Real chat datasets (ShareGPT/LMSYS) lack controllable prefix sharing and length. Worth
  checking before deciding: what vLLM's own `benchmark_prefix_caching.py` does, what the
  LMCache/production-stack papers and Mooncake-style trace papers use for prefix-sharing
  workloads, whether any public trace has genuine long-shared-prefix structure.
- **ISL/OSL.** Constraint: prompts must exceed `kv_aware_threshold` (default 2000 tokens) or
  kvaware never takes the cache path; LMCache chunks are 256 tokens. Proposed: ISL ~ 2048
  prefix + 32 suffix, OSL = 64 (decode length doesn't affect routing; short keeps runs fast).
- **Shared-prefix rationale.** Why 20 prefixes, why 2K tokens, why Zipf and which s - the
  "dev story" narrative Ben wants to own.

## Key Context

- **Read first:** issue #3 (the question), issue #1 (map - Decisions so far),
  `docs/handoff-core-implementation.md` §5 "measurement trap", CHANGELOG top entries.
- **New facts from Eliad's session (PR #15 / issues #13, #16) that shape the methodology:**
  - #13: Controller KV registry dies on every router restart; only an **engine restart**
    revives admissions. Every arm switch = apply/revert + engine restart + warm-up (~7 min).
    #13 blocks #7 (evaluation) and its mechanism investigation is a separate ticket.
  - Change 1 is verified live: 2-holder `layout_info` observed on gapu-2. Two-holder recipe
    documented in `deploy/dev/README.md`.
  - #16: image builds go through CI → Quay (robot account, SHA tags). Its open question
    ("measure runs on the built image?") belongs to ticket #3, item 4 above.
- **Environment invariants:** 2×A10 on `gapu-2`, model pinned `Qwen/Qwen2.5-3B-Instruct`,
  router:0.1.9.dev9 + vllm-openai:v0.3.9post2 pinned pairing, `-k` for the self-signed ingress.
- **Rubric lens for every choice:** correctness 40% + reproducibility 30% dominate; a modest
  rock-solid claim beats an impressive flaky one. Deadline ~2026-08-10, benchmark methodology
  is day-3-latest.
- Grilling etiquette for the resuming session: one question at a time, recommendation per
  question, decisions are Ben's.

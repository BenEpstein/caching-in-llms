---
branch: feat/relative-load-normalization
date: 2026-08-04 22:05
status: blocked
---

# Handoff: feat/relative-load-normalization

> status: live · 2026-08-04 · numbers below were computed this session from committed
> artifacts; re-derive before quoting in the report.

## Current State

Two things happened this session. The **policy change is done and green**: `loadaware` now
normalizes load against the live fleet mean, `alpha` is deleted, 120 tests pass. The
**measurement finding is the reason this is blocked**: the driver's `ttft_s` is measured
client-side over a WAN link, so TTFT is not comparable across cells run at different times,
and the pre-registered TTFT co-primary is contaminated for every run in the project.

The open question, and the reason for this handoff: **what becomes the TTFT metric of record,
such that it is valid across benchmarks run hours or days apart, with no network in it.**
Ben is bringing a proposed solution; do not pick one unilaterally.

## Modified Files

Branched off `feat/evaluation-runs` (PR #22). Three commits: `7e2dffb` (policy), `f21d18f`
(doc arithmetic fix), `2d76f01` (TTFT rescue).

```
patches/vllm_router/routers/routing_logic.py | 138 ++-   the policy: relative_loads(), no alpha
tests/test_loadaware_routing.py              | 158 ++-   56 -> 60 tests
benchmarks/load_gate.py                      |  81 ++-   beta_from() deleted -> relative_imbalance()
benchmarks/run_sweep.sh                      |  44 ++-   fixed beta grid, no BETA_HEADLINE input
benchmarks/collectors/prom_dump.py           |  18 ++-   engine-side TTFT now collected
benchmarks/{run_cell,rate_pilot,scarcity_gate}.sh, export_summary.py, plot_results.py
patches/README.md, benchmarks/README.md, CONTEXT.md, deploy/values-loadaware-image.yaml
docs/upstream-findings.md                    |  23 ++-   Finding 5 units paragraph amended
CHANGELOG.md                                 |  64 ++-
```

Plus `results/2026-08-04-*/prom/vllm_time_to_first_token_seconds_*` backfilled into all 10
rate-16 cells.

## Test Status

- Tests: **passing**, 120 (`pytest tests/ benchmarks/`). 60 router + 60 benchmark.

## What's Done

- **`alpha` removed.** Argmax is invariant under positive scaling, so only the ratio was ever
  free. Every recorded run used alpha=1.0, so no result moves. A test asserts it cannot return.
- **Load normalized against the fleet mean:** `(load - mean) / max(1, mean)`.
  `DEFAULT_LOADAWARE_BETA = 1.0` = "an endpoint 100% above fleet-average load forfeits one full
  cache hit". No hardware, rate or fleet size in that sentence.
- **`load_gate.beta_from()` deleted**, replaced by `relative_imbalance()` which reports rather
  than calibrates. `run_sweep.sh` no longer takes `BETA_HEADLINE`.
- **Router image built and pushed:** `quay.io/rhl193000/lmstack-router-loadaware:f21d18f`.
- **Four cells run at rate 16** (all valid, error rates 0.13-0.46%):
  `loadaware-b1.0` n=20, `loadaware-b0.5` n=3, `loadaware-b0.25` n=3, `kvaware` n=3 (control).
- **Engine-side TTFT backfilled into all 10 rate-16 cells** and added to `prom_dump.METRICS`.
  Prometheus storage is an `emptyDir` - this was hours from being unrecoverable.

## What's Left

1. **Decide the TTFT metric of record.** Blocking. See Key Context for the three options and
   the per-seed windowing unlock. Ben is bringing a proposal.
2. **Re-derive the §5 numbers** on whichever metric wins. The b1.0-vs-kvaware TTFT comparison
   currently in this session's notes is void.
3. **Confirmatory n=20 at the chosen beta.** Pre-register the beta on #3 *before* running -
   choosing beta by looking at TTFT and then reporting a p-value from that run is the same
   optional-stopping error the project already refused on seeds.
4. **Decide the headline beta.** On imbalance, 0.5 and 1.0 are both ~40-46% better than a
   same-hour kvaware; 0.25 has no effect at all (2.257 vs control 2.113). Useful range starts
   at 0.5. Cannot rank 0.5 vs 1.0 on TTFT until item 1 is settled.
5. **CHANGELOG** has the policy entry but not the WAN finding - the detail is in `2d76f01`'s
   commit message and needs folding in.
6. Nothing pushed since `f21d18f`; `2d76f01` is local only.

## Blockers

**The TTFT co-primary is not measurable as currently instrumented.** Everything downstream of
that (headline, fig7, the beta choice) waits on the decision in item 1.

## Key Context

### The WAN finding, and why it is certain

`load_driver.py:108-150` measures `ttft_s` with `perf_counter` from send to first chunk. The
driver runs on Ben's laptop against `llm-cache-llm.apps.gapu-2...`. Measured RTT to that host:
**min 18.7 / avg 44.4 / max 132 ms, stddev 39.7 ms**; a trivial `/v1/models` HTTPS call takes
87-192 ms. `results/aborted-20260804-vpn/` says this link has bitten the project before.

Over the evening of 2026-08-04, non-engine overhead went **258 ms -> 478 ms** while engine-side
TTFT stayed **flat at 0.168 -> 0.180 s**. The system under test never slowed down.

Ruled out first, all verified rather than assumed:

| checked | result |
|---|---|
| dataset | manifest byte-identical across all cells; 20/20 on-disk sha256 match |
| recording code | `load_driver`/`workload_gen`/`freeze_workloads`/`warmup`/`collectors` byte-identical 265be91..f21d18f |
| requests sent | `prefix_id` sequence and `prompt_tokens` identical; send schedule within 20 ms |
| `prompt_tokens` diffs | the known HTTP 500s, 1-2/seed, present in **both** cells |
| engine | queue time 0.01 ms, 0 preemptions, KV usage DOWN, prefix hit rate UP |
| router | RSS and CPU flat all day; and it restarts every cell, so no state accumulates |
| nodes | worker0 carries 147 pods at 93% CPU / 99% mem *requested*, worker1 51 - but actual CPU 5% at rest, and no node metrics are retained to prove contention |

**Why the signature fits a constant network offset and nothing else:** client TTFT rose
uniformly including **p10 (2.0x)** - a floor shift, which no policy or GPU effect produces,
they move the tail. ITL was untouched because a constant cancels in a difference between
consecutive chunk arrivals. E2E was nearly untouched because ~6 s of decode dominates it.

### The numbers that matter

Engine-side p95 is derived from `vllm:time_to_first_token_seconds_bucket` deltas over each
cell window, summed across engines, linearly interpolated:

| time | cell | engine ttft p95 | client ttft p95 |
|---|---|---|---|
| 00:37 | b0.034 | **0.336** | 0.396 |
| 13:58 | b0.034 | **0.342** | 0.378 |
| 15:27 | kvaware | 0.462 | 0.426 |
| 16:02 | b0 | 0.446 | 0.438 |
| 19:14 | b0.068 | 0.236 | 0.550 |
| 19:25 | roundrobin | 15.787 | 11.528 |
| 20:44 | b1.0 | 0.333 | 0.675 |
| 21:16 | b0.5 | 0.357 | 0.646 |
| 21:26 | b0.25 | 0.249 | 0.703 |
| 21:43 | kvaware CTRL | 0.319 | 0.658 |

The two `b0.034` replicates 13 h apart agree to **1.8%** engine-side. And b1.0 vs the 15:27
kvaware is **28% better** engine-side against **76% worse** client-side - the confound inverted
the sign, it did not merely add noise.

### Options for item 1

- **(A) Engine-side `vllm:time_to_first_token_seconds` as the metric of record.** Already
  collected and backfilled, so **zero re-runs** and today's cells are rescued.
  *Cost:* histogram buckets are coarse; and the naive form gives one number per cell, which
  would destroy the paired-Wilcoxon design that every §5 p-value rests on.
  **Unlock:** seeds replay *sequentially*, and each seed's window is derivable from `send_ts`
  in its `driver-seed<N>.csv`. Query the histogram per seed window and the per-seed pairing
  survives intact. This is the thing to try first.
- **(B) Run the driver in-cluster** (Job/pod next to the router). Keeps client-side semantics -
  what a user actually experiences - with no WAN. *Cost:* new deploy plumbing, and every
  comparison re-run.
- **(C) Subtract a measured network baseline.** RTT probes during the cell, subtract per
  request. *Cost:* fragile - RTT varies per request and the correction is unverifiable.

Recommendation to put to Ben, **not** to act on unilaterally: **A now** (rescues the existing
runs, preserves pairing), **B for the final report** if time allows, and report client-side as
a clearly-labelled secondary "user-observed" number valid only within a session. **C is a
trap** - do not offer it as a serious option.

### Consequences worth carrying into §6

- **Load imbalance is derived from Prometheus, server-side, so it is immune to all of this.**
  That is now a *mechanism*, not just an observation, and it explains why imbalance reached
  p<0.0001 while TTFT was a null in every single sweep. Strong §5/§6 material.
- The measurement-harness fix is a genuine **Reproducibility** contribution (30% of the grade),
  not an embarrassment: a benchmark that measured its own network and reported it as system
  latency, caught by an in-session control and a server-side cross-check.
- **Two corrections were issued this session** and both came from the same confound: "beta=1.0
  over-diverts and costs TTFT" and "the relative formulation is inefficient on cache hits".
  Both were drawn from cross-time comparisons. Neither survives. Do not reinstate them from
  the earlier notes without re-deriving on the clean metric.

### Gotchas

- `beta` is an **env var**, not a build input, so all beta cells share the image `f21d18f`.
  Only rebuild when `patches/**` changes.
- Cells **cannot** run concurrently - one cluster, one router deployment. ~8 min fixed setup
  per cell against ~50 s per seed, so cells are the expensive unit and seeds are nearly free.
- n=3 is very noisy on TTFT: kvaware's own three seeds read 0.268 / 0.506 / 0.724. Imbalance
  discriminates cleanly at n=3; TTFT does not.
- Seeds 1-3 are **not** systematically harder, but a 3-seed median can sit 0.74-1.19x the
  20-seed median, so never compare an n=3 median against an n=20 median.
- The clamp in `relative_loads` is `max(1, mean)`, which never engages at this scale (mean load
  is 25-34). If the policy needs to be quieter during lulls, that floor is the lever, not beta.
- `prom_dump` writes into `results/<run>/prom/`; re-running it for a window is idempotent and
  safe, which is how the backfill worked.

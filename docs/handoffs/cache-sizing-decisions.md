---
branch: feat/evaluation-runs
date: 2026-08-03 (cache-sizing decision session)
status: decisions locked - replaces the "Proposal to react to" section of docs/handoff-cache-sizing.md
---

# Handoff: cache sizing → locked evaluation design

Answers the open question in [`docs/handoff-cache-sizing.md`](../handoff-cache-sizing.md).
The diagnosis there was correct; the proposed fix (shrink the CPU buffer) was not, and is
dropped. Nothing has been run or changed yet.

## The correction that drives everything

**HBM, not the CPU tier, is what makes the cache term free.** Measured from the engine's own
startup log:

```
kv_cache_utils.py:1087  GPU KV cache size: 393,744 tokens     # = 14.5 GB per engine
```

The 20-prefix working set is 40,960 tokens, **10.4% of that**. vLLM's native prefix cache holds
every prefix on every engine permanently, with 10x headroom. This is why fig3 saturates at ~0.95
on all arms including roundrobin.

Consequence: **shrinking `cpuOffloadingBufferSize` cannot force a recompute.** LMCache would
evict from CPU, but the KV stays resident in HBM and the engine still serves it at full speed.
The only thing that changes is what the router *believes* - the evict message drops the registry
entry, so the router routes away from an instance that is in fact warm. That degrades the signal
without creating the scarcity. Strictly worse than today.

The empirical relation on our A10s is `KV pool = 23·u − 6.2 GB`, which reproduces the measured
14.5 GB at `u=0.90` exactly. That makes `gpuMemoryUtilization` the working lever.

### Two open sub-questions from the prior handoff now close

- **"What buffer size?"** - none. Wrong knob. CPU tier stays at 4 GB.
- **"Does this LMCache version expose a GPU-resident tracked backend?"** - **no.** Only
  `local_cpu_backend.py:144` and `local_disk_backend.py:269` emit `KVAdmitMsg`; there is no GPU
  storage backend in the engine's `storage_backend/__init__.py`. HBM-only is dead as stated,
  not merely blocked. Do not revisit.

## Decisions locked (Ben, this session)

1. **Keep kvaware + LMCache as the baseline.** The router has no cache knowledge of its own; the
   registry is the only source of per-instance cache state. `prefixaware` (router-local trie, no
   LMCache) was considered and rejected: it guesses rather than observes, and on that path the
   project never touches the caching library, which is off-brief.
2. **CPU tier stays at 4 GB.** Under the new pool size it holds the entire working set while HBM
   churns, which is the durable-tier role it is supposed to play. No config change.
3. **Create scarcity in HBM:** `gpuMemoryUtilization: 0.90 → 0.37` (~2.2 GB ≈ 60k tokens/engine).
4. **Grow the prefix pool 20 → 32** (65k tokens ≈ 1.9x one engine's retention after in-flight KV).
5. **Requests per seed 500 → 900.** Not arbitrary: with 32 prefixes, first-touch requests are
   unavoidable full prefills. At 500 they are 6% of traffic and sit on top of the p95 threshold,
   inflating it on every arm; at 900 they fall to 3.5% and drop below it.
6. **Reallocate seeds instead of spreading them evenly.** β sweep at 3 seeds (it is a tuning
   curve, not a hypothesis test); headline cells at 10 seeds. n=6 with one reversal is capped
   near p=0.2 regardless of effect size - that was always going to force a longer run.
7. **Promote load imbalance to a co-primary §5 metric** alongside TTFT p95. It is already
   collected, unambiguous, and has the clean β=0 ablation.
8. **Re-run the rate pilot.** The knee moves down when the KV pool shrinks; 7.5 req/s does not
   carry over.

## Target ratios

| Quantity | Pilot | Locked |
|---|---|---|
| Per-engine HBM KV pool | 394k tok (14.5 GB) | ~60k tok (2.2 GB), `gpuMemoryUtilization: 0.37` |
| In-flight KV at target rate | ~25k tok | ~25k tok |
| ⇒ prefix retention per engine | everything | ~35k tok ≈ 17 prefixes |
| Prefix pool | 20 (41k tok) | 32 (65k tok) |
| CPU tier | 4 GB (114k tok) | 4 GB, unchanged (114k > 65k ⇒ registry is a complete superset) |
| Requests per seed | 500 | 900 |

## Why this regime produces a publishable result

- **roundrobin gets genuinely slow.** Each engine holds ~40% of the pool, so random assignment
  misses over half the time and pays a full 2048-token prefill. That TTFT penalty does not exist
  today because every engine holds everything.
- **β becomes a real parameter.** Today, routing to the idle engine is free (it has the prefix
  too), so the sweep is degenerate and larger β is monotonically better. Under scarcity it costs
  a recompute, so there is an interior optimum and the §5 parameter sweep becomes a curve.
- **kvaware keeps its strength** (routes to the holder, concentrates 3.7:1), so the comparison
  stays one clean variable.

## Run plan

| Phase | Cells | Seeds | Runs |
|---|---|---|---|
| A: β tuning | b0, b0.1, b0.5, b1.0 | 3 | 12 |
| B: confirmatory | winning β, kvaware, roundrobin | 10 | 30 |

~42 runs ≈ **4.5h** (pilot was 36 runs / 3h; per-run fixed overhead ~3.9 min dominates, load
time goes 1.1 → 2.1 min). One overnight run, inside the 2026-08-10 deadline.

## Gates before spending the 4.5h

1. **Scarcity gate (~15 min).** Apply the config, warm up roundrobin only, check engine-local hit
   rate. It must drop well below the current 0.95 saturation. If it is still ~0.95 the scarcity
   did not take - stop, do not run the sweep.
2. **Rate pilot (~30 min).** `benchmarks/rate_pilot.sh` on the new memory config to find the knee.
3. **Preemption becomes a validity gate** alongside the registry probe. A 60k-token pool with
   `maxModelLen: 16384` has less headroom; if `vllm:num_preemptions` climbs, TTFT is measuring
   scheduler thrash, not routing. Expected ~29x concurrency headroom at 2080-token requests -
   verify, do not assume.

## Procedure (blocking)

- Every change applies to **all arms identically** or the comparison is void.
- **Record the methodology amendment on [#3](https://github.com/BenEpstein/caching-in-llms/issues/3)
  and demote the current 6 cells to a pilot BEFORE looking at any new data.** Adding seeds after
  seeing p=0.219 is optional stopping unless the confirmatory run is pre-registered.
- Verify the new KV pool from the engine's own startup log line rather than trusting the
  `23·u − 6.2` arithmetic.
- Workload regeneration: `benchmarks/freeze_workloads.py` at 32 prefixes / 900 requests, re-freeze,
  update `benchmarks/workloads/manifest.json` sha256s.

## Sequence

1. `gpuMemoryUtilization: 0.37` in `deploy/values-baseline-kvaware.yaml`; confirm token count from log.
2. Regenerate + re-freeze workload (32 prefixes, 900 requests); update manifest.
3. Scarcity gate → rate pilot.
4. Amendment on #3; demote pilot.
5. Phase A, pick β; Phase B confirmatory.

## Cost / state

Nothing changed or launched. #7 stays open. The existing `results/20260803-*` become the pilot,
not the headline. ~6 days to 2026-08-10.

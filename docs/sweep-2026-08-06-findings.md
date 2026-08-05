# Confirmatory sweep 2026-08-06: run log, diagnosis, and the next experiment

> status: live · 2026-08-06 · run log and measured diagnosis for the #31 confirmatory sweep.
> Decisions and their rationale live in `CHANGELOG.md` (2026-08-06) and issue #31; this doc is
> the artifact those point at. Part 3 is a **proposal**, not a decision - nothing in it is
> pre-registered until it is written up and signed on #31.

## Part 1 - What ran

Pre-registration rev 2 (#31 comment `5196484091`) as amended by Amendment 1 (`5196605866`),
both signed off before the first cell. Cell order is Amendment 1's, not β-ascending.

| | |
|---|---|
| Command | `LOADAWARE_TAG=acf43d1 BENCH_TAG=42e6a32 ./benchmarks/run_sweep.sh 16` |
| Cells | `kvaware`, `b0.5`, `b1.0`, `b2.0`, `b0` (Amendment 1 order) |
| n | 20 seeds/cell × 500 requests |
| Workload | frozen, SHA-256 pinned: 128 prefixes, Zipf s=0.9, ISL 1578, OSL 64 |
| Rate | 16 req/s offered, open-loop Poisson |
| Driver | in-cluster, `gapu-2-worker1`, all five cells |
| Duration | 1 h 44, 23:05 → 00:47 |
| Branch | `worktree-confirmatory-sweep-31` off `main` @ `ffe1c77` |

Commits: `b6ba33a` harness fix · `578941a` data (230 files) · `5544cf2` figures + CSV.

### Validity

All five cells pass. Pooled error 0.20–0.46%, no seed near the 10% catastrophic ceiling,
`utilization_coverage` 1.000 on all ten series including DCGM. Two seeds flagged over the 1%
reporting threshold (`kvaware` seed 14 at 1.2%, `b0` seed 10 at 2.0%) - reported, not fatal,
per amended rule 1. Arm error-bias ratio 1.26× against a 2× gate.

**Zero preemptions in every cell.** KV cache occupancy mean ~0.25, max 0.77. No cell was
memory-pressured, so no latency number here is an eviction artefact.

### Results against the pre-registration as written

| # | Claim | Effect | p | α | Verdict |
|---|---|---|---|---|---|
| 1 | Load imbalance | −48.1%, CI [37.7%, 56.3%], 18/20 seeds | 0.000010 | 0.025 | **PASS** |
| 2 | TTFT p95 | −2.7%, CI [−4.3%, +15.4%], 12/20 seeds | 0.1153 | 0.025 | **NULL** |

Descriptive, mean across 20 seeds, no p-values attached:

| metric | `kvaware` | `b0.5` | `b1.0` | `b2.0` | `b0` |
|---|---|---|---|---|---|
| imbalance | 2.448 | 1.269 | 1.196 | 1.127 | 2.956 |
| TTFT p50 (s) | 0.173 | 0.152 | 0.159 | 0.170 | - |
| TTFT p95 (s) | 0.334 | 0.290 | 0.282 | 0.410 | 0.379 |
| TTFT p95 sd | 0.113 | 0.042 | 0.047 | 0.169 | 0.142 |
| ITL p95 (ms) | 157.4 | 136.2 | 130.9 | 142.6 | - |
| prefix hit % | 91.2 | 90.7 | 87.9 | 86.1 | 91.3 |
| throughput (req/s) | 14.32 | 14.35 | 14.39 | 14.31 | - |

### Two problems the run surfaced

**1. The `b0` drift sentinel did not hold.** `b0` vs `kvaware` on TTFT p95: −13.2%, CI
[−18.5%, −5.8%] excluding zero, 16/20 seeds worse. `b0` is also worse on imbalance (2.956 vs
2.448), though that one is non-significant with a wide CI. Both metrics point the harmful way,
which is the signature drift would leave on the last cell of the window. Amendment 1 fixed the
reading in advance: **ambiguous between drift and placement, not read as a placement effect.**

This does not threaten claim 1 - no plausible drift manufactures 48% at p=0.00001, and `b0`
(load term off) shows no balance gain at all, which is the ablation doing its job. It does mean
the 1 h 44 window is not certified for latency. Closing it costs one 20-minute `kvaware` cell
at the end. Rev 2 dropped that bracket on the reasoning that #27 had removed the drift
mechanism; this run is evidence that reasoning was wrong.

**2. The balance co-primary had no test path.** `compare --metric imbalance` raised `KeyError`
until `b6ba33a`. Not a #30/#43 regression - the glue was never written. Fixed, with tests.

## Part 2 - Why balance did not convert to latency

This is the load-bearing diagnosis and it is measured, not inferred.

**The fleet never queued.** `vllm:num_requests_waiting` on the `kvaware` arm: **0 of 284
scrapes** nonzero. Not "low" - zero. `b0.5` likewise 0/282. Only `b2.0` ever queued, at 1/284
scrapes.

**There was no capacity pressure to relieve.** KV occupancy 25% mean / 77% max, zero
preemptions, GPU SM utilization 87–91%. The engines were busy but keeping pace comfortably.

**Throughput was never limited.** The reported 14.37 req/s looks like an 11% shortfall against
16 offered, and it is not one: the driver put **15.52 req/s on the wire** (32.2 s send span for
500 requests), and `wall_s` includes a **2.58 s drain tail** after the last send. 500 ÷ (32.2 +
2.58) = 14.37. It is a measurement artefact of how throughput is computed, not saturation.
**Do not read the throughput column as evidence of a capacity ceiling.**

**So:** the workload produced real imbalance (2.45× in-flight ratio) with no contention behind
it. Landing on the busier engine cost almost nothing, because the busier engine still had
headroom. Load-aware routing fixed a number that was not hurting anyone.

That is the whole story of the p95 null, and it is a property of the **operating point**, not
of the policy. Rev 2 asserted this sweep "runs at or above the knee, where [β] does [have
something to act on]". The queueing data falsifies that premise: rate 16 / OSL 64 is still
below the knee on this fleet.

What the policy *did* do to latency is compress variance: p95 sd 0.113 → 0.042 (−63%), and the
per-seed improvement correlates −0.929 with how bad `kvaware` was on that seed. It clips the
bad tail rather than shifting the distribution. Exploratory, found after the null, labelled as
such - but it is the seed of the next experiment.

## Part 3 - How to show we are better (proposal, not pre-registered)

"We reduced imbalance 48%" is a **mechanism** claim. It is not a performance claim and should
not be sold as one. To make a performance claim, two things have to change.

### 3a. Move to an operating point where imbalance costs something

Load-aware routing can only pay when being on the busy engine hurts. Three levers, roughly in
order of expected payoff:

**1. Raise the offered rate to the knee.** SM utilization is already 87–91%, so the knee is
close - probably 18–24 req/s. Ramp `kvaware` alone (cheap, one arm) and find the first rate
where `num_requests_waiting` is *consistently* nonzero and p95 starts climbing superlinearly.
`benchmarks/rate_pilot.sh` exists for this.

> **Do not overshoot.** Past the knee both engines pin at capacity and imbalance *collapses* -
> already measured: 2.99× at OSL 64, 3.98× at 128, 1.89× at 256. The window where the policy
> can help closes at both ends. Sit at the knee, not past it.

**2. Sharpen the workload so imbalance is larger at the same load.** Both of these raise
contention without raising total work, which is exactly what is wanted:
- **Shrink the prefix pool** (128 → 32 or 16). Fewer distinct prefixes means hotter prefixes,
  which means `kvaware` concentrates harder onto one Instance.
- **Raise the Zipf skew** (s = 0.9 → 1.2). Same effect by a different route.

**3. Lengthen decode (OSL).** Longer decode means longer in-flight residency per request, and
in-flight residency is the thing that converts placement into queueing. Probably the strongest
single lever for making imbalance *matter*. Caveat: OSL 128 at rate 16 already saturates (65%
of offered achieved), so pair a longer OSL with a *lower* rate rather than stacking both.

Any change to `workload_gen.py` or `freeze_workloads.py` **requires a rebuilt bench image** -
both ship inside it. Do not reuse `42e6a32` across such a change.

### 3b. Measure something that is a performance claim

| candidate | what it says | cost |
|---|---|---|
| **Goodput / SLO attainment** | "X% more requests meet the latency SLO at the same offered rate" | one 2-arm sweep |
| **Capacity at fixed SLO** | "serves N% more traffic on the same two GPUs" | a ramp per arm, ~2× cluster time |
| TTFT p95 (current) | already null at this operating point | - |

**Recommendation: goodput at the knee rate.** It directly monetizes the variance reduction
already measured (tail-clipping *is* SLO attainment), reuses the existing harness shape, and
costs one sweep. Per-seed goodput is a proportion, so the same paired exact Wilcoxon on 20
paired per-seed values applies unchanged - no new statistics.

**Capacity at fixed SLO** is the stronger story for §5 and for a systems audience ("N% more
traffic on the same hardware"), but needs a ramp on both arms. Worth it if cluster time allows.

#### Feasibility check on existing data (EXPLORATORY - not a claim)

Computed post-hoc on this sweep's cells purely to answer "does this metric have signal at all":

| SLO | `kvaware` | `b0.5` | delta | seeds better |
|---|---|---|---|---|
| TTFT < 150 ms | 42.2% | 49.6% | +7.3 pts | **16/20** |
| TTFT < 200 ms | 67.8% | 74.9% | +7.1 pts | 14/20 |
| TTFT < 250 ms | 82.0% | 89.2% | +7.2 pts | 12/20 |
| TTFT < 300 ms | 89.8% | 95.4% | +5.7 pts | 10/20 |

16/20 at the tight SLO against 12/20 for the p95 median-paired test. The metric has more signal
than the one that returned null, **even at an operating point with no queueing**. That is
encouraging for 3b and is the main reason to prefer goodput.

> **This table is exactly the trap the next pre-registration must close.** Four thresholds were
> computed and the best-looking one is quoted first. Choosing 150 ms *because* it looks best is
> multiple testing with the correction omitted. The next pre-registration must fix **one** SLO
> threshold in advance, justified by a service requirement rather than by this table, and must
> disclose that this exploratory scan happened.

## Part 4 - Integrity guardrails for whoever picks this up

The project's credibility rests on having reported a null. Protect that.

1. **Do not re-analyse this sweep's data with a new metric and call it a result.** Goodput on
   these cells is exploratory, full stop. A performance claim needs a new run at a new operating
   point with its own pre-registration.
2. **The reason for changing metric must be the diagnosis, not the null.** It is defensible to
   say "we measured zero queueing on the baseline, so the operating point could not test the
   hypothesis, so we moved the operating point and chose a metric suited to it." It is not
   defensible to say "p95 did not work so we tried something else." Both produce the same next
   experiment; only the first is honest, and the difference is visible in whether the
   pre-registration cites the queueing data.
3. **Pre-register and get both sign-offs before the first cell.** Same discipline as #31.
4. **Include a closing `kvaware` bracket.** This run proved the sentinel was needed and that
   dropping it costs interpretability. ~20 min.
5. **Report the sequence in §6.** First run: p95 null at rate 16, zero queueing. Second run: new
   operating point, new metric. A reader who discovers that ordering themselves will assume the
   worst; a reader who is told it up front sees a diagnosis.

## Part 5 - Loose ends

- **`docs/handoffs/` is gitignored** (`.gitignore:16`, issue #29). This document is in `docs/`
  so it is on the branch. If a working-notes handoff is wanted too, it will not be committed.
- **`docs/handoffs/prereg-review-31.md`** (untracked) still reads
  `status: signed-off-ready-to-run` with "Blockers: None ... it is ticked" directly under a box
  stating Amendment 1 was unsigned. It was unsigned at session start; both boxes were ticked
  mid-session. Stale, and it is the first thing a resuming session reads.
- **`export_summary.py` regenerates from disk and does not append**, so rerunning it drops any
  cell whose raw directory is gone. Five such cells existed (2-seed `kvaware` probes, 10 rows).
  They were dropped, not preserved: `scripts/reproduce.sh` check 1 (#28) requires every summary
  row to have committed raw data, and those rows fail it - on `main` as well as here, so that
  check fails on `main` today. Rows survive at `ffe1c77:results/summary-per-seed.csv`.
  `reproduce.sh` now passes 5/5 on this branch.
- **No GPU memory occupancy is collected.** DCGM has `GPU_UTIL`, `MEM_COPY_UTIL`,
  `POWER_USAGE` only - no `FB_USED`. `MEM_COPY_UTIL` is bandwidth, not occupancy.
  `vllm_kv_cache_usage_perc` is the working proxy and is what fig10's "GPU memory" panel plots.
- **Branch is not pushed.** No PR. Adversarial review (`/code-review`, `/simplify`) not yet run
  on the diff.

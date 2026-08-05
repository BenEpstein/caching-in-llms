# Confirmatory sweep 2026-08-06: run log, diagnosis, and the open question

> status: live · 2026-08-06 · run log and measured diagnosis for the #31 confirmatory sweep.
> Decisions and their rationale live in `CHANGELOG.md` (2026-08-06) and issue #31; this doc is
> the artifact those point at.
>
> **Parts 1 and 2 are settled fact - what ran and what it measured. Part 3 is deliberately
> unresolved.** It lays out the option space with the argument on both sides of each branch and
> ends in a list of decisions nobody has made yet. It is not a plan to execute. Whoever picks
> this up should expect to argue it, and should treat any ranking they find here as one input,
> not an answer.

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
| Branch | `feat/confirmatory-sweep-31`, [PR #46](https://github.com/BenEpstein/caching-in-llms/pull/46) |

Rebased onto `main` after PR #45 (`scripts/reproduce.sh`) landed mid-run; SHAs below are
post-rebase. `3659f26` harness fix · `ac6d232` data (230 files) · `a37cae5` figures ·
`4f57ffd` this doc · `bcc6cd3` reproduce.sh green · `e3794a0` review fixes.

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

**2. The balance co-primary had no test path, and fixing it broke rule 6.** This needs stating
plainly because it is the kind of thing a report must volunteer rather than have found.

`compare --metric imbalance` raised `KeyError`. `per_seed_imbalance` lived in
`export_summary.py`, a CSV exporter with no statistics, while the only Wilcoxon lives in
`analyze.py`. Nothing joined them. Not a #30/#43 regression -
`git log -S imbalance - benchmarks/analyze.py` returns one commit, `ab9530f`, which added the
word in a docstring. The glue was never written, and `run_sweep.sh` told the operator to test
claim 1 with a command that could only raise.

**Rule 6 forbids the fix.** *"Analysis is the `analyze.py` / `export_summary.py` path exactly as
committed at `42e6a32`. No analysis code is written for this run. Any analysis code written
after data exists is exploratory and labelled as such."*

The timeline is not ambiguous. The last cell's measurement window closed at **00:47:04**
(`results/20260806-002645-loadaware-b0/run.json`). The test path was committed at **00:56:03**.
**Claim 1's p = 0.000010 was produced by code written nine minutes after the data existed.**

What happened: the gap was found at analysis time, the run was stopped, and the choice was put
to Ben rather than taken unilaterally. He authorised writing it - *"forget about the
pre-registration rule ... write the code if needed"*. That is the principal's call to make and
he made it knowingly. It is recorded here because a decision like that is worth nothing
undocumented.

**What mitigates it, and what does not.** Everything about the test was fixed pre-data in
rev 2: the metric definition (`export_summary.py:per_seed_imbalance`), the statistic (one-sided
exact Wilcoxon on 20 paired per-seed differences), α = 0.025, the direction, and the bootstrap
CI. The new code exercises no researcher degree of freedom - it imports the same committed
`wilcoxon_exact_one_sided` and `bootstrap_ci_median_rel_reduction` that produced claim 2's
null, and the effect (−48.1%, 18/20 seeds) is far too large for the choice of test to be doing
any work. What is *not* mitigated: rule 6 called for labelling it exploratory, and it is not
being labelled exploratory. That is a deliberate, authorised departure from the
pre-registration, and §6 must say so in those words.

The same applies, more mildly, to the `ALPHA` 0.05 → 0.025 fix: also analysis code, also
changed after data existed. It moves the printed threshold onto the pre-registered one and
changes no verdict in this run, but it is the same category.

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

## Part 3 - How do we show we are better? (open, for the next session to argue)

This part is deliberately NOT a plan. It is the option space plus the evidence for and against
each branch, because the decision is a judgement about what this project is trying to prove and
that is not settled. Nothing here is pre-registered. Anyone picking this up should expect to
argue the options, not execute them.

### The question under all of it

**What counts as "better" for this project?** Four defensible answers, and they lead to
different experiments:

| answer | the claim it produces | do we already have it? |
|---|---|---|
| Balance | "48% less load imbalance, p=1e-5" | **Yes, banked.** |
| Latency | "X% lower TTFT p95" | No - null at n=20, and the operating point could not test it |
| Goodput | "X% more requests meet the SLO" | Not measured. Exploratory signal looks better than p95's |
| Capacity | "N% more traffic on the same 2 GPUs at fixed SLO" | Not measured. Strongest claim, most cluster time |
| Predictability | "63% less variance in p95" | Measured, but exploratory and post-hoc |

Ben's stated position (2026-08-06): balance alone *"doesn't mean anything to me, we need to
show that we are better."* That argues against stopping at the banked claim. It does not by
itself pick between goodput, capacity and latency.

### Option 0 - run nothing else, write up what exists

The case FOR, which deserves a hearing before any cluster time is spent: the rubric is
Correctness 40 + Reproducibility 30 + Performance Gain 15 + Clarity 15. Correctness and
reproducibility are 70% and are in good shape - `reproduce.sh` is green, the harness caught its
own broken co-primary and its own lying captions. Performance Gain is 15% and a significant
48% balance improvement with a clean ablation already earns a share of it. A null on latency,
honestly reported, costs less than most people assume and demonstrates exactly the discipline
§6 is graded on.

The case AGAINST: "we balanced the fleet" is a mechanism claim. A reader who wants to know
whether the system got *better* is not answered, and §5 asks for relative improvement on a
metric. Also the `b0` sentinel failure is a live loose end regardless of what else is decided.

**Cheapest thing that closes a real gap either way:** the ~20 min closing `kvaware` bracket. It
resolves drift-vs-placement on `b0` and is worth doing under every option including this one.

### Option A - move the operating point, keep TTFT p95

Rerun the same design at the knee. Keeps the metric the project originally pre-registered, so
there is no metric-substitution question to answer at all.

- **For:** cleanest possible story - same metric, same test, one variable changed, and the
  change is justified by measured evidence (zero queueing) rather than by the null.
- **Against:** p95 median-paired was a poor instrument for what the policy does even where it
  worked. It clips the tail on bad seeds and costs slightly on good ones, which is a *shape*
  change; a paired test on the median is nearly blind to it (12/20 signs). Moving the operating
  point may not fix that.

### Option B - move the operating point AND switch to goodput

Measure the fraction of requests meeting a fixed TTFT SLO.

- **For:** directly monetises the tail-clipping the policy actually does. Per-seed goodput is a
  proportion, so the same paired exact Wilcoxon applies with no new statistics. Exploratory
  check below suggests more signal than p95 even at the un-queued operating point.
- **Against:** it is a metric change following a null, and no amount of good reasoning makes
  that look innocent to a hostile reader. It requires choosing an SLO threshold, which is a new
  researcher degree of freedom that has to be nailed down in advance and justified on service
  grounds, not on the data.

### Option C - capacity at fixed SLO

Ramp each arm until p95 breaches an SLO; report the sustainable rate.

- **For:** the strongest claim available - "serves N% more traffic on the same two A10s" is
  what a systems audience actually accepts, and it is unambiguously a performance result.
- **Against:** roughly 2x the cluster time (a ramp per arm), and the ramp itself needs a
  stopping rule pre-registered or it becomes optional-stopping by another name.

### Which levers move the operating point (needed by A, B and C)

Load-aware routing can only pay when being on the busy engine hurts. Open question which of
these to use, and in what combination:

1. **Raise offered rate.** SM utilisation is already 87-91%, so the knee is probably 18-24
   req/s. `benchmarks/rate_pilot.sh` exists. Cheapest to try.
2. **Shrink the prefix pool** (128 -> 32 or 16) **or raise Zipf skew** (s = 0.9 -> 1.2). Both
   raise contention without raising total work, which is the interesting direction.
3. **Lengthen decode (OSL).** Longer residency per request is what converts placement into
   queueing, so this is arguably the most direct lever - but OSL 128 at rate 16 already
   saturates (65% of offered achieved), so it has to be paired with a lower rate.

> **The trap on all three: the window closes at both ends.** Past the knee both engines pin at
> capacity and imbalance *collapses* - measured, 2.99x at OSL 64, 3.98x at 128, 1.89x at 256.
> Overshooting produces a null that looks like the policy failing when it is the workload
> saturating. Any ramp needs to find the knee, not clear it.

Any change to `workload_gen.py` or `freeze_workloads.py` **requires a rebuilt bench image** -
both ship inside it, so `42e6a32` cannot be reused across such a change.

### Feasibility check on existing data (EXPLORATORY - not a claim)

Computed post-hoc purely to answer "does a goodput metric have signal at all", and relevant to
weighing Option B against Option A:

| SLO | `kvaware` | `b0.5` | delta | seeds better |
|---|---|---|---|---|
| TTFT < 150 ms | 42.2% | 49.6% | +7.3 pts | **16/20** |
| TTFT < 200 ms | 67.8% | 74.9% | +7.1 pts | 14/20 |
| TTFT < 250 ms | 82.0% | 89.2% | +7.2 pts | 12/20 |
| TTFT < 300 ms | 89.8% | 95.4% | +5.7 pts | 10/20 |

16/20 at the tight SLO against 12/20 for the p95 median-paired test, at an operating point with
no queueing at all.

> **This table is also the argument against Option B, not just for it.** Four thresholds were
> computed and the best-looking one is quoted first. If the next pre-registration picks 150 ms
> because of this table, that is multiple testing with the correction omitted. Whoever chooses
> B must fix one threshold in advance on service grounds and disclose that this scan happened.

### The decisions someone has to actually make

1. Is Option 0 acceptable - do we spend more cluster time at all?
2. If not: A, B, or C? (Equivalently: is the metric-substitution cost of B/C worth the better
   instrument?)
3. Which lever moves the operating point, and does the workload profile change (which forces an
   image rebuild) or only the rate (which does not)?
4. If B: what SLO, justified how?
5. If C: what is the ramp's stopping rule?
6. Does the closing `kvaware` bracket go in? (Recommended under all options; ~20 min.)

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

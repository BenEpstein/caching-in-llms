# Confirmatory sweep 2026-08-06: run log, diagnosis, and the open question

> status: live · 2026-08-06 (revised, same day) · run log and measured diagnosis for the #31
> confirmatory sweep. Decisions and their rationale live in `CHANGELOG.md` and issues #31 / #50;
> this doc is the artifact those point at.
>
> **Parts 1 and 2 are settled fact - what ran and what it measured.** Part 3 was written as an
> open option space and is now **closed**: it records what was decided and why the question it
> posed dissolved rather than being answered. Two things in the original text are superseded and
> are marked where they appear - the `b0` drift reading (Part 1) and the "free goodput holdout"
> (Part 3). Open work moved to #50.

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
bad tail rather than shifting the distribution - which is exactly why the paired Wilcoxon on
p95 is null (12/20 seeds, a near coin-flip on sign) while the cell-level mean moves 13%. The
same mechanism is what goodput picks up, and goodput is reported: see the Goodput row in
Part 3 and `fig12-goodput.png`.

## Part 3 - How we show we are better (CLOSED 2026-08-06)

This part was written as an open option space ending in six decisions nobody had made. It is
now closed. The question did not get answered so much as dissolve, and the record of how is
worth more than the option table it replaces. Decisions and their rationale are in `CHANGELOG.md`
and #31; what remains open moved to #50.

### What landed

| | |
|---|---|
| Goodput metric `ttft_slo_miss`, tunable `--slo`, fig12, `reproduce.sh` coverage, unit tests | #49 |
| `roundrobin` comparator at n=20 | #51 |
| `roundrobin` on fig12 as the capacity floor | #52 |

Descriptive, mean over 20 seeds per arm, at the documented 150 ms objective:

| arm | goodput @150 ms | achieved req/s | Load Imbalance |
|---|---|---|---|
| `kvaware` | 42.0% | 14.32 | 2.448 |
| `loadaware-b0.5` | **49.4%** | 14.35 | **1.269** |
| `loadaware-b0` (ablation) | 40.0% | 14.12 | 2.956 |
| `roundrobin` | 7.6% | **10.31** | 1.678 |

### The resolution: report the curve, not a point

The selection problem people worry about with goodput attaches to **one sentence only** - a
p-value at a chosen objective. "−19.0% missed at 150 ms, p=0.0021" is a claim about one point
out of a scan of eight, and that sentence would need a pre-registration or a holdout to stand.

`fig12-goodput.png` selects nothing. It plots goodput across the whole 50-400 ms sweep, so the
result is a statement about the entire function and there is no hidden search inside it.
Measured across the 176-point grid: the gain is positive at **168 of 176** points, negative only
between 56 and 70 ms and by at most **0.18 points** (where both arms serve ~1% and the curves
are indistinguishable), and peaks at **+8.2 points at 124 ms**. Turning the load term off puts
the curve **below** the baseline across the range.

So the reportable result needs no further cluster time and no chosen threshold:

> Across every TTFT objective where the metric has room to move, `loadaware-b0.5` serves more
> requests within objective than `kvaware`; with the Load Penalty off, it serves fewer.

That is descriptive, not a tested claim, and it sits beside the pre-registered null rather than
replacing it. Reporting the data more completely is not the same as claiming significance from
a search.

### Three arms, three failure modes - the framing the roundrobin cell buys

- `roundrobin` balances load and ignores Cache-Hit Benefit. It cannot carry the workload:
  **10.31 of 16 offered req/s**, and it needs ~15 s to bring 99% of requests in.
- `kvaware` maximises Cache-Hit Benefit and ignores Load Penalty. It carries the workload but at
  **2.45× Load Imbalance**.
- `loadaware-b0.5` weighs both, and is best on every column above.

Goodput is the one figure a saturated arm belongs on, because its denominator is requests
*sent*: every arm was offered the identical frozen workload at the same rate, so falling behind
counts as missed. A p95 contrast instead conditions on the requests that finished, which
flatters the arm that fell behind. Reported as capacity, not as latency (#51).

### SUPERSEDED: the "free goodput holdout"

An earlier revision of this doc, and #50, proposed the `20260805-0*` 5-cell n=20 sweep as a
zero-cost out-of-sample check on goodput. **It is not usable.** That sweep predates #27
(`8c51b66`, "run the measured replay in-cluster"): its `run.json` carries no `driver` key and
its directory no `window.env`, and its commit `a6eb563` is timestamped 00:47 against a 00:52
cell start. It is **WAN-measured**, and the offset is ~88 ms on TTFT p50 (0.261 s vs 0.173 s
in-cluster on `kvaware`).

An additive offset of that size is survivable for a ratio or a median difference and fatal for a
**threshold** metric: a 150 ms objective sits mid-distribution in-cluster and below the median
over the WAN, so effect size and power both change for reasons that are network rather than
policy. There is therefore **no independent in-cluster dataset in the repository** to confirm
goodput against; every in-cluster n=20 sweep at rate 16 is the one the metric was chosen on.
That is a reason to report the curve descriptively, not a reason to spend an hour manufacturing
a holdout.

## Part 4 - Integrity guardrails for whoever picks this up

The project's credibility rests on having reported a null. Protect that.

1. **Goodput is reported (decided 2026-08-06); the null is reported with it.** The rule this
   replaces said goodput on these cells could never be a result. The distinction that makes it
   reportable is finer than that wording: reporting the **whole goodput curve descriptively**
   selects nothing, while attaching a **p-value at one chosen objective** is a claim about one
   point out of a scan of eight and would need a pre-registration or an independent dataset.
   §5 reports the curve - the objective is swept 50-400 ms rather than picked, the arms separate
   across the whole range, the `b0` ablation reverses the sign, and no new data was collected,
   so the 150 ms statistic is never the headline. What the rule was protecting still stands -
   **the TTFT p95 null stays in the report next to it**, and the count of metrics examined is
   disclosed. The line that would cross into fishing is quietly dropping the null, or adding a
   third metric because goodput also came back weak.
2. **The reason for changing metric must be the diagnosis, not the null.** It is defensible to
   say "we measured zero queueing on the baseline, so the operating point could not test the
   hypothesis". It is not defensible to say "p95 did not work so we tried something else". The
   difference is visible in whether the write-up cites the queueing data.
3. **Pre-register before the first cell.** Ben's approval only - the two-sign-off rule is
   superseded (2026-08-06).
4. **`b0` is not a drift control.** It was chosen as one on the assumption that β=0 makes
   `loadaware` behave like `kvaware`, and departure 3 in `routing_logic.py` means it does not.
   Whatever the next run uses, it cannot be this. **No closing `kvaware` bracket** - decided, and
   the earlier revision of this rule recommending one is withdrawn.
5. **Report the sequence in §6.** Pre-registered p95 null at rate 16 with zero queueing measured,
   then goodput as a descriptive follow-up on the same data, disclosed as such. A reader who
   discovers that ordering themselves will assume the worst; a reader who is told it up front
   sees a diagnosis.
6. **§6 must state the rule 6 departure in those words.** Claim 1's p-value came from code
   committed nine minutes after the data existed (Part 1). Authorised, documented, and not
   labelled exploratory - which is a departure from the pre-registration and has to be
   volunteered rather than found.

## Part 5 - Loose ends

- **`docs/handoffs/` is gitignored** (`.gitignore:16`, issue #29). This document is in `docs/`
  so it is on the branch. If a working-notes handoff is wanted too, it will not be committed.
  `docs/handoffs/prereg-review-31.md` was deleted 2026-08-06: it was superseded by this file and
  its status header still claimed the Amendment 1 gate was passed when it was not. Carried
  forward from it, because it exists nowhere else:
- **`BENCH_TAG=42e6a32` names the image that was smoke-validated, not whatever `main` hashes
  to, and the two have diverged.** `main`'s `load_driver.py` lost an unused closed-loop mode
  after the image was built, and `analyze.py` - which also ships in the image
  (`Dockerfile.bench:29`) - has since gained the imbalance move and the `ALPHA` constant. None
  of it touches the measurement path: the image uses `analyze.percentile` via `load_driver`,
  and that function is untouched. **Do not rebuild the image to "sync" it.** A rebuild is
  required only when `workload_gen.py` or `freeze_workloads.py` change, because those define
  the frozen workload the image self-verifies. A gratuitous rebuild discards the validation
  that makes `42e6a32` citable.
- **The `42e6a32` validation evidence exists as a claim, not a file.** A 3-seed smoke passed on
  it (in-pod manifest check, 0.00% pooled error, `utilization_coverage` 1.000) and a 20-seed
  shakedown passed on the pre-merge `90dd30a`. Both output directories were untracked and were
  lost to a `git clean` from a concurrent session. The numbers are recorded in rev 2 and in the
  CHANGELOG; the `run.json`/`job.log` that proved them are gone. Re-runnable in ~10 min if the
  artifact is ever needed:
  `SEEDS="1 2 3" BENCH_TAG=42e6a32 ./benchmarks/run_cell.sh kvaware 16 results-smoke`
  (keep it outside `results/`, do not commit it - it is not a measurement).
- **`docs/handoffs/prereg-review-31.md`** (untracked) still reads
  `status: signed-off-ready-to-run` with "Blockers: None ... it is ticked" directly under a box
  stating Amendment 1 was unsigned. It was unsigned at session start; both boxes were ticked
  mid-session. Stale, and it is the first thing a resuming session reads.
- **`export_summary.py` regenerates from disk and does not append**, so rerunning it drops any
  cell whose raw directory is gone. Five such cells existed (2-seed `kvaware` probes, 10 rows).
  They were dropped, not preserved: `scripts/reproduce.sh` check 1 (#28) requires every summary
  row to have committed raw data, and those rows fail it - on `main` as well as here, so that
  check fails on `main` today. Rows survive at `ffe1c77:results/summary-per-seed.csv`.
  `reproduce.sh` passes 5/5 on `main`, now over the confirmatory cells rather than the superseded 2026-08-05 ones, and runs in CI.
- **The `20260805-0*` sweep is WAN-measured and is excluded from the evidence base.** No
  `driver` key, no `window.env`, commit `a6eb563` predates #27. See Part 3.
- **`roundrobin` is passed to `plot_results.py` via `--comparator`, never as a positional run.**
  In `cells` it reaches every figure and its 11 s p95 flattens fig1's whole beta curve.
- **No GPU memory occupancy is collected.** DCGM has `GPU_UTIL`, `MEM_COPY_UTIL`,
  `POWER_USAGE` only - no `FB_USED`. `MEM_COPY_UTIL` is bandwidth, not occupancy.
  `vllm_kv_cache_usage_perc` is the working proxy and is what fig10's "GPU memory" panel plots.
- **Branch is not pushed.** No PR. Adversarial review (`/code-review`, `/simplify`) not yet run
  on the diff.

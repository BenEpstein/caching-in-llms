# Changelog

All notable changes to this project are documented here, newest first.
Format adapted from [Keep a Changelog](https://keepachangelog.com/en/1.1.0/): one entry per
work session (or significant commit), with **Added / Changed / Decided / Fixed** subsections as
applicable. Since this is a research project, **Decided** captures project-direction decisions
with a pointer to the evidence — those matter as much as code.

## [Unreleased]

## 2026-08-05 - Confirmatory sweep: the load term is the mechanism, beta=0.5 is the knee

Five cells, n=20 each, rate 16, OSL 64, one unattended batch 00:52-02:33. All valid
(pooled error 0.29-0.50%). Throughput flat at 14.33-14.54 req/s across every arm, so all
arms sat at the same operating point and latency differences are attributable to placement.

| cell | imbalance | ttft p95 | itl p95 | e2e p95 | hit rate |
|---|---|---|---|---|---|
| `kvaware` | 2.630 | 0.411 | 0.158 | 6.866 | 0.9118 |
| `loadaware-b0` | 2.647 | 0.463 | 0.184 | 8.196 | 0.9102 |
| **`loadaware-b0.5`** | **1.262** | **0.401** | **0.146** | **6.052** | 0.9035 |
| `loadaware-b1.0` | 1.209 | 0.417 | 0.151 | 6.064 | 0.8772 |
| `loadaware-b2.0` | 1.189 | 0.454 | 0.163 | 6.387 | 0.8725 |

### Decided
- **The load term is the mechanism, and the ablation is unambiguous.** beta=0 is
  indistinguishable from the baseline on imbalance (2.647 vs 2.630, **7/20 seeds, p=0.8847**)
  - cache-aware placement alone does nothing for load balance. Every beta >= 0.5 lands
  **19-20/20 at p<0.0001** against both `kvaware` and `beta=0`. One parameter, same binary,
  effect present only when it is non-zero: there is no room for an implementation artifact.
- **beta = 0.5 is the operating point.** It captures ~95% of the achievable balance for
  **0.7 pp of cache hit rate**, where beta=1.0 costs 3.3 pp and beta=2.0 costs 3.8 pp for a
  further 4% of imbalance. It is also best on every latency metric (TTFT, ITL and E2E).
  The knee reproduces yesterday's independent n=3 finding.
- **The headline does not depend on the beta pick.** Imbalance reduction is 44-54% at
  p<0.0001 for every beta in {0.5, 1.0, 2.0}. Report it as "the load term reduces imbalance
  ~50%, present at every beta >= 0.5, absent at beta=0" - robust to the operating-point
  choice, so no pre-registration of a specific beta is needed to defend it.
- **The beta-selection rule was NOT pre-registered and is ambiguous on this data.** "Within
  5% of the best reduction" picks beta=0.5 read as percentage points (2.81 pp below best) and
  beta=1.0 read as relative (5.12% below). Recorded as a defect in the rule, not resolved
  after the fact. This sweep is therefore **characterization**, not a confirmatory test of a
  pre-specified beta.

### Added
- **TTFT decomposed into engine vs non-engine, and the answer changes what is measurable.**
  Mean TTFT this session: client 254-333 ms, engine 133-159 ms, so **45-59% of measured TTFT
  never touches the model** (prefill alone is 95-110 ms). The non-engine half swung **121 ms
  (b0.5) to 195 ms (b1.0) between two cells an hour apart in the same session** - larger than
  the 10-60 ms arm differences being chased. That is a per-cell systematic offset, not
  per-request noise, so **more seeds cannot fix it**, and it is the complete explanation for
  why TTFT has been a null in every sweep this project has run.
- **Engine-side, the latency signal matches the imbalance signal**: mean engine TTFT
  `kvaware` 154.7, `b0` 158.9, `b0.5` **132.5**, `b1.0` 137.7, `b2.0` 157.6 ms - beta=0 at the
  baseline, beta 0.5-1.0 ~14% better. No p-values yet: these are cell-level histogram means,
  not per-seed pairs. Recovering the pairing needs per-seed histogram windows (each seed's
  window is derivable from `send_ts`), which is **zero cluster time** and the highest-value
  next step.
- Yesterday's drift confirmed as entirely non-engine: 91 ms (15:27) -> 308 ms (20:44) ->
  276 ms (21:43) while engine-side stayed 118-168 ms.
- `osl_tokens` added to `export_summary` columns; `results/summary-per-seed.csv` regenerated
  to **299 rows across 33 cells**.

### Fixed
- **`loadaware-b0.5` and `loadaware-b1.0` each name TWO DIFFERENT POLICIES** in
  `summary-per-seed.csv`, discriminated only by `git_commit`: before `7e2dffb` beta multiplied
  an ABSOLUTE in-flight count, from `7e2dffb` onward a fleet-relative one. They do not convert
  by a constant. `beta=0` is the only value meaning the same thing on both sides. Warned at
  the top of `export_summary.FIELDS`, where the file is generated.

## 2026-08-04 (night, later) - TTFT measures the WAN, not the system under test

### Decided
- **The driver's client-side `ttft_s` is not comparable across cells run at different times,
  and the TTFT co-primary is contaminated for every run in the project.** `load_driver.py`
  measures with `perf_counter` from send to first chunk, over the laptop->cluster link. That
  link is a WAN: RTT min 18.7 / avg 44.4 / max 132 ms, stddev 39.7 ms. Over the evening of
  2026-08-04 non-engine overhead went 258 -> 478 ms while **engine-side TTFT stayed flat**
  (0.168 -> 0.180 s). Evidence it is a constant per-request offset and not the policy: client
  TTFT rose uniformly including **p10 (2.0x)** - a floor shift, where a policy or GPU effect
  moves the tail - while ITL was untouched (a constant cancels in a difference between
  consecutive chunk arrivals) and E2E nearly untouched (~6 s of decode dominates).
  Ruled out first, verified not assumed: dataset (manifest byte-identical, 20/20 sha256),
  recording code (`load_driver`/`workload_gen`/`freeze_workloads`/`warmup`/`collectors`
  byte-identical), requests actually sent (`prefix_id` and `prompt_tokens` identical, schedule
  within 20 ms), engine (queue 0.01 ms, 0 preemptions, KV usage down, hit rate up), router
  (RSS/CPU flat, restarts every cell).
- **Load imbalance is derived from Prometheus, server-side, and is therefore immune.** This is
  now a mechanism rather than an observation, and it explains why imbalance reached p<0.0001
  while TTFT was a null in every sweep the project has run.
- **The metric of record is an open question, deferred to Ben.** Options and the per-seed
  windowing unlock that preserves the paired design are in
  `docs/handoffs/feat-relative-load-normalization.md`.

### Added
- **Engine-side TTFT collected from now on** (`prom_dump.METRICS`) and **backfilled into all 10
  rate-16 cells** from live retention. Prometheus storage is an `emptyDir`, so this was hours
  from unrecoverable. Engine-side p95: the two `b0.034` replicates 13 h apart agree to **1.8%**
  (0.336 / 0.342) where the client-side comparison across the same gap was off by 76% *with the
  sign inverted*.
- **Four cells at rate 16**, all valid: `loadaware-b1.0` n=20, `b0.5` n=3, `b0.25` n=3, and a
  `kvaware` n=3 **drift control** - the control is what exposed all of the above.

### Fixed
- **The prefix is 1544 tokens, not 2048, and ISL is 1578.** `workload_gen._filler` emits
  `approx_tokens * 0.75` words, so the `prefix_tokens: 2048` knob is a request to the
  generator and not its output. Verified two ways: the engine's `/tokenize` endpoint on the
  prefix substring (1544) and on the full prompt (1578), and `usage.prompt_tokens` on every
  recorded request (1578, min = median = max). "A misrouted request pays a full 2048-token
  prefill" appears in earlier entries and in `docs/handoffs/` - it is ~31% too large; the
  mechanism is unaffected but the number is wrong. `benchmarks/README.md` (which was also
  still claiming 20 prefixes and s=1.2) and `freeze_workloads.py` now carry the measured
  values. The knob keeps its name so the frozen manifest's checksums stay valid.
- **Which hit counter means what, settled by measurement.**
  `vllm:prefix_cache_{hits,queries}_total` count **TOKENS, not blocks** - queries/request is
  1573.8, tracking ISL 1578. Engine-side reuse on the `kvaware` n=20 cell is therefore
  **1435 of 1578 tokens per request (90.9%)**, or 92.9% of the 1544-token shared prefix.
  `lmcache:num_hit_tokens_total` is a **different tier** (CPU offload) and reads ~99
  tokens/request because KV never became scarce; it is not the cache-hit quantity and must
  not stand in for it.
- **Known gap: the router's `matched_tokens` is not recorded anywhere.** It is the actual
  input to `score_endpoint`, arrives via the Controller's `layout_info`, and surfaces only in
  a `logger.debug` while the deployment runs `logLevel: INFO`. So the realized ceiling of the
  benefit term is **unverified** - an earlier claim in this session that it caps at 0.973 was
  chunk arithmetic, not measurement, and is retracted. Evidence points the other way: the
  smoke test in `docs/handoff-core-implementation.md` got a **2048-token match on a
  ~2000-token prompt**, i.e. longer than the prompt, which is why `score_endpoint` has a
  `min()` guard. Difference is <=2.7% on the crossover either way, well under run-to-run
  noise, so it does not justify regenerating the frozen dataset. Documented in
  `LoadAwareRouter.score_endpoint`; resolving it needs a live probe with router debug logging.
- Two conclusions issued earlier this session and then withdrawn, both from cross-time
  comparisons: "beta=1.0 over-diverts and costs TTFT" and "the relative formulation costs
  double the cache hits". Neither survives the control. **Do not reinstate without re-deriving
  on the engine-side metric.**
- The 400-in-flight worked example conflated the load ratio (400/47 = 8.5) with the
  penalty-to-benefit ratio (0.034 x 400 = 13.6 against a benefit capped at 1.0).

## 2026-08-04 (night) - beta is dimensionless: load normalized against the fleet mean

Branch `feat/relative-load-normalization`, off `feat/evaluation-runs`. Code + docs only,
no runs yet. Builds directly on the evening sweep below.

### Decided
- **`alpha` is removed.** An argmax is invariant under positive scaling, so
  `alpha*benefit - beta*load` and `benefit - (beta/alpha)*load` are the same policy: alpha and
  beta were never two parameters, only their ratio. Every run in `results/` used alpha=1.0, so
  nothing measured changes. A test now asserts it cannot come back.
- **Load is normalized against the live fleet mean**, `(load - mean) / max(1, mean)`, so both
  terms of the score are dimensionless and beta carries no unit from the deployment. The old
  formulation could not ship a default: an absolute in-flight count has no bounded scale, so a
  beta of 0.034, tuned where the busiest engine ran ~47 in-flight, yields a penalty of **13.6**
  on a fleet running 400 - against a benefit term capped at 1.0, so the cache stops mattering
  and placement silently collapses to least-loaded, with nothing in the logs to announce it.
  This is the §4 "tunable
  parameters exposed and documented" requirement and the upstream-merge path (§4 grade-100),
  not a tidy-up.
- **`DEFAULT_LOADAWARE_BETA = 1.0`**, read as "an endpoint 100% above fleet-average load
  forfeits one full cache hit". No hardware, model, rate or fleet size in that sentence, which
  is what makes it defensible without a probe.
- **`load_gate.beta_from()` is deleted.** It solved `beta*delta_load = alpha*trigger` from ONE
  probe's absolute concurrency, and the residual `trigger=0.5` was itself arbitrary. Evidence
  it had to go: two probes at the same offered rate gave delta_load 39.46 and 14.69 (beta
  0.013 and 0.034) because they caught the fleet at 39.5 vs 20.8 mean concurrency. Replaced by
  `relative_imbalance()`, which **reports** the quantity the policy acts on instead of
  calibrating a parameter from it.

### Added
- Evidence, computed from the committed prom scrapes - **no new cluster time**. Across the four
  **untreated** rate-16 cells (probe A, probe B, `kvaware` n=20, `loadaware-b0` n=20) the
  absolute calibration spans **2.69x** and the relative one **1.41x**; across the three cells at
  comparable fleet load it spans **1.06x** (0.500 / 0.501 / 0.529). The residual is entirely
  probe B, a 13-sample 66 s probe that caught the fleet at half the concurrency of the others.
  Treated cells are excluded as circular - their imbalance is the residual *after* the policy
  acted.
- **The evening sweep's two optima both sit next to the new default**, via
  `beta_rel = beta_abs * live_fleet_mean`: the TTFT optimum `beta_abs=0.034` (mean load
  26.6-27.3) is **beta_rel 0.90-0.93**, and the ITL optimum `beta_abs=0.068` (mean load 19.65)
  is **beta_rel 1.34**. So beta=1.0 lands between the two measured optima, and the published
  n=20 headline was already run within ~8% of it. That is the confidence argument for the
  rerun, and it is the reason the new grid is {0, 0.5, 1.0, 1.5} rather than a decade wide.
- Router tests 56 → 60: scale invariance (same decision at 10x load), fleet-relative load at
  n=4 engines, the near-idle clamp, and "there is no alpha". `test_load_gate.py` gains four
  `relative_imbalance` cases. **120 tests pass.**
- **A more transferable §5 statement**: treated cells sit at relative imbalance 0.069 / 0.122
  against 0.47-0.50 untreated, i.e. the load term cuts relative imbalance **4-7x**.

### Known gaps
- **Untested above 2 engines in anger.** With n=2, `(load-mean)/mean` is ±r by construction and
  only encodes which side of the mean an engine is on; larger fleets have real structure. Unit
  tests cover n=4 on the pure `select_url` path, but no cluster has run it.
- **The load SIGNAL is unchanged** - still a request count, not Dynamo's unique KV blocks. This
  fixes the scale, not the deduplication. `docs/upstream-findings.md` Finding 5 stands as the
  named next experiment.
- **No runs yet.** The headline and grid must be re-measured at rate 16. `kvaware` and
  `loadaware-b0` are unaffected (beta=0 zeroes the load term either way) and are NOT rerun.
  Old runs stay in `results/` for the before/after comparison.
- **The evening sweep's beta cells become historical.** Their beta values are in the old
  absolute units and are only comparable to the new ones through the live-mean conversion
  above, which holds at the mean and not per-decision (the router now recomputes the mean every
  request). Reported as a conversion, never as a re-label.

## 2026-08-04 (evening) - Sweep complete: beta curve has an interior tradeoff

### Added
- **`loadaware-b0.068` n=3** (`results/20260804-190542-loadaware-b0.068`) and
  **`roundrobin` n=3** (`results/20260804-191644-roundrobin`), both rate 16, 0.00% errors.
  The rate-16 sweep is now complete at 5 arms.
- All 9 figures regenerated from the full cell set; `fig8`/`fig9` tracked for the first time.
- `results/summary-per-seed.csv`: 160 rows across 19 cells.

### Decided
- **beta trades TTFT against ITL, and the optimum differs by metric.** Medians at rate 16:

  | arm | n | TTFT p95 | ITL p95 | imbalance | achieved req/s |
  |---|---|---|---|---|---|
  | `roundrobin` | 3 | 11.528 | 0.863 | 1.723 | **10.7** |
  | `kvaware` | 20 | 0.426 | 0.171 | 2.680 | 14.2 |
  | beta=0 | 20 | 0.438 | 0.185 | 2.646 | 14.2 |
  | beta=0.034 | 20 | **0.378** | 0.143 | 1.296 | 14.5 |
  | beta=0.068 | 3 | 0.550 | **0.097** | **1.061** | 14.7 |

  Raising beta diverts more requests off their cached engine: decode gets faster (smaller,
  more even batches -> ITL p95 falls 32%) while prefill gets slower (more misses -> TTFT p95
  rises past the baseline). beta=0.034 sits near the TTFT optimum, beta=0.068 near the ITL
  optimum. Same mechanism that made beta>=0.5 collapse at 10.5 req/s, caught here while it is
  still a tradeoff. **n=3, descriptive only** - these cells cannot be paired against the n=20
  arms and the seed-set check correctly refuses to try.
- **`roundrobin` is better balanced than the cache-aware baseline (1.723 vs 2.680) and 27x
  worse on TTFT p95.** It equalises request COUNTS, not work: a misrouted request pays a full
  2048-token prefill. This is the cleanest statement in the project of what "load" means, and
  it is why the extension must add load-awareness on top of cache-awareness rather than
  replacing it. **Caveat: it achieved only 10.7 req/s against 16 offered** while every other
  arm delivered 14.2-14.7, so it is saturated and not at the same operating point - the
  throughput shortfall is the honest headline for that arm, not the 27x.

### Fixed
- Published seed count corrected on issue #7: TTFT p95 improves on **12/20** seeds, not 13/20.
  p, median and CI unchanged; the endpoint is a null either way. All other published counts
  re-verified correct (imbalance 19/20 both candidate cells, beta=0 vs kvaware 9/20,
  beta=0.034 vs beta=0 19/20).

## 2026-08-04 (later) - Headline + ablation measured; seed labels were lying

### Added
- **`kvaware` n=20 at rate 16** (`results/20260804-151901-kvaware`) - the comparator the
  sweep had been missing. Pooled error 0.56%, all validity rules pass.
- **`loadaware-b0` n=20 at rate 16** (`results/20260804-155356-loadaware-b0`) - the ablation.
- `results/summary-per-seed.csv` regenerated: 154 seed rows across 17 cells.

### Decided
- **The load term is the mechanism, not the routing implementation.** Median load imbalance:
  `kvaware` 2.680, `loadaware` beta=0 **2.646** (p=0.2979, 9/20 - indistinguishable from the
  baseline), `loadaware` beta=0.034 **1.296**. Latency agrees: beta=0 vs kvaware is p=0.8529
  on TTFT p95 and p=0.4927 on ITL p95. Turning beta on produces the entire effect. This was
  pre-declared as falsifiable - a beta=0 result near 1.30 would have voided the headline.
- **Pre-registered headline (issue #3 comment, posted before the comparator ran):** load
  imbalance **-48.3%, p<0.0001, 19/20 seeds**, replicated across both candidate cells
  (-52.7% against the 00:29 cell). **TTFT p95 is a null** (p=0.1305, median -8.2%, CI spans
  zero). Co-primary threshold 0.025 (Bonferroni over 2).
- **`itl_p95` stays a SECONDARY and is not promoted.** It is nominally significant against
  kvaware (p=0.0291) and against beta=0 (p=0.0060, CI [0.5%, 34.1%]), but it crosses the
  line between replicate cells (p=0.0570 vs the 00:29 cell), and promoting a metric after
  seeing its p-value is the same error as adding seeds. Reported as supporting evidence.

### Fixed
- **Per-seed labels named the wrong seed everywhere they were printed or plotted.**
  `read_run` sorted the glob LEXICOGRAPHICALLY (`seed1, seed10, seed11 … seed2, seed20`)
  while `cmd_compare` and `fig_paired` labelled rows with `enumerate()`, so every "seed N"
  above N=1 was wrong (printed "seed 2" was really seed 10). **Pairing was unaffected** -
  both arms were mis-ordered identically - so every p-value, median and CI in the project
  is correct, and only the labels lied. Verified by re-running the headline under numeric
  ordering: p=0.1305, W+=74.0, identical. The committed `summary-per-seed.csv` also escaped
  (it derived the seed from the filename); all 74 pre-existing rows re-verified byte-identical.
  `docs/figures/fig4-paired-seeds.png` IS mislabelled and awaits regeneration with the full
  cell set. Fixed at the root: the seed number is now a carried field on `seed_stats`.
- **`compare` matched on seed COUNT, not seed SET.** Two cells could each hold 20 CSVs drawn
  from different seeds and `zip()` would pair them silently, reporting a clean p-value for a
  comparison that never happened. Now matches the set and names the offenders; `fig_paired`
  guards the same way. 116 tests (was 110).

## 2026-08-04 - Confirmatory sweep at the knee: driver bug fixed, load gate added

### Fixed
- **The driver recorded NO TTFT at all** (regression from `e29cb4a`). 500 rows with an empty
  TTFT column while token counts populated normally, so a CSV looked plausible at a glance
  while missing the primary metric. `stream_options.include_usage` puts a `usage` key on
  *every* chunk (`null` on token chunks), so the substring test `'"usage"' in data` matched
  all of them and `continue`d past the TTFT/ITL recording; the final chunk still set the
  token counts. Fixed by classifying on whether a chunk **carries a token** (`choices`
  non-empty), extracted as a pure `classify_chunk()`. Verified live: 20 requests x 64 tokens
  -> 1260 gaps (= 20 x 63). **This makes the 2026-08-04 sweep the first run in the project
  with a valid ITL measurement**; any ITL figure in PR #22 never existed.

### Added
- `benchmarks/load_gate.py` - the load analogue of the scarcity gate, run on a short kvaware
  probe before a sweep is funded. Validated against the known-bad case: it FAILS both
  2026-08-03 headline cells and reproduces their published 1.68x imbalance.
- Regression tests for the paths flagged as uncovered in #7 (ITL parsing, old-CSV `.get()`
  fallback, the `job=vllm-engines` filter that corrupted 17 of 74 seed rows) plus the gate,
  the chunk classifier, and the amended error rule. **110 tests.**
- Seeds 10 -> 20 (`freeze_workloads.py`), verified purely additive: seeds 1-10 regenerate
  bit-identically, only the manifest grows.

### Decided
- **This system saturates on COMPUTE, not memory - so the baseline's pathology is
  decode-batch concentration, not queueing.** Measured: at offered 16 and 18 the busiest
  engine ran 59 and 100 mean concurrent requests with `num_requests_waiting` = 0.00, **0
  preemptions**, and queue time **0.0 ms/req at every rate including 10.5**. Prefix caching
  makes concurrency nearly free in KV (~530 tokens per in-flight request against 1578-token
  prompts), so against the verified 104,624-token pool KV tops out near 0.70 and never
  exhausts. Confirmed by the gate probe: ITL p95 degraded **4.55x** over idle while TTFT p95
  degraded 2.69x.
- **The first load-gate criterion (preemption or waiting > 0) was falsified as
  unsatisfiable, not strict, and replaced by measured degradation** (TTFT p95 or ITL p95
  >= 1.25x an idle baseline, plus asymmetry >= 1.5x). Testing the consequence is stricter
  than testing a counter - a counter can fire without harming anyone.
- **The rate-selection rule "achieved/offered < 0.90" is falsified**: it picks rate 12,
  which sits in the flat region (TTFT p95 0.251 s, identical to rates 8 and 10). The ratio
  declines smoothly from 0.99 with no knee in it. Replaced by departure from the latency
  plateau -> **rate 16** (asymmetry 2.09x, TTFT p95 2.69x idle, ITL p95 4.55x idle, 0 errors).
- **Validity rule 1 amended BEFORE the run**: an arm-independent error floor is reported,
  not fatal; a comparison is voided only when error rates differ materially between arms
  (> 2x ratio AND > 1pp absolute), with a 10% catastrophic ceiling retained. The 500s are
  arm-independent (present in roundrobin, which never touches the registry; 16/16 tracebacks
  are post-routing `ServerDisconnectedError`), and the probe measured exactly 1.0% on one
  seed - so the old rule would have discarded 85 minutes on noise.
- **beta = 0.034 at rate 16**, from the measured `delta_load` = 14.69 via
  `beta * delta_load = alpha * 0.5`. Recorded instability: a second probe at the same rate
  gave `delta_load` = 39.46 (beta 0.013). The 0.034 figure comes from the only probe whose
  driver recorded TTFT; the other is UNMEASURABLE by the gate's own criterion. The spread is
  a direct consequence of beta being tied to absolute concurrency - the §6 limitation the
  project deliberately chose not to fix mid-experiment.
- Ben's `results/rate-pilot/` CSVs are **provenance-verified** as the current workload
  (`max(prefix_id)` = 126, 72 distinct prefixes), so the rate ramp did not need repeating.
- **Eliad works solo from 2026-08-04**; Ben is off the project. `CLAUDE.md` still says two
  people and is stale.

### Added (2026-08-03, late)
- **`results/` is now tracked in git** (364 files, 5.2 MB): raw per-seed driver CSVs, `dcgm.csv`,
  and the `prom/*.json` scrapes for every run, so a collaborator or grader can inspect the
  evidence behind every figure without rerunning the cluster.

- **Finding 5 in `docs/upstream-findings.md`** - our load term vs NVIDIA Dynamo's KV-router,
  with a drop-in §6 Discussion paragraph. Measured the prefix-dedup factor in our own workload
  (0.69 at 10.5 req/s, 0.45 at 7.5) by reconstructing the in-flight set from the driver CSVs.

### Decided
- **Do not adopt Dynamo-style deduplicated-block load accounting; document it as a limitation.**
  It needs per-worker block tracking plus a completion hook (i.e. patching `request.py`), which
  invalidates every run in `results/`, and the headroom is ~9% on imbalance and ~5% on hit rate
  since neither ever became scarce (KV usage max 33%, `num_requests_waiting` 0, round-robin ties
  the cache-aware arms at 0.95 hit rate). Revisit only under a cache-scarcity rerun
  (`benchmarks/scarcity_gate.sh`). Evidence: `docs/upstream-findings.md` Finding 5.
- **Accept the router's leaked `in_prefill` counters in the committed runs.** 39 backend
  disconnects (`results/router-errors.log`) hit a path where `on_request_complete` is skipped, so
  the router's own in-flight gauge drifts +4 to +7 on one engine over a run (floor of
  `vllm:num_requests_running{job="router"}` minus the engine gauge). Bias runs *against* the
  extension, is ~10x smaller than the reported imbalance effect, and the zero-failure cell
  `20260803-163350-loadaware-b0.1` reproduces the result. Fix upstream (`on_request_complete` into
  a `finally`) only if we rerun above the knee.
- **Commit raw run artifacts, keep frozen workloads out.** `.gitignore` now ignores only
  `results/**/*.jsonl` - the 2.5 MB `rate-pilot/pilot-seed999.jsonl` is regenerable from
  `benchmarks/workloads/manifest.json`, which is the existing checksum-manifest policy for
  workload data. Everything else is small enough that reproducibility beats repo size.

## 2026-08-04 - Post-mortem on the amended sweep: the rate was the bug

### Decided
- **The 10.5 req/s operating point invalidates the latency co-primary, not the policy.** In both
  headline cells `vllm:num_requests_waiting` is 0.00 mean *and* 0.00 max on both engines, queue
  time is 0.0-0.8 ms, and KV usage is 15-19%. Nothing queued anywhere, so a load-aware router had
  no load to be aware of and the 4.7% TTFT gap is residual cache locality alone. Evidence:
  `results/20260803-{210741-loadaware-b0.1,212450-kvaware}/prom/`.
- **The knee was never measured.** `rate_pilot.sh` defaulted to 2..10 req/s and TTFT p95 is flat
  across it (0.212 / 0.259 / 0.251 / 0.249 s at 4/6/8/10). The 20-rate CSVs in
  `results/rate-pilot/` put the knee at 14-16 (20 offered yields 14.9 achieved), so 10.5 was
  ~70% of a knee the pilot had not reached, deep in the flat region. Any rerun must be at or just
  under the knee, and that is a new pre-registration.
- **Do not cut seeds to shorten a run; cut cells.** Measured on the 2026-08-03 sweep: ~8 min of
  fixed setup per cell against ~50 s per seed replay, so 6 cells were 48 of the 76 minutes. Cut
  `loadaware-b1.0` and one roundrobin seed instead, and keep `loadaware-b0` - it is the ablation
  that isolates what beta buys. Sweep is now 5 cells / 30 replays.

### Fixed
- **Per-engine imbalance mixed in router-scraped series** (`export_summary.py`, `plot_results.py`).
  `vllm:num_requests_running` is exported twice: per pod under `job=vllm-engines`, and per backend
  under `job=router` where every series shares `instance="stack-router-service:80"` and differs
  only by `server`. Keying on `pod or instance` collapsed both router series into one bucket
  averaging the two engines, and that synthetic third "engine" entered the max/min. It changed 17
  of 74 committed seed rows. Corrected, the imbalance co-primary strengthens: median reduction
  24.8% -> **25.9%**, p 0.0068 -> **0.0049**. `results/summary-per-seed.csv` regenerated.

### Added
- **Inter-token latency is now measured.** The driver records every inter-token gap
  (`itls_ms`), and `analyze.py` reports pooled `itl_p50/p95/p99`. Decode is ~92% of E2E at OSL=64
  and was previously unmeasured except as an aggregate.
- **`ttft_p90`** in the per-seed stats. On the 2026-08-03 data the policy shifts the whole TTFT
  body - paired over 10 seeds, p50 p=0.0029 (9/10, 6.9%) and p90 p=0.032 (8/10, 7.4%) - while
  p95 p=0.0527 and p99 p=0.81. The TTFT outliers arrive in bursts at single instants (loadaware
  seed 9: five of the six worst all at t=29 s), so per-seed p95/p99 largely counts engine stall
  events, not routing quality. **Reported for diagnosis; the headline metric stays as
  pre-registered** - switching it after seeing these p-values would be optional stopping.
- **`fig8-itl-percentiles.png`** and **`fig9-throughput.png`**; `fig5-percentiles` gains a p90
  panel and seed-spread whiskers.
- **Client event-loop lag probe** in the driver, printed per seed. TTFT is timestamped inside the
  client's event loop, so an effect smaller than the loop's own lag is not measurable; this makes
  that checkable instead of assumed.

## 2026-08-03 - Amended sweep complete: imbalance significant, TTFT not

### Added
- Full 6-cell sweep at the amended config (10.5 req/s, 128 prefixes s=0.9, u=0.45):
  `results/20260803-2*`. `fig7-beta-tradeoff.png` is the new causal figure (hit rate vs β on
  one axis, TTFT p95 on the other).

### Decided
- **Headline: `loadaware` β=0.1 achieves significantly better load balance at statistically
  indistinguishable latency.** TTFT p95 **p=0.0527, NOT significant** (7/10 seeds, median 4.7%,
  CI [-4.2%, +8.2%]); load imbalance **p=0.0068, significant** (8/10 seeds, median 24.5%,
  survives Bonferroni for two co-primaries). No seeds added after seeing p=0.0527 - that would
  be optional stopping. Full write-up:
  <https://github.com/BenEpstein/caching-in-llms/issues/7#issuecomment-5170943693>.
- **β≥0.5 blows up 4-6x, and the cause is measured rather than inferred.** Prefix-cache hit rate
  falls monotonically with β (0.918 → 0.787 → 0.735) because diverting a request off its cached
  instance now costs a real 2048-token prefill. This is the interior optimum the amended
  workload was built to expose, and it exists - the pilot's β curve was degenerate.
- **Cache-aware routing is the dominant effect: `roundrobin` posts TTFT p95 5.502 s, 18x worse
  than kvaware/b0.1**, with 0.709 hit rate, 25 preemptions and 0.420 s mean queue time. Random
  placement against a scarce cache is catastrophic.
- **The pilot's 3.7x kvaware imbalance was substantially a registry artifact.** It falls to 1.68x
  once prefixes are genuinely spread, because "route to the holder" then spreads load as a side
  effect. That convergence, not a measurement problem, is why the latency effect shrank from 17%
  to 4.7%.
- **The 25 HTTP 500s (0.17%) are NOT the stale-id bug**: they appear in every arm including
  roundrobin, which never touches the registry. Arm-independent, so they do not bias the
  comparison. Root cause unidentified - captured tracebacks hold only starlette frames.

### Fixed
- `run_cell.sh` held the Prometheus port-forward open for the whole cell; it died before the
  dump and `set -e` discarded three completed cells. Now forwarded at dump time with retries,
  and a failed dump warns instead of aborting - driver CSVs are the latency source of truth.

### Gaps for the next run
- `vllm:num_preemptions_total` + vLLM prefix-cache counters missing for the two **headline**
  cells (collector fixed in ba6caae only after they ran) - so the headline arms are absent from
  fig7 and preemption is unquantified where it matters most.
- n=10 is underpowered for a 4.7% effect; pre-register n≈20 before seeing data.
- β grid too coarse: everything happens between 0 and 0.5. Sample {0.05, 0.1, 0.2, 0.3}.
- **β=0 posts the lowest p95 (0.289) and highest hit rate (0.918) at n=3.** If pure
  cache-awareness beats the load-aware variant on latency, that bears on the project's premise
  and needs n=10, not 3.

## 2026-08-03 - Scarcity gate falsified the first amendment; workload re-derived

### Fixed
- **The scarcity gate was reading a cumulative metric as if windowed.** vLLM's
  `Prefix cache hit rate` log field counts from engine start, so a `--since` grep reports the
  running average, not the window. It read 0.085 mid-warm-up and returned a false PASS where
  the true windowed rate was 0.889. Now takes a delta of
  `vllm:prefix_cache_{queries,hits}_total`, and measures under load rather than at warm-up
  concurrency (with negligible in-flight KV, almost the whole pool is free for retention and
  the config flatters itself).

### Decided
- **64 prefixes at s=1.2 is falsified: measured 0.889 under load vs the pilot's ~0.95.** The
  realised KV pool was 104,624 tok on both engines against a predicted ~99,000, so the
  calibration held - the design point was wrong. In-flight KV at 7.5 req/s is far below the
  56k projected from kvaware's concentrated peak, so the hot set still fit.
- **Scarcity is a count condition before it is a distribution condition.** An LRU simulator
  over the real generator (validated against the measurement: predicted 0.865, measured 0.889)
  shows that when the pool fits in cache the exponent does *nothing* - pool=20 gives 0.960 at
  s=0.9 and 0.960 at s=0.0, identical. And at s=1.2 pool size barely helps: 0.687 even at 256
  prefixes, because the concentrated head stays resident however long the tail grows. Both
  conditions are needed.
- **Workload re-derived: `prefix_pool_size` 64 -> 128, `zipf_s` 1.2 -> 0.9.** Gate PASSES at
  **0.711** (threshold 0.75). s=0.9 rather than lower is deliberate: s≈1 is the canonical Zipf
  exponent, so it reads as a normal serving profile, where flatter exponents reach the same
  scarcity but describe near-uniform prefix popularity that nobody observes. Revision:
  <https://github.com/BenEpstein/caching-in-llms/issues/3#issuecomment-5169741456>.
- **The simulator is a design tool, not a predictor of the metric.** Measured 0.711 vs
  predicted 0.605 is consistent with effective capacity ~55 rather than 38 prefixes: in-flight
  KV is smaller than assumed, and vLLM's counter is block-level so a partially-evicted prefix
  still scores partial hits. It will always read above a whole-prefix simulation.
- **§6 framing**: the pilot workload could not discriminate the policies and the evaluation
  workload was re-derived so the working set exceeds cache capacity, with the gate
  measurements as evidence. Pilot numbers stay in §5 as the contrast.

## 2026-08-03 - Methodology amended (#3): the pilot could not test the hypothesis

### Decided
- **The 2026-08-03 sweep is demoted to a pilot; #3 is amended and pre-registered before any
  new data.** Root cause: the per-engine HBM KV pool is 393,744 tokens (13.52 GiB, verified in
  the engine startup log) and the 20-prefix working set is 40,960 tokens - 10.4% of it. Every
  engine held every prefix, so there was no placement decision for cache-aware routing to get
  right. Amendment: <https://github.com/BenEpstein/caching-in-llms/issues/3#issuecomment-5169437718>.
- **The earlier "shrink the CPU offload tier" proposal is withdrawn.** It cannot force a
  recompute: the KV stays resident in HBM regardless, so CPU eviction would only desynchronise
  the registry from reality. Credit to the cache-sizing handoff session for the correction
  (`docs/handoffs/cache-sizing-decisions.md`).
- **HBM-only is dead, not merely blocked.** Only `local_cpu_backend` and `local_disk_backend`
  emit `KVAdmitMsg`; there is no GPU-resident tracked backend. Without a backend the registry
  is empty, `lookup()` returns nothing, and both arms collapse to load-only routing (#13).
- **Scarcity is defined against the Zipf hot set, not the nominal pool.** The handoff's 32
  prefixes gave retention 1.1x the hot set (16 retained vs 18 hot at s=1.2) - no scarcity where
  the traffic is. Locked instead: `gpuMemoryUtilization` 0.90 -> **0.45** (~99k tok) and prefix
  pool 20 -> **64** (hot set 31), giving retention 0.7x the hot set with 1.8x headroom over
  kvaware's measured peak in-flight KV. The handoff's `u=0.37` was a units error (GB vs GiB);
  measured calibration is 22.49 GiB total, 6.72 GiB non-KV overhead, 29,123 tok/GiB.
- **Preemption is a reported outcome, not a run-voiding gate.** Under concentration it is a
  genuine consequence of the baseline's placement; gating on it would discard the baseline arm
  systematically and yield no result.
- **Single-stage run plan, ~1.7-2.3 h instead of ~4.5-5.2 h.** The two-stage design existed to
  *select* β, but β=0.1 is the shipped default and was already the pre-registered headline - it
  is being tested, not chosen. Headline pair (b0.1, kvaware) at 10 seeds; β-sweep and
  roundrobin at 3. Requests/seed stays 500: the 900 figure assumed first-touch prefills that
  `warmup.py` already eliminates. n=10 on the headline pair is not negotiable - at n=6 one
  reversal caps exact Wilcoxon at p=0.219 regardless of effect size, which is what the pilot hit.
  Revision: <https://github.com/BenEpstein/caching-in-llms/issues/3#issuecomment-5169446888>.

### Changed
- `deploy/values-baseline-kvaware.yaml`: `gpuMemoryUtilization` 0.90 -> 0.45, with the
  calibration and the verify-from-the-log requirement in-line.
- `freeze_workloads.py`: 64 prefixes, seeds 1-10; workload regenerated and re-frozen (new
  manifest SHA-256s). `run_cell.sh` takes a `SEEDS` env var instead of hardcoding 1..6, and
  `run_sweep.sh` maps each cell to its seed count.

## 2026-08-03 - Ticket #7: full 6-cell evaluation sweep executed on gapu-2

### Added
- `benchmarks/plot_results.py` + `docs/figures/`: the three §5 figures, built from the same
  `analyze.py` per-seed stats as the tables so a figure cannot disagree with its table.
  Centerpiece `fig1` (TTFT p95 vs β) shows β=0.1 is the minimum and β≥0.5 converges onto the
  baselines.
- Full sweep at 7.5 req/s, 6 cells × 6 seeds × 500 req, all validated (errors ≤ 0.6%, every
  registry probe 4/4, every warm-up gate ≥ 20 cache-path routings):
  `results/20260803-{161908-loadaware-b0,163350-loadaware-b0.1,164940-loadaware-b0.5,170418-loadaware-b1.0,171846-kvaware,174639-roundrobin}`.

### Fixed
- `run_cell.sh` step 4 (worker-registration gate) is now gated on `USES_LOOKUP`, like the
  registry probe and warm-up gate already were. A `--routing-logic roundrobin` router never
  instantiates the LMCache controller (verified: zero controller lines in its log), so no
  worker ever registers and the gate could only time out - it killed the roundrobin cell on
  the first sweep attempt. Lookup arms are unaffected, so the five completed cells stand.

### Decided
- **Cache hit rate cannot be the §5 headline metric at this workload.** `lmcache:lookup_hit_rate`
  is scraped from the *engines* (`job=vllm-engines`), so it measures each engine's hit rate
  against its own local cache, not whether the router picked the instance holding the KV. With
  a 20-prefix pool it saturates at ~0.95 on **every** arm including roundrobin (`fig3`). TTFT
  is the discriminating metric; `fig3` is kept to document the null, not to claim a win.
- **The pre-registered headline does not reach significance at n=6, and the seed count is the
  binding constraint, not the effect.** loadaware β=0.1 vs kvaware: 5/6 seeds improve, median
  −17.0% TTFT p95, exact one-sided Wilcoxon p=0.219. Seed 2 reverses (0.418 s vs a 0.20 s cell
  median; its TTFT *mean* is also elevated, so it is a whole-seed excursion, not one bad
  request). At n=6 a single reversal caps exact Wilcoxon at p=0.219 regardless of effect size -
  only 6/6 in one direction clears p<0.05. Seed 2 passes every validity rule and is **not**
  dropped (rule 2 forbids post-hoc exceptions). Raising the seed count is a change to the
  frozen methodology (#3) and is pending Ben's decision on #7.
- **Cell position is not a confound.** The two independent kvaware cells - the #21 dry cell and
  the in-sweep cell 5, ~1.5 h apart - agree closely (median TTFT p95 0.272 s vs 0.259 s), so the
  ~3 h drift I flagged before the run does not drive the headline.

## 2026-08-03 - Ticket #21 root-caused: both router-stability issues explained

### Fixed
- `load_driver.py` now reads the response body on a failed request before raising. Streamed
  responses arrive body-unread, so `raise_for_status()` was discarding the server's error
  detail - all six demo CSVs recorded a bare "500 Internal Server Error" with no cause. The
  error column is widened 200 -> 500 chars to fit a traceback summary.

### Decided
- **Issue 1 (SIGKILL at 4 req/s) is a liveness starvation caused by a blocking HF call on the
  event loop, and it is baseline-inherent - it caps the pilot rate, it does not invalidate the
  comparison.** Evidence, all on the live `:c68ccfc` deploy: the router pod cannot write an HF
  cache (runs as uid 1001020000, `HF_HOME` unset, no writable `/.cache`), so
  `AutoTokenizer.from_pretrained` fails on **every** request - the exception path never sets
  `self.tokenizer`, so it is retried per request, and the failing call costs **245 ms median**
  because it reaches huggingface.co over the network before failing. Measured in-router
  blocking is **0.248 s/request mean** (n=644, p95 0.282 s), giving a single-event-loop ceiling
  of **4.04 req/s** - the router was killed at exactly 4 req/s. The kill is the liveness probe,
  not memory: `reason: "Error"` (not `OOMKilled`) with `timeoutSeconds: 1`, period 5s,
  threshold 3. `KvawareRouter.route_request` contains the identical try/except, so the stock
  baseline arm pays the same cost.
- **Issue 2 (background 500s) is not ours: 16/16 tracebacks are
  `aiohttp.client_exceptions.ServerDisconnectedError`** raised in upstream
  `request.py:164` while opening the upstream connection, i.e. after the routing decision had
  already succeeded. The router's aiohttp pool holds idle engine connections **15.0 s**
  (`TCPConnector(limit=0)`, aiohttp default) while the engine closes them at **5 s**
  (vLLM `VLLM_HTTP_TIMEOUT_KEEP_ALIVE=5`, not overridden). Arm-independent: the code path is
  shared by every routing logic.
- **That timeout mismatch alone is NOT sufficient, and the leading hypothesis now links the two
  issues.** A controlled sequential probe (90 requests, idle gaps 1.0/2.0/2.5/3.0/4.0/5.0 s,
  n=15 each) returned **90/90 HTTP 200** - with one connection in play aiohttp processes the
  engine's FIN and discards the dead connection cleanly. The demo 500s occurred only under
  concurrency, and the suspected mechanism is that the 0.25 s/request event-loop block above
  *delays the router's processing of the engine's FIN*, so closed connections linger in the
  pool as reusable and get handed out. **That experiment has now run and both issues cleared
  on the single fix** (below), which is the intervention evidence for the link.

### Fixed
- **`HF_HOME=/tmp/hf`, applied to BOTH arms via `benchmarks/run_cell.sh`, clears both issues.**
  It cannot live in values: chart 0.1.11's `deployment-router.yaml` hardcodes the router env
  list and declares no volumes at all (verified against the pulled chart, 2026-08-03), so there
  is neither a `routerSpec.env` passthrough nor a volume hook - and `/tmp` is the only writable
  path in the container. Set on baseline cells too; an arm-only fix would flatter loadaware.
  Verified live after rollout:
  - in-router blocking **221 ms -> 1 ms median**; first request costs 3.50 s once (the tokenizer
    load), which the existing warm-up gate already absorbs
  - remote `/tokenize` calls **933/7 -> 0/0** across the two engines
  - 2 req/s x 3 seeds x 100: **300/300 OK, 0 tracebacks** (pre-fix 16/600 = 2.7%; under a 2.7%
    rate, 0-in-300 has p ~ 3e-4)
  - the 4 req/s repro that previously SIGKILLed the router: **100/100 OK, 0 restarts**
  Caveat: this is a before/after on a shared cluster, not a controlled A/B - the workloads are
  the frozen seeds 101-103 in both cases, but engine cache state differed.

### Changed
- Confounder found while root-causing: because local tokenization always fails, every request
  falls back to a synchronous `requests.post(.../tokenize)` aimed at `endpoints[0]` - **933
  tokenize calls landed on engine-0 vs 7 on engine-1** over the demo window. That is an
  unmeasured load asymmetry pointed at one engine, which biases a *load-aware* routing
  experiment specifically. Must be fixed before #7, identically in both arms.
- Verified the live router overlay (`router-patch` configmap: `routing_logic.py`,
  `kv_controller.py`, `parser.py`) is **byte-identical** to the branch, so the demo runs did
  exercise the intended code. Corrects the handoff's "NO dev overlay mounted" note.

## 2026-08-01 (late night) - Ticket #6 completed: benchmark harness built

### Added
- Benchmark harness implementing the locked methodology (issue #3): `freeze_workloads.py`
  (6 frozen seeds pinned by SHA-256 manifest), `warmup.py`, `collectors/prom_dump.py` +
  `collectors/dcgm_poll.py`, `run_cell.sh` per-cell choreography (deploy → #13 gates →
  warm-up → 6 seeds → collect → `run.json`), `run_sweep.sh`, `rate_pilot.sh`, and
  `analyze.py` with the pre-registered exact one-sided Wilcoxon + bootstrap CI
  (stdlib-only). 30 new unit tests; `benchmarks/README.md` pre-registers the validity rules.

### Changed
- `workload_gen.py`: `pool_seed` split from `seed` so all 6 replay seeds share ONE frozen
  prefix pool (a single warm-up must cover every seed). `load_driver.py`: `send_ts` is now
  wall-clock epoch (aligns rows with Prometheus/DCGM windows), optional `--summary-json`,
  and `--insecure` opt-in TLS flag (gapu-2 self-signed route) instead of implicit trust.

### Changed
- **Review + simplify pass over PR #20** (/code-review + /simplify, 4 findings applied,
  ~90 lines net removed): `percentile` now lives only in `analyze.py` (driver imports it);
  tautological Wilcoxon brute-force test replaced with a hand-computed midrank-ties case;
  driver `--summary-json` dropped (windows derive from CSV `send_ts`); unused
  `prom_dump --metric` dropped; redundant bootstrap sorts removed; DCGM poll sleeps to a
  deadline. Correctness from review: driver pins OSL via `ignore_eos` (early EOS was
  leaking output-length variance into E2E/throughput), unbounded httpx connection pool
  (pool-queueing would count as TTFT past the knee), per-chunk JSON parse removed from the
  measurement path, `--since` log windows floored at the gate timestamp (probe traffic
  could satisfy the warm-up gate), router image asserted per cell and
  `analyze.py compare` refuses runs with differing rate/workload manifests
  (validity rules now enforced, not recorded).

### Fixed
- **Live verification on gapu-2 falsified four harness assumptions** (issue #6, PR #20):
  (1) chart 0.1.11 silently ignores `routerSpec.env` - α/β now travel via `oc set env`
  per cell, removed on baseline cells so a stale β can't leak through the three-way merge;
  (2) the pinned router exposes NO `registered_workers_count` gauge - registration gate is
  now the router's `Registered instance-worker` log lines (deploy/README diagnostic
  corrected too); (3) `lmcache:request_cache_hit_rate` is a histogram, prom_dump now pulls
  `_sum`/`_count`; (4) DCGM is a DaemonSet and a Service port-forward pins to ONE pod - now one port-forward per exporter pod, poller takes multiple `--url`s (verified rows
  from both workers). Also: `helm upgrade` now pins `--version 0.1.11` (0.1.12 has schema
  drift), and a mounted dev overlay (`router-patch` ConfigMap - found live!) is
  auto-reverted before measuring. Driver, warm-up gate line, registry probe, prom_dump,
  and DCGM poller all exercised against the real cluster.

### Decided
- **Workload JSONLs are not committed; the manifest is** (issue #6): 6×~6 MB of synthetic
  filler would bloat the submission repo, and generation is deterministic - the committed
  `workloads/manifest.json` (config + SHA-256 per seed) plus a mandatory
  regenerate-and-verify in `run_cell.sh` gives the same frozen-dataset guarantee.
- **Registry probe skipped on the roundrobin cell** (issue #6): roundrobin routing ignores
  the KV registry, and the probe's pinning signal is meaningless there; the worker
  registration wait still applies. Documented in `benchmarks/README.md`.

## 2026-08-01 (night) - Ticket #16 completed: image pipeline pushes to Quay

### Fixed
- **Dockerfile was stale vs Change 2:** only `patches/lmcache` was copied into the image, so
  the "§6 deliverable" would have run stock kvaware routing. Now also copies
  `patches/vllm_router` (loadaware policy + CLI widening) and the CI verify step greps all
  three patched files for the `LOADAWARE PATCH` marker before pushing.

### Changed
- `deploy/values-loadaware-image.yaml`: `CHANGEME` → `quay.io/rhl193000/lmstack-router-loadaware`
  (public Quay repo, robot-account push from CI); `routingLogic: kvaware` → `loadaware` now
  that Change 2 is landed.

### Decided
- **Quay repo is public** (issue #16): cluster pulls with no imagePullSecret and a grader can
  pull the exact SHA-tagged image cited in the report. Credentials live only in GitHub Actions
  secrets (`QUAY_USERNAME`/`QUAY_TOKEN`, robot account scoped to this one repo).

### Added
- **Image path verified end to end on gapu-2** (closes #16): first CI push
  (`:c68ccfc`, digest `sha256:ae5772fe…`) deployed via
  `helm upgrade --reuse-values -f deploy/values-loadaware-image.yaml` (rev 13); router pod
  pulled the exact digest from Quay, booted `loadaware` (α=1.0, β=0.1 defaults), and after
  the expected #13 blind window the registry probe pinned 4/4 - prefix affinity confirmed on
  the built image, dev-loop overlay uninvolved. #7 unblocked.

## 2026-08-01 (evening) - Ticket #3 resolved: benchmark methodology locked

### Decided
- **Benchmark methodology (issue #3, full detail in its resolution comment):** one frozen
  Zipfian prefix workload (s=1.2, 20 prefixes × 2048 tok, OSL 64, committed JSONL); merged
  6-cell sweep (loadaware β ∈ {0, 0.1, 0.5, 1.0} + kvaware + roundrobin) × 6 seeds × 500
  requests, full engine-restart choreography per cell (#13); headline pre-registered as
  shipped β=0.1 vs kvaware on client-observed TTFT p95, one-sided paired Wilcoxon p<0.05 +
  bootstrap CI; mechanism metric = per-instance load CV.
- **Measured runs use built images only** (stock image for baselines, SHA-tagged Quay image
  for loadaware; overlay never measured) - settles #16's open question and makes #16 a hard
  blocker for #7 (blocked-by edge wired). No mock/simulation anywhere.
- **Metric plumbing verified live on gapu-2:** driver CSV is the only percentile-capable
  latency source (router exposes averages only; engine TTFT histogram is coarse and starts
  at the engine); Prometheus dump per run for load/queue/LMCache-hit metrics; DCGM polled
  directly via port-forward (stack Prometheus does not scrape it; no ServiceMonitor added).

### Added
- `docs/handoffs/claude-wayfinder-3-e533a3.md` (session log + metric verification) and
  `docs/handoffs/claude-wayfinder-3-e533a3-decisions.md` (workload exploration, D1-D4).

## 2026-08-01 (implementation) — Change 2 landed: `loadaware` placement (issue #5)

### Added
- **`loadaware` placement policy** in `patches/vllm_router/routers/routing_logic.py`:
  `LOADAWARE` enum value, a factory branch mirroring `KVAWARE`, and a `LoadAwareRouter`
  (subclass of `KvawareRouter`) that scores **every** endpoint by
  `α·(matched_tokens/prompt_tokens) − β·(in_prefill + in_decoding)` and routes to the argmax.
  The patch is **additions only** — `KvawareRouter` is byte-identical, so the baseline arm of
  the experiment is untouched. Registered in both `get_routing_logic()` and
  `cleanup_routing_logic()`.
- **α/β exposed as tunables** (§4 requires this): `LOADAWARE_ALPHA` / `LOADAWARE_BETA` env
  vars, defaults 1.0 / 0.1, overridable by kwargs. Documented in `patches/README.md`.
- **`--routing-logic loadaware` accepted by the CLI**
  (`patches/vllm_router/parsers/parser.py`): the flag's `choices` are hard-coded literals, not
  derived from `RoutingLogic`, so the enum value alone would have been rejected by argparse and
  the router would have exited before the factory ran. One-line widening; `apply-router-patch.sh`
  learned the `parser.py` target. An AST-based test asserts `choices` and `RoutingLogic` stay in
  lockstep in both directions.
- **38 more offline unit tests** (`tests/test_loadaware_routing.py`; suite now 50, still no
  cluster/GPU/install; suite now 57). `tests/conftest.py` grew a second loader that stubs the `vllm_router`,
  `requests`, `fastapi` and `uhashring` import surface and loads the tracked patch file itself.
  Covers the α/β crossover, ties, cold start, the fallbacks, and a regression test that
  `kvaware` still pins to the loaded cache holder.

### Fixed
- **The instance_id → URL bridge is refreshed when it goes stale, not once.** Review caught two
  silent failures the first cut had: a count-of-entries guard never notices a restarted engine
  (it registers under a *fresh* instance_id while the bridge only ever grows), so every holder
  would read as unmapped and placement would degenerate to least-loaded for the life of the
  router — an invalidated evaluation run with nothing in the logs. And when two ids share a URL,
  only the **live** one may be credited: the Controller's `kv_pool` keeps the dead instance's
  chunks until an explicit deregister, but the restarted engine came back with an empty cache, so
  that match is phantom. Evidence: `test_an_engine_restart_refreshes_the_bridge_instead_of_scoring_it_cold`,
  `test_a_dead_instance_id_earns_no_phantom_credit`.
  Known residual window, documented in the docstring: the bridge only learns the fresh id once
  the restarted engine appears in a `layout_info`, so until its first admit the dead id's match
  still reads as credit. Closing it means an unconditional Controller round-trip per request on a
  path that already blocks the event loop (production-stack#1016) — so the operational answer
  stands: gate runs on `registry-probe.sh`, do not restart engines mid-run. `kvaware` has the
  same hole and routes purely on that credit; §5 material.

### Decided
- **Cache-hit benefit is normalized to the fraction of the prompt cached**, not the raw
  matched-token count the handoff brief sketched. With raw counts the meaningful α:β ratio is
  ~1:1000 *and* shifts with prompt length, so one (α, β) pair would be a different policy for a
  500- and a 4000-token prompt — unusable for the §5 sweep. Normalized, `1/β` reads directly as
  "in-flight requests that cancel a full cache hit". Evidence:
  `test_benefit_is_normalized_so_the_weights_are_prompt_length_invariant`.
- **`loadaware` does not apply `kv_aware_threshold`.** Upstream needs that band because kvaware
  cannot weigh a small match against anything; the argmax can. Keeping it would also route
  every sub-threshold prompt by QPS in *both* arms, making that slice of the workload an
  identical no-op comparison. `kvaware` keeps the band (baseline unchanged). Evidence:
  `test_short_prompts_are_placed_not_dropped_to_the_qps_fallback`.
- **α/β travel by environment variable, not a CLI flag.** Registering a flag means the parser
  *and* `app.py` (which builds the `initialize_routing_logic` kwargs), i.e. one more file to
  mount and keep in sync than the one-line `choices` widening already forced. The factory still forwards `loadaware_alpha`/
  `loadaware_beta` kwargs, so adding a flag later touches no code here.
- **Not applied to the cluster in this session.** With issue #13 open (a router restart empties
  the KV registry and engines never re-admit), a live `loadaware` run would see `layout_info={}`
  and degenerate to the fallback — the ~7 min engine restart buys nothing until #13 lands.
  Offline tests + PR is the whole of #5.
## 2026-08-01 (§6 image path) — Ticket #16: build in CI, not on a laptop

### Added
- `Dockerfile` — the §6 reproducibility deliverable: the pinned stock router (**by digest**,
  not just tag — router and engines must carry the same lmcache minor or the controller
  protocol fails silently) plus a straight copy of `patches/`, which already mirrors the
  site-packages layout. No compilation, no CUDA, no weights.
- `.github/workflows/router-image.yml` — builds on every change to `patches/`/`Dockerfile`,
  **verifies the overlay actually landed** inside the built image (a silently-unpatched image
  would read as a failed experiment rather than a failed build), then pushes to Quay tagged
  by git short SHA. Without credentials configured it still builds and verifies, and skips
  only the push.
- `deploy/values-loadaware-image.yaml` — Helm overlay pointing `routerSpec` at the built
  image, layered on top of the baseline values.

### Decided
- **The image is built in CI, not locally.** The handoff assumed a local build, which needs a
  container runtime on a laptop (none installed) and a manual registry login. CI has Docker,
  keeps the credential in repo secrets, and — the part that earns the 30% — means a grader
  reproduces the image by pushing a commit rather than trusting an artifact one person built
  by hand.
- **Tag by commit SHA, never `latest`.** A floating tag makes the router/engine lmcache
  pairing impossible to audit after the fact; the report cites the exact tag its numbers
  came from.
- **Credentials are a Quay robot-account token in GitHub Actions secrets**, never a user
  password, and the image repository should be public so the cluster needs no pull secret.

### Not done — needs a repo admin
Quay robot account + `QUAY_USERNAME`/`QUAY_TOKEN` secrets + `QUAY_IMAGE` variable, then a
first push and a pull test from the cluster. The build path is otherwise complete and is
exercised by CI on every push.

## 2026-08-01 (investigation) — Ticket #13 resolved: the KV registry's blind window

### Found — three facts that compose into a silent measurement trap
- The Controller's `kv_pool` is **in-memory**: a router restart empties it.
- Admission is **one-shot per chunk** — `LocalCPUBackend.submit_put_task` returns early on
  `if key in self.hot_cache`, so a chunk is announced exactly once, at first store. Nothing
  already cached is ever re-announced. (`RegisterMsg`, `HeartbeatMsg` and `KVAdmitMsg` all
  share one PUSH socket, so this is not a dead-socket problem — re-registration recovers
  because heartbeats *repeat*; admission never does.)
- Admits are lost for **~40 s** after a router restart, until both workers re-register
  (10 s heartbeat delay + 30 s interval).
- **Composed:** a prefix first stored inside that window is invisible to the Controller for
  the life of the engine process, while the engine still serves it from its own cache
  perfectly. Both arms of an experiment then degrade to QPS routing and look identical for
  the wrong reason.

### Measured (fresh prefix per probe, from rollout completion)
```
t+4s … t+30s   requests spread 2/2   → registry empty
t+42s          requests pinned 4/4   → registry live (both workers re-registered)
```

### Added
- `deploy/dev/registry-probe.sh` — no-patch health check for the registry. Sends the same
  >2000-token prefix N times: `kvaware` pins all N to one Instance when the registry is
  populated and spreads them when it is empty. Exit 0/1, so it gates a run.

### Decided
- **Every measurement is gated on `registry-probe.sh` with an unused seed**, run after each
  `apply-router-patch.sh` and each `revert-router-patch.sh`, before warm-up. Poisoned
  prefixes are never reused.
- **No engine restart is required** — this walks back the same-day claim that it was. Waiting
  ~40 s for re-registration is enough, so the dev loop stays ~60 s + the probe.
- **Upstream:** the principled fix is for a worker to re-announce its `hot_cache` on
  (re-)registration, making admission self-healing like registration. That is LMCache
  `v1/cache_controller` code, which is being deprecated (LMCache#4025) and is off our PR
  target — so it belongs as an upstream **issue**, per the two-PRs-only rule in #10.

## 2026-08-01 (implementation) — Change 1 landed: multi-instance lookup (issue #4)

### Added
- **`patches/` — tracked copies of the router-image Python files we modify**, mirroring their
  path under `/opt/venv/lib/python3.12/site-packages/`. The dev loop and the future §6 image
  apply the *same* bytes. Conventions in `patches/README.md`.
- **Multi-instance lookup** in `patches/lmcache/v1/cache_controller/controllers/kv_controller.py`:
  `lookup()` now credits **every** instance holding each chunk instead of `kv_pool[key][0]`,
  so `layout_info` reports per-instance matched-token counts. Wire-compatible — `LookupRetMsg`
  was already `{instance_id: (location, matched_tokens)}`.
- **`tests/` — 18 offline unit tests** (`pytest tests/`, no cluster/GPU/lmcache install).
  `tests/conftest.py` stubs the `lmcache` import surface and loads the *tracked patch file
  itself* by path, so the bytes under test are the bytes that get mounted. Includes a verbatim
  reference implementation of the stock lookup for the regression assertions.

### Decided
- **Prefix credit is contiguous per instance.** An instance stops earning matched tokens at its
  first missing chunk even if it holds later ones — a cache match is a prefix match, so tokens
  after a hole are unusable. The upstream global `break` is subsumed: the walk ends when no
  instance is still contiguous. Evidence: `tests/test_kv_controller_lookup.py`
  (`test_gap_stops_credit_at_the_gap_not_after_it`). This is a real design decision and belongs
  in §5 of the report.
- **`kvaware` is *not* behaviourally invariant under this patch**, even though
  `routing_logic.py` is untouched. The *instance* it selects is unchanged — both
  implementations insert `kv_pool[key0][0]` first and Python keeps a key's original position
  on re-assignment — but that instance's **`matched_tokens` can grow**, because an instance is
  now credited on every chunk it holds rather than only on chunks where it happens to be `[0]`.
  kvaware bands `matched_tokens` against `kv_aware_threshold` (`routing_logic.py:354-369`) to
  choose the cache path over the QPS fallback, so a larger count can flip that branch.
  **The baseline arm must be measured with `revert-router-patch.sh` applied**, never with
  Change 1 mounted. Evidence: `test_selected_instance_is_unchanged_even_with_several_holders`
  and `test_matched_tokens_of_the_selected_instance_can_grow`.

### Fixed
- **`deploy/dev/apply-router-patch.sh` was unrunnable on macOS** — `declare -A` needs bash 4 and
  macOS ships bash 3.2, which mis-parsed the subscripts as arithmetic and killed the script.
  Replaced the associative array with a `patch_target()` `case`.

### Verified live (cluster `gapu-2`, patch mounted, both engines)
- `[LOADAWARE] lookup matched 2 instance(s): {'…-pm79x': ('LocalCPUBackend', 5691),
  '…-x9dkx': ('LocalCPUBackend', 5691)}` — two instances reported for one prefix, which stock
  lookup structurally cannot do. Recipe: two *concurrent* cold requests on a fresh >2000-token
  prefix split across both engines (QPS fallback), then a third request to observe the lookup.

### Found — blocks evaluation (issue #13, since resolved — see the entry above)
- **A router restart leaves the Controller's `kv_pool` empty and every `lookup()` returning
  `{}`**, with nothing in the logs saying so. Cost most of this session's live-verification
  time. Investigated and characterised in #13.

## 2026-08-01 (docs consolidation) - Ticket #12 resolved

### Changed
- Deleted superseded exploration docs: `caching-landscape.md`, `router-optimization-ideas.md`,
  `handoff-second-optimization.md` (git history keeps them). Remaining five docs carry
  `status: live | frozen` headers; `handoff-core-implementation.md` header-only (in use by #4).

### Decided
- **Docs discipline (issue #12)**: decisions land in tickets + CHANGELOG only; `docs/` holds
  artifacts, never rationale; `docs/decisions/` closed to new entries. Recorded in CLAUDE.md.

## 2026-08-01 (later still) - Ticket #2 resolved: repo cleanup + CI skeleton

### Added
- Root `README.md` (project summary, repo layout, setup/test instructions),
  `requirements.txt` (httpx + pytest), and `.github/workflows/ci.yml` running
  `pytest benchmarks/` on every push and PR.

### Changed
- Pruned for the submission repo: personal hyperresearch skills moved out of
  `.claude/skills/` (now user-level on Ben's machine), course lecture PDFs and the
  scheduling docx removed from `docs/references/` (git history keeps them). Only the
  authoritative `Final Project Guidelines.pdf` remains.

## 2026-08-01 (later) - Wayfinder map: e2e plan on GitHub Issues

### Decided
- **The e2e plan now lives on GitHub Issues** - map issue #1 (label `wayfinder:map`),
  9 sub-issue tickets (#2-#10) with native blocked-by edges. Frontier (start now, in
  parallel): #2 repo cleanup + CI, #3 benchmark methodology, #4 Change 1 lookup.
- **All four 2026-08-01 reconciliation items settled (Ben, at map charting; recorded in
  issue #1 "Decisions so far"):**
  1. Upstream PR target = production-stack, not LMCache v1 (newer re-verification
     evidence wins; LMCache#4025 deprecation).
  2. PR breadth = two PRs only (#10); extra fixes become upstream *issues*, not PRs.
  3. The map is the final implementation plan - coding starts immediately off
     `docs/handoff-core-implementation.md`, no further planning session.
  4. Second optimization = nothing; kvaware fast path (F) only becomes a ticket if
     evaluation is running by day 5.
- **Benchmark plan remains deliberately undecided** - now ticket #3 (grilling), day-3
  latest; it gates evaluation (#7) via the harness (#6).

## 2026-08-01 (end of session) — Deadline known: scope cut to core-only

### Decided
- **Submission deadline is ~2026-08-10** (9 days, stated by Eliad 2026-08-01).
- **Adaptive β is OUT; flipped to runner-up E (core-only + evaluation + upstream PRs).**
  This executes the flip condition `docs/decisions/second-optimization.md` pre-registered —
  "core not landed with most of the schedule still ahead" — which triggered because the
  project was idle 2026-07-05 → 08-01 and no implementation code exists on day 1 of 9.
  Rubric backs it: correctness 40 + reproducibility 30 = 70% vs performance gain 15%.
  What survives of adaptive β is the α/β sensitivity sweep §5 requires anyway.
- Remaining scope, in order: multi-instance lookup → `loadaware` (static α/β) → benchmark
  harness → evaluation → report. Upstream PRs are opportunistic only.

### Changed
- `docs/decisions/second-optimization.md` marked SUPERSEDED with the trigger recorded; its
  load-signal-staleness analysis stays load-bearing for the report's motivation.
- `docs/handoff-core-implementation.md` — added the deadline, a day-by-day schedule with no
  slack, a cut-scope trigger for day 3, and a hardened definition of done.

## 2026-08-01 (end of session) — Implementation handoff

### Added
- `docs/handoff-core-implementation.md` — self-contained brief for the next session to start
  writing the core feature: exact file paths, line numbers and verbatim current code for both
  changes (read live out of the running router pod, not from upstream HEAD), the dev loop, the
  available routing signals, the offline test plan, and the measurement trap in §5.

### Next session starts here
Implement Change 1 (multi-instance `lookup()`), observe two instances in a single
`layout_info`, keep `kvaware` byte-identical as the baseline arm.

## 2026-08-01 (later) — Dev loop solved; two doc corrections

### Added
- `deploy/dev/` — in-cluster dev loop for router/LMCache code: ConfigMap + `subPath`
  overlay onto the running router pod, no image build and no container runtime required.
  `apply-router-patch.sh` / `revert-router-patch.sh` + README. **Validated end-to-end**:
  a marked-up `routing_logic.py` was mounted, confirmed live (log attributed to
  `routing_logic.py:393`), then cleanly reverted to stock.

### Fixed
- **`deploy/README.md` gotcha #1 was wrong** — "router restart ⇒ engine restart" does not
  hold in our configuration. The controller re-registers unknown workers on heartbeat
  (`registration_controller.py:176-192`), gated on `lmcache_worker_heartbeat_time > 0`,
  which `values-baseline-kvaware.yaml:58` sets to 30. Verified live: router-only restart,
  both workers back in ~30 s, engines untouched. **Dev loop is ~60 s, not ~25 min.**
- **`docs/upstream-findings.md` Finding 1 corrected** — "workers never re-register" is
  false; do not file it. Reframed to the real defect: `workerHeartbeatTime` is not a chart
  default, so stock deployments silently degrade kvaware on router restart. That is a
  one-line chart fix and a much easier merge.

### Changed
- **Lookup extension is far smaller than the July design assumed.** In the deployed
  lmcache 0.3.9post2, `kv_controller.lookup()` already returns
  `layout_info: Dict[instance_id → (location, matched_tokens)]` — the wire format already
  expresses per-instance match info. The defect is a single `[0]`: `self.kv_pool[key][0]`
  credits only the *first* holder of each chunk, discarding the rest. So the change is
  ~10 lines with **no protocol/message-schema change**, and the router-side counterpart is
  replacing `list(layout_info.keys())[0]` (`routing_logic.py:349`) with the α/β argmax.
- Recorded design consequence: under pure `kvaware` a prefix is rarely held by more than
  one instance, so the lookup fix is a near-no-op in isolation — it only pays once routing
  spreads requests. Rungs 2 and 3 of the ablation ladder are co-dependent and the workload
  must be designed so replication actually occurs. First live evidence:
  `layout_info={'…cc926': ('LocalCPUBackend', 2048)}` — one holder, 2048 matched tokens.

## 2026-08-01 — Restart after 4-week pause; upstream re-verification

### Decided
- **Sequencing: core first, second optimization deferred.** Build and validate the core
  (multi-instance lookup extension + `loadaware` static β) before committing to adaptive β.
  Adaptive β remains the intended rung 4 but is no longer a precondition for a complete
  project — it collapses to the α/β sensitivity sweep §5 requires anyway (Eliad).
- **Upstream-PR target moves off LMCache v1 `cache_controller`.** LMCache Q3 roadmap
  (LMCache#4025, opened 2026-07-06) begins deprecating non-MP mode this quarter, so a
  lookup-extension PR into v1 is unlikely to be accepted. The PR portfolio retargets to
  production-stack (router Service ports 9001/9002, one-shot registration, image matrix)
  plus production-stack#1016. Evidence: upstream re-verification, this session.
- **Contribution reframed:** the novelty is the *placement policy*, not the lookup itself —
  LMCache#4275 (merged 2026-07-28) added a fleet-wide key directory with per-instance
  placement lists in the new `mp_coordinator`. Still novel for the path production-stack
  actually uses; cite #4275/#4226 as concurrent related work.

### Changed
- Report must cite the **configured** `--engine-stats-interval 5` (deployed value), not the
  15 s chart / 30 s CLI defaults, when arguing load-signal staleness for adaptive β.

### Fixed
- Baseline restored: engines were externally scaled to 0 for ~11 days and one A10 was held
  by another namespace. Both GPUs reclaimed, `stack-llm-deployment-vllm` back to 2 replicas,
  both workers re-registered with the controller, end-to-end completion verified via the
  public Route (needs `curl -k` — self-signed cert in the ingress chain).

### Verified (upstream, still true at 2026-08-01)
- LMCache v1 `lookup()` is still first-instance-only (`kv_controller.py:380-387` TODO;
  `utils.py:580` `find_kv()` returns on first hit) and **unclaimed by any PR**;
  production-stack's `KvawareRouter` still takes `[0]` from `layout_info`.
- No `loadaware`/priority/hybrid strategy in production-stack's `RoutingLogic`;
  router-queuing PRs #876/#905 still open, nothing merged.
- Pinned image tags `lmstack-router:0.1.9.dev9-g37bafbcf5.d20260107` and
  `vllm-openai:v0.3.9post2` both still active on Docker Hub.
- Correction to the 2026-07-05 memo: the `:402` TODO is on `batched_p2p_lookup`; the one
  that matters is the block at lines 380-387 above `async def lookup()`.
## 2026-08-01 (evening, parallel planning session) — Scope lock: PRs and load signal

### Decided
- **Upstream-PR scope narrowed to the two the project requires** (Ben): LMCache
  per-instance lookup (core dependency, filed early) and production-stack loadaware
  router (filed after benchmarks). Ports fix stays a documented deploy workaround;
  re-registration stays a gotcha note. No drive-by fixes/PRs.
- **Core locked with G folded in** (Ben): loadaware score's load term is a tunable
  signal — `count` (in-flight requests, default/baseline) vs `work-left` (estimated
  remaining tokens from existing RequestStats; the G idea) — compared as one ablation
  rung. F dropped (partially fixed upstream by #1025; residual value not worth a rung).
  Ladder: kvaware → +per-instance lookup → +load(count) → +work-left → +adaptive β.

## 2026-08-01 (later still) — FullLookup overlap verified

### Decided
- **Lookup extension proceeds unchanged; cite + build on LMCache #1420.** "FullLookup"
  (= LMCache PR #1420, feeding production-stack #670) is functionally the same
  per-instance lookup capability we're building, but was auto-closed stale 2025-12-25,
  unmerged, no design objections. Verified at LMCache HEAD `0427938a`: no FullLookup in
  code/history, `kv_controller.py` identical to our pin, multi-results TODO still open.
  Our PR = revive the capability with the benchmarks both dead attempts (#1420, #884)
  lacked. Evidence: `docs/router-optimization-ideas.md` (FullLookup section).

## 2026-08-01 (later) — Prior-art check on the core idea + F correction

### Added
- Prior-art section in `docs/router-optimization-ideas.md`: core blended-score idea still
  unclaimed upstream, but warm — #884 (switch-based load+kvaware combo, died 2026-06 for
  lack of benchmarks; citable prior art), #852 (least-QPS only, stalled), #670 (TTFT
  routing draft, dormant, closest to idea G; uses LMCache "FullLookup" — **verify overlap
  with our lookup extension**). Urgency reinforced: file the lookup-extension PR early.

### Changed
- Idea F corrected: upstream #1016/#1025 (2026-07-29) already fix the event-loop-blocking
  half (thread offload only, explicitly no tokenization caching) — F narrows to
  prefix-cached tokenization + the overhead benchmark; upstream angle = extend #1025.

## 2026-08-01 — Fresh optimization-idea survey of production-stack `main`

### Added
- `docs/router-optimization-ideas.md` — survey of `main` @ `3314ee6` for second-optimization
  candidates beyond the 2026-07-05 menu: F fast-path/tokenization (blocking event-loop work
  in `KvawareRouter`, strongest new find), G work-left load signal (roadmap P2 "predictive
  routing"), H tier-aware benefit discount (gated on a ½-day spike), I queuing policy
  (Discussion/RFC-comment only), plus a prefixaware micro-PR for the upstream track.

### Decided
- **Adaptive β stays the second optimization** — upstream barely moved since the pin
  (18 commits, no routing-logic changes; #876/#905 still open with the locality-vs-fairness
  question still deferred), so the 2026-07-05 memo's evidence holds. New ideas slot in as:
  G folded into the load-signal definition, F as low-risk third rung / first upstream PR.
  Evidence: `docs/router-optimization-ideas.md`.

## 2026-07-05 (later) — Second-optimization deep-dive

### Decided
- **Second optimization = adaptive β** (feedback-controlled load weight, pluggable
  `BetaPolicy`, driven by the router's fresh event-driven `RequestStats` instead of the
  15–30 s-stale scraped `EngineStats`). Runners-up with flip conditions: B (pre-warm,
  gated on NIXL spike + spare days) and E (core-only). Full rationale + survey evidence:
  `docs/decisions/second-optimization.md` (Claude session, per handoff brief).
- NIXL spike **not** run: A/B eliminated on criteria 1–3 regardless of outcome; but the
  key static unknown was settled live — `nixl` 0.7.1 IS importable in the running
  `v0.3.9post2` engine pods, so B's flip condition is realistic.

### Added
- `docs/decisions/second-optimization.md` — decision memo (also records: lookup TODO
  still open at `bf20f51`; LMCache migrating to MP mode → file the lookup PR early;
  upstream #876/#905 confirm the load-signal-freshness gap).

## 2026-07-05 — Requirements grounding session (grilling)

### Decided
- **Core contribution locked:** `loadaware` placement policy (α·cache_benefit − β·load) +
  the multi-instance Lookup Extension it requires; ships router-image-only (the controller
  runs in the router pod — engine images stay official/pinned) (Eliad).
- **Framing rule** for §2/report: "KV-cache-aware request placement", never headline
  "load balancing"; hit rate stays a first-class metric (Eliad).
- **Second optimization parked** — deep-dive delegated to a dedicated session;
  self-contained brief with candidate menu, decision criteria, and NIXL-spike gate in
  `docs/handoff-second-optimization.md` (Eliad).
- Upstream-PR portfolio (service ports, one-shot registration, image matrix) is an
  always-on parallel track, independent of the second-optimization choice (Eliad).

### Added
- `CONTEXT.md` — project glossary (ubiquitous language: Instance, Controller, Placement
  Policy, Lookup Extension, Replication Mechanism vs Policy, Affinity Probe, …) (Eliad).
- `docs/handoff-second-optimization.md` — handoff brief for the second-optimization
  deep-dive (Eliad).

## 2026-07-04 (later) — Baseline VALIDATED end-to-end

### Added (post-validation polish)
- **External Routes — no port-forward needed** (VPN required, documented in `deploy/README.md`):
  model API `https://llm-cache-llm.apps.gapu-2.customers.k8s.co.il/v1` (OpenAI-compatible,
  no key), Grafana `https://grafana-cache-llm.apps.gapu-2.customers.k8s.co.il`
  (admin / cache-llm, password set in values — fine for VPN-only cluster) (Eliad).

### Fixed (post-validation polish)
- LMCache/vLLM dashboards showed "datasource not found": the shipped dashboard JSONs
  hardcode datasource uid `prometheus`; pinned our provisioned datasource to that uid in
  values. Verified: dashboards load, all 3 Prometheus targets up (Eliad).

### Fixed
- **Found the working official image pairing:** router `lmstack-router:0.1.9.dev9-g37bafbcf5.d20260107`
  + engines `vllm-openai:v0.3.9post2` — both lmcache 0.3.9post2 (pin-history archaeology:
  production-stack pinned 0.3.9post2 until 2026-01-14; no official engine image carries the
  0.3.11 that `:latest` needs). Older router = single controller socket on 9000, so the chart's
  stock Service suffices; reply/heartbeat flags removed from routerSpec (Eliad).

### Added
- `docs/upstream-findings.md` — the four control-plane findings written up for the final
  report + upstream issues/PRs: one-shot worker registration (router restart silently kills
  kvaware), chart Service missing controller ports, the image-pairing incompatibility matrix
  (incl. broken official tutorial), and the silent-failure design critique; includes the
  reusable affinity-probe snippet (Eliad).
- **Prefix-affinity validation PASSED:** both workers registered
  (`Registered instance-worker` ×2 in router logs); 3 requests sharing a 1200-token prefix —
  first falls back (cold), second and third log `found by kvaware router` and land on the same
  instance, latency 0.83s → 0.39s from the KV hit. Ecosystem fully operational: 2×A10 engines,
  kvaware router, Grafana dashboards, Prometheus, benchmark harness. Next: agree on the
  optimization design (Eliad).

## 2026-07-04 — Direction pivot, deep-research verdict, baseline decision, first benchmark code

### Decided
- **No modifications to any production-stack component until the optimization design is agreed**
  (Eliad, during deployment session). Official images only; the in-progress custom router-image
  build (needed to align lmcache versions) was cancelled and its BuildConfig removed. Consequence:
  baseline runs on `lmstack-router:latest` (lmcache 0.3.11) + `vllm-openai:v0.3.9post2`
  (lmcache 0.3.9.post2) — the closest official pairing; kvaware registration with this pairing
  is being validated now that the service-port fix is in.
- Router-image builds, when we get to them, are additionally blocked by a cluster-level
  QuayIntegration admission webhook (defunct Quay install never provisioned the builder SA in
  `cache-llm`). Unblock options recorded in the session log; needs a cluster-scoped denylist
  patch or local container tooling.
- **Pivot from the May direction (fork SGLang + GDSF eviction) to KV-cache infrastructure.**
  A full-tier deep-research run (~180 sources, 6 depth investigations, adversarial review)
  compared KV offload/onload vs. KV-aware routing head-to-head for our rubric and hardware.
  Verdict: fork **vLLM Production Stack** and build a combined cache-hit + load weighted routing
  policy against its still-primitive shipped routers. Report:
  `research/notes/final_report_kv-offload-vs-routing.md` (local, gitignored) — key findings:
  the honest-baseline inversion (production-stack is the one repo whose default router is not
  yet hybrid), the A10 offload roofline (naive offload is PCIe-bound; only a κ-aware admission
  policy is defensible), and the N=2 statistical-benchmark gap in the literature.
- **Baseline = vLLM Production Stack + LMCache** with a `loadaware` routing strategy; design in
  `docs/project-brief.md`, code-level feasibility with file/line references in
  `docs/feasibility-verification.md` (Eliad).
- Named alternatives with flip conditions (report §6): Dynamo `lib/kv-router` bandwidth-calibrated
  tier discount; κ-aware offload admission; vllm-project/router RFC #51 (Rust prestige lane).

### Added
- `benchmarks/` — seedable Zipfian prefix workload generator + unit tests, async load driver
  emitting TTFT/E2E/token metrics to CSV (Eliad).
- `deploy/` — OpenShift configs fit to cluster reality: RWX CephFS model PVC, burstable memory,
  `values-baseline-kvaware.yaml` (Eliad).
- Observability on the cluster: Grafana (via kube-prometheus-stack subchart, operator/CR
  disabled) + plain single-pod Prometheus (`deploy/prometheus.yaml`, 5s pod-SD scrape of
  engines + router) with the shipped vLLM/LMCache dashboards preloaded (Eliad).
- Stack deployed to `cache-llm` on gapu-2: router + 2× Qwen2.5-3B replicas, one per A10 (Eliad).

### Fixed (deployment debugging — all documented in `deploy/README.md` gotchas)
- **Upstream chart bug (PR candidate):** router Service omits LMCache controller ports
  9001/9002 → worker registration hangs silently → kvaware degrades to QPS routing.
  Patched the Service; diagnose via router `/metrics` `registered_workers_count` (Eliad).
- **lmcache version skew router↔engine breaks the controller↔worker ZMQ protocol silently.**
  Tried `vllm-openai:v0.5.1rc2` — its lmcache is 0.5.1rc2, a *major* jump past the router's
  0.3.11 (register messages arrive post-port-fix but fail to decode; `reply_socket_message_count`
  grows while `registered_workers_count` stays 0). Reverted to `v0.3.9post2`, the closest
  official engine release; first fair compatibility test with ports fixed is in progress (Eliad).
- OpenShift arbitrary-UID crash (`HOME=/` unwritable → flashinfer dies): `HOME=/tmp`;
  GPU rolling-update deadlock: `strategy: Recreate`; router startup-probe kill during
  ~20s kvaware init: relaxed threshold; Grafana default-datasource collision:
  `defaultDatasourceEnabled: false` (Eliad).
- `docs/project-brief.md`, `docs/feasibility-verification.md` (Eliad).
- `CHANGELOG.md` (this file) + changelog discipline in `CLAUDE.md`.
- NotebookLM notebooks for grounded Q&A over the research corpora: `d5a7565e` (offload-vs-routing
  report + 272 sources), `e4f7c11c` (May baseline-selection corpus, 132 sources).

### Changed
- `CLAUDE.md` slimmed for collaboration; machine-specific tooling moved to each collaborator's
  gitignored `CLAUDE.local.md`; recorded the baseline decision.
- Prior research run's pipeline artifacts archived to `research/runs/llm-cache-baseline/` (local).

### Fixed
- Cleaned up 9 duplicate NotebookLM notebooks created by an auto-push hook bug (dedup key
  included file hash, so every report edit re-pushed); hook fix tracked separately in the
  claude-config repo.

## 2026-07-03 — Repository bootstrap

### Added
- Initial commit: project docs (`docs/references/` course PDFs, `docs/caching-landscape.md`),
  `CLAUDE.md`, research skill files, `.gitignore` (excludes `research/` working data and
  `.hyperresearch/`).
- Private GitHub repo `BenEpstein/caching-in-llms`; Eliad added as collaborator.

### Decided
- (Superseded 2026-07-04) May deep-research verdict: fork SGLang RadixAttention + cost-aware
  GDSF eviction policy. Report: `research/notes/final_report_llm-cache-baseline.md` (local).

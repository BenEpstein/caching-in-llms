# Changelog

All notable changes to this project are documented here, newest first.
Format adapted from [Keep a Changelog](https://keepachangelog.com/en/1.1.0/): one entry per
work session (or significant commit), with **Added / Changed / Decided / Fixed** subsections as
applicable. Since this is a research project, **Decided** captures project-direction decisions
with a pointer to the evidence — those matter as much as code.

## [Unreleased]

## 2026-08-07 - test-suite audit (#60) and upstream-PR re-scope (#10)

### Changed
- **Test suite 196 → 190** (#60). Deleted four redundant tests: `test_classification_is_pure_and_repeatable`
  (3 params, asserted `f(x) == f(x)` on a pure function), `test_substring_test_would_have_failed_here`
  (asserted a property of its own fixture string, plus a repeat of the test above it),
  `test_loadaware_is_selectable_from_the_command_line` (implied by the set-equality test beside it),
  and `test_list_position_equals_seed_number_after_the_fix` (same fixture and assertion as
  `test_read_run_orders_numerically_not_lexicographically`, whose docstring absorbed it).
  No coverage lost. `pytest benchmarks/ tests/ -q` and `scripts/reproduce.sh` both green.

### Decided
- **The benchmark suite is correctly sized; #60's premise does not hold.** All 11 files were read
  test by test. Almost every test names a specific defect this project shipped, so only 6 of 196
  were removable. `tests/` (66) serves §4's "unit tests covering your new policy" and scores
  Correctness (40%); `benchmarks/` (130) tests the measurement harness and serves §3 + §5,
  scoring Reproducibility (30%) and protecting Performance Gain (15%). Evidence: the ticket's own
  three delete leads were each checked and none held - no tests reference the dead driver flags,
  the three `job=vllm-engines` parses are still three separate implementations (they only become
  duplicates once #55 consolidates them), and no test is pinned to a `results/` run dir.
- **Upstream PR 2 is re-scoped from the Lookup Extension to the `loadaware` Placement Policy** (#10).
  The prior deferral stands: [LMCache#4025](https://github.com/LMCache/LMCache/issues/4025) marks
  v1 `cache_controller` for non-MP deprecation, so `tests/v1/cache_controller/` is a dying target.
  `src/vllm_router/routers/routing_logic.py` is alive upstream (six `RoutingLogic` members), its CI
  is `ubuntu-latest` + `uv run pytest` with no GPU, and `src/tests/test_prefixaware_router.py` is a
  ready template. #10 now carries a 7-step DoD that simulates their CI locally before opening the PR.
- **Upstream test conventions stay out of this repo.** Our `tests/conftest.py` stubs the `lmcache`
  and `vllm_router` import surface so the suite runs with no cluster and no GPU; upstream CI has the
  real packages. Porting to their layout is work for the fork, at PR time. #10 is opportunistic and
  gates no graded deliverable, so rewriting our suite for it would spend 70% of the grade to chase 0%.

## 2026-08-06 (goodput promoted) - `ttft_slo_miss` is a reported secondary, not exploratory

### Decided
- **Goodput (`ttft_slo_miss`) is no longer labelled exploratory** (Ben, 2026-08-06). It is a
  secondary metric of the #31 confirmatory sweep, reported alongside the two co-primaries.
  Rationale: it is computed from the same committed driver CSVs, over the same five cells, as
  every other statistic in the sweep - no new data, no new run, and any objective is
  recomputable from the repository. The objective is **swept, not pinned**: `fig12` draws
  50-400 ms and the arms separate across the whole range, so the 150 ms default is a reporting
  choice rather than a load-bearing threshold. Supporting evidence: the effect is a plateau
  (~7 points, 120-250 ms) rather than a peak; the `b0` ablation runs **negative** across the
  same range, which no measurement artefact produces; and `b0.5` ran 20 min *after* `kvaware`,
  so window drift biases this pair conservatively. Superseded the previous framing recorded
  under "Goodput on this sweep is EXPLORATORY" (2026-08-06, earlier entry).
- **What did not change: the pre-registered verdicts.** TTFT p95 remains the null (p=0.1153)
  and imbalance remains the pass (p=1e-5, −48.1%). Goodput is reported *beside* them, not in
  place of the null. `results/expected/stats.txt` regenerates every figure byte-identical -
  only caveat prose moved.

### Changed
- **The "EXPLORATORY" stamp removed from the six places it was emitted**, so the runtime output
  and the written result agree: `analyze.py` (module docstring, `TTFT_SLO_S` comment, and the
  line `compare` prints beside the p-value), `plot_results.py` (fig12 title + layout table),
  `run_sweep.sh` (closing banner), `benchmarks/README.md` (§ heading + body),
  `test_analyze.py` (section comment). `compare` now prints the objective and points at the
  sweep instead. Two of those printed at runtime, so leaving them would have made
  `reproduce.sh` contradict the report.
- **`scripts/reproduce.sh` greps `SLO [0-9]+ ms` instead of `EXPLORATORY`.** The objective must
  stay welded to the p-value in the committed baseline; only the string it matches changed.
  `results/expected/stats.txt` regenerated - **all statistics identical**, caveat text only.
- Verified: `pytest benchmarks/` 130 passed, `scripts/reproduce.sh` 5/5, `figure-data.json`
  diff-identical to the committed baseline.

### Fixed
- **Threshold-sensitivity was asserted but never tabulated.** `fig12` plots the mean goodput
  gap across the sweep; it does not show whether the *paired* test holds off the default. It
  does: `b0.5` vs `kvaware` is significant at α=0.025 at 100/124/150/175/400/500 ms
  (p=0.0001 … 0.0215) and marginal in the 200-300 ms band (p=0.035-0.057), positive at every
  threshold, never significant the wrong way, 13-18 of 20 seeds better throughout. `b0` is
  negative at every threshold. Recomputable from committed CSVs via `compare --slo`.
## 2026-08-06 (fig12) - roundrobin joins the goodput figure (#31)

### Added
- **`roundrobin` on `fig12-goodput.png`** as a fourth line, and its curve in
  `results/expected/figure-data.json` so `reproduce.sh` checks it. 7.6% of requests under
  150 ms against `kvaware` 42.0% and `loadaware-b0.5` 49.4%.
- `plot_results.py --comparator <run-dir>` and `COMPARATOR` in `reproduce.sh`.

### Decided
- **Goodput is the one figure a SATURATED arm belongs on, and p95 is not.** Goodput's
  denominator is requests *sent*: every arm was offered the identical 500-request frozen
  workload at the same Poisson rate, so `roundrobin` delivering 10.31 of 16 req/s simply
  counts as missed. A p95 contrast instead conditions on the requests that finished, which
  flatters the arm that fell behind. The figure caption states the 10.3-of-16 delivery and
  the ~15 s needed to reach 99%, so the curve cannot be read as a latency result.
- **The framing cell is passed via `--comparator`, never as a positional run.** In `cells` it
  reaches every figure, and its 11 s p95 compresses fig1 - the centerpiece - until the whole
  beta curve is a flat line on the floor. Measured, not hypothetical: that is what the first
  attempt produced. Scoped to fig12, the other eleven figures regenerate byte-identical.

## 2026-08-06 (roundrobin) - the third comparator, at n=20 (#31)

### Added
- **`roundrobin` at n=20, rate 16** (`results/20260806-144135-roundrobin`), replacing the
  n=3 probe. Same frozen workload manifest, same rate and OSL as the confirmatory sweep.
  Valid: pooled error 0.08%, `utilization_coverage` 1.000 on all ten series.

| arm | n | achieved | TTFT p50 | TTFT p95 | ITL p95 | Load Imbalance |
|---|---|---|---|---|---|---|
| `kvaware` | 20 | 14.32 | 0.173 | 0.334 | 157.4 ms | 2.448 |
| `loadaware-b0.5` | 20 | 14.35 | 0.152 | 0.290 | 136.2 ms | 1.269 |
| `loadaware-b0` | 20 | 14.12 | 0.182 | 0.378 | 183.9 ms | 2.956 |
| `roundrobin` | 20 | **10.31** | 1.417 | 11.161 | 911.3 ms | 1.678 |

### Decided
- **Report roundrobin as a CAPACITY result, not a latency one.** It sustains 10.31 of 16
  offered req/s while both cache-aware policies sustain 14.3, so its latency numbers are
  measured past its own knee and are not a like-for-like comparison at equal delivered load.
  The defensible statement is that cache-blind placement cannot carry the workload; a paired
  p95 contrast against a saturated arm would not be one. It also runs in a separate window
  from the confirmatory sweep, which the capacity statement is robust to and a latency
  contrast would not be.
- **Round-robin equalizes request COUNTS, not load.** Its Load Imbalance is 1.678, better
  than `kvaware`'s 2.448 but worse than `loadaware-b0.5`'s 1.269, because a cache miss costs
  far more in-flight residency than a hit. Descriptive, from one cell, not a tested claim.

## 2026-08-06 (latest) - goodput lands in the pipeline (#31)

### Added
- **`ttft_slo_miss`, a goodput metric wired end to end**: `analyze.py compare --metric
  ttft_slo_miss [--slo]`, `seed_stats`, `fig12-goodput.png`, `reproduce.sh`, unit tests and a
  §4-style tunable (`analyze.TTFT_SLO_S`, default 0.150 s, documented in
  `benchmarks/README.md`). The tested quantity is the MISS rate, not goodput, so it runs
  through the *same committed* Wilcoxon and relative-reduction bootstrap as `ttft_p95` with no
  inverted test and no new statistics; figures plot the complement.
- **On the confirmatory sweep** (`b0.5` vs `kvaware`, n=20): −19.0% missed requests at the
  150 ms objective, CI [10.7%, 22.1%], p=0.0021; −12.4%, p=0.0004 at 125 ms. The `b0`
  ablation is null and slightly negative (−3.6%, p=0.8058), which is consistent (exploratory,
  not a tested claim) with the gain tracking the load term rather than the run window.

### Decided
- **Goodput on this sweep is EXPLORATORY and is labelled so in the code, not only in a doc.**
  It was first computed *after* the pre-registered `ttft_p95` test returned null on the same
  data, and `compare` prints that caveat beside the p-value so an operator pasting output into
  the report cannot lose it. A performance claim needs a fresh run whose pre-registration
  fixes the metric and the objective before the data exists. Evidence:
  `docs/sweep-2026-08-06-findings.md` Part 3, and the effect is broad rather than peaked
  (7.4 points at 150 ms, 8.2 at 124 ms), so the objective is a service-grounds choice.
- **The objective stays out of `results/summary-per-seed.csv`.** That table is the evidence a
  reader checks the report against; baking one provisional threshold into it would read as a
  threshold already chosen. The driver CSVs are committed, so any objective is recomputable.

### Fixed
- **`reproduce.sh` was verifying a superseded sweep.** Its `HEADLINE`/`BASELINE`/`ABLATION`
  defaults named the 2026-08-05 early-hours cells while `docs/figures` had been regenerated
  from the confirmatory sweep in `a37cae5`. It diffed old-against-old, passed 5/5, and never
  touched the figures actually committed - a check verifying the wrong run reads exactly like
  a check that passes. Repointed at the confirmatory cells; `results/expected/` regenerated,
  and the balance co-primary (p=0.0000, −48.1%) is now covered too, which it was not before.
- **A relative reduction against a zero baseline raised `ZeroDivisionError` from inside the
  bootstrap**, ~80 lines from its cause. `ttft_p95` and `imbalance` are never zero so it could
  not fire until goodput arrived. `compare` now refuses up front and, for `ttft_slo_miss`,
  says the objective is loose enough that the baseline misses nothing.
- **`compare` names an unknown `--metric`** instead of raising a bare `KeyError` from a list
  comprehension - the failure mode that left `--metric imbalance` untestable for its whole
  life.

## 2026-08-06 (later) - bench image import regression (#31)

### Fixed
- **`import utilization` at analyze.py module level broke the bench image.** `load_driver.py`
  does `from analyze import percentile` and runs *inside* that image, whose `Dockerfile.bench`
  COPY ships six files and not `utilization.py`, so `import load_driver` raised
  `ModuleNotFoundError` - the measurement path, i.e. every future sweep. Shipped on `main` in
  `e3794a0` and turned `bench-image` red. Import moved inside `per_seed_imbalance`; the image
  is unchanged, so `BENCH_TAG` semantics are untouched.
- **It was catchable pre-merge and was missed.** `bench-image.yml` triggers on `push` with a
  paths filter, so it runs on any branch push touching `benchmarks/**`. It ran on `e3794a0` and
  **failed at 22:41:23Z; #46 was merged at 22:48:13Z**. The PR head at merge was `a939f51`, a
  docs-only commit the paths filter skipped, so no run existed for the head SHA and
  `gh pr view --json statusCheckRollup` reported the *previous* commit's `build` success -
  green, carried forward from a commit predating the break. The rollup was read as proof and
  the branch's workflow-run history was never checked.
- **Guard:** `test_analyze_module_level_imports_survive_the_bench_image` parses
  `Dockerfile.bench` for the shipped file list and fails if `analyze.py` gains a module-level
  import of a local module not in it. Verified to fail on the real bug, not just pass on the
  fix. It runs under pytest on every commit with no paths filter, so it cannot go stale the way
  the rollup did.
- **Process note:** a green `statusCheckRollup` does not mean the head commit was tested. When a
  paths filter skips the head, the rollup carries the last run's conclusion forward. Check the
  workflow run history for the branch and confirm the run's `headSha` matches.

## 2026-08-06 - Confirmatory sweep run; claim 1 passes, claim 2 null (#31)

Full run log, diagnosis and next-experiment proposal: `docs/sweep-2026-08-06-findings.md`.

### Decided
- **Claim 1 (balance) PASSES, claim 2 (TTFT p95) is NULL**, against pre-registration rev 2
  ([`5196484091`](https://github.com/BenEpstein/caching-in-llms/issues/31#issuecomment-5196484091))
  as amended by Amendment 1
  ([`5196605866`](https://github.com/BenEpstein/caching-in-llms/issues/31#issuecomment-5196605866)):
  imbalance -48.1% CI [37.7%, 56.3%] p=0.000010 18/20; TTFT p95 -2.7% CI [-4.3%, +15.4%]
  p=0.1153 vs alpha 0.025. Rev 2 pre-declared the null publishable; reported as one.
- **Rule 6 was knowingly overridden, with Ben's authorisation.** Claim 1's test path did not
  exist and was written 9 min after the last cell closed. Disclosure and mitigation in the
  findings doc, Part 1; §6 must repeat it.
- **The `b0` drift sentinel did not hold** (-13.2% TTFT p95, CI excluding zero, 16/20 worse).
  Per Amendment 1: ambiguous between drift and placement, not read as placement. Claim 1
  unaffected. A closing `kvaware` bracket (~20 min) would settle it.
- **The operating point, not the policy, explains the p95 null**: `num_requests_waiting` was
  nonzero in 0 of 284 baseline scrapes. Rev 2's "at or above the knee" premise is falsified.
- **Next experiment should measure goodput at the knee, not TTFT p95** - proposal only, not
  pre-registered. Guardrails in the findings doc, Part 4.

### Added
- Confirmatory sweep, 5 cells x 20 seeds, committed to `results/`; all pass the validity gate.
- `analyze.py compare --metric imbalance`, the path claim 1 never had; 7 tests.
- `fig11-inflight-vs-time.png` - per-engine in-flight vs time, the #31 DoD figure.
- `CONTEXT.md`: **Load Imbalance**, the co-primary, distinguished from **Relative Load**.

### Fixed
- `ALPHA = 0.025` constant: the verdict line printed `< 0.05` against a pre-registered 0.025.
- `per_seed_imbalance` now reuses `utilization.read_series` instead of a third verbatim copy
  of the router-job filter, which also gains its `worker_id` disambiguation.
- Three figure captions asserted conclusions this data falsifies (fig1's "6 seeds", fig7's
  "hit rate is flat", fig3's "saturated"); all now derived on render.
- `summary-per-seed.csv` referenced 5 cells with no committed raw data, failing
  `reproduce.sh` check 1 on `main` too. Dropped; they survive at `ffe1c77`.
- Units documented rather than renamed: `ttft_*`, `e2e_*` and `itl_*` are all SECONDS.

## 2026-08-05 (late) - scripts/reproduce.sh (#28)

### Added
- **`scripts/reproduce.sh`** - regenerates every reported number from committed data and fails
  on drift. Five checks: every run in the summary has committed raw data; both frozen workloads
  reconstruct from their manifests; `summary-per-seed.csv` regenerates; the headline and
  ablation statistics regenerate; the series behind every figure regenerate. Gives §6 the line
  *"no number appears in this report that `scripts/reproduce.sh` cannot regenerate from
  committed data"* - one command, no hardware.
- **`plot_results.py --dump-data`** - writes the series behind each figure as JSON.
  **Figures are deliberately not byte-compared**: matplotlib output moves with font
  availability, version and metadata, so a PNG diff would go red on a fresh runner for reasons
  unrelated to our data - and a check that cries wolf gets muted, which is worse than no check.
- `results/expected/{stats.txt,figure-data.json}` - the derived baselines.

### Fixed
- **`--update` could have destroyed data.** It refreshed every comparison target, and one of
  those targets is `results/summary-per-seed.csv` itself - committed data, not a baseline. When
  a run directory is missing, the regenerated file is a strict *subset*, so `--update` would
  have silently deleted real rows. Split into `verify_against` (committed data, never written)
  and `check` (derived baselines under `results/expected/`, safe to refresh). Guarded and
  verified: `--update` leaves the summary byte-identical.
- `mapfile` (bash 4) replaced with a portable read loop - macOS ships bash 3.2, and this repo
  has already been bitten once by a bash-4 builtin (`declare -A` in `apply-router-patch.sh`).

### Found - needs a decision (#7 / #28)
- **Five runs referenced by `summary-per-seed.csv` have no committed directory**, so 10 of its
  299 rows cannot be regenerated from the repository:
  `20260803-235537-kvaware`, `20260804-000537-kvaware`, `20260804-001911-kvaware`,
  `20260805-002149-kvaware`, `20260805-003128-kvaware`. Git has **no history** for any of them -
  they were never committed, not deleted. The last two are the OSL pilot cells (note the
  `osl` column reading 128 and 256). None is a headline arm.
  **`reproduce.sh` is therefore red on `main` today, and is deliberately NOT wired into CI
  until this is resolved** - shipping a knowingly-red check trains people to ignore CI.
  Fix is either committing those directories or dropping their rows; both are Ben's call
  because it is his data.


## 2026-08-05 (late) - Fake vLLM server + end-to-end harness test (#28)

### Added
- **`benchmarks/fake_vllm.py`** - an OpenAI-compatible streaming stub, stdlib only (a test
  fixture should not add a runtime dependency). Token chunks carry **`"usage": null`**, which is
  the entire point: that is what `stream_options={"include_usage": true}` makes vLLM emit, and a
  stub omitting the key could not catch the bug this exists for. Injected first-token and
  inter-token delays make TTFT and ITL *known values* rather than merely non-zero - "non-zero"
  would also pass for a driver timestamping the wrong chunk.
- **`benchmarks/test_harness_e2e.py`** - 9 tests running `load_driver.py` **as a subprocess**
  (the actual CLI a cell invokes) against the stub, then through `analyze.read_run`. Asserts
  TTFT equals the injected delay, gap count is exactly `max_tokens - 1`, the usage-only chunk
  contributes neither TTFT nor a gap, the CSV schema matches `Result`'s fields, and errored
  requests are counted but carry no latency. **2.1 s**, no GPU.

### Verified
- **The regression demo #28's DoD requires.** Reintroducing the 2026-08-04 classification bug
  (`'"usage"' in data`) turns **5 of 9 red**; reverting restores green. Instructive detail: the
  token-count and CSV-schema tests **still passed** with the bug in place - which is exactly how
  it shipped. Every column looked plausible except the primary metric.


## 2026-08-05 (late) - Confirmatory sweep pre-registered and signed off (#31)

### Decided
- **Pre-registration rev 2 signed off, gate open** - #31 comment
  [`5196484091`](https://github.com/BenEpstein/caching-in-llms/issues/31#issuecomment-5196484091).
  Rev 1 (`5196041638`) is superseded and marked as such; it stays unedited as the record of the
  first sign-off. Nothing runs against rev 1.
- **Metrics reframed to "what we test" vs "what we show."** Two tested claims at α=0.025
  (imbalance ratio, TTFT p95), everything else as bar charts with variance bars and no p-values.
  Per-seed stays the unit of analysis (500 requests inside a seed are not independent; pooling
  would claim n=10,000) but is banned from the report.
- **TTFT p95 kept as the latency claim**, p50 rejected as a co-primary. Switching after seeing
  p50 move further is the error the pre-registration exists to prevent; a p95 null with a p50
  shift is reported as a labelled exploratory finding.
- **p10 dropped entirely.** The committed path emits p50/p90/p95/p99; adding p10 would have meant
  writing analysis code for this run. Rule 6 now forbids that outright.
- **β=2.0 retained.** Balance saturates by β=0.5; in the prior 20-seed sweep β=2.0 bought 0.07
  further imbalance reduction and cost ~12% p50 / ~13% p95, landing worse than baseline on both.
  It is the only cell that shows the tradeoff turning over, and what makes β=0.5 a defended
  optimum rather than an arbitrary small number.
- **No closing `kvaware` bracket** (Ben, 2026-08-05). Arm is therefore confounded with position
  in the window; the residual is declared, with `loadaware-b0` as drift sentinel.
- **Amendment 1: cell order is `kvaware, b0.5, b1.0, b2.0, b0`**, not β ascending - #31
  amendment comment, posted pre-data with zero cells run. Two reasons: the only pair carrying a
  p-value (`kvaware`, `b0.5`) becomes adjacent and so spans the least wall-clock, and `b0` at
  maximum separation becomes a real drift sentinel, recovering most of the dropped closing
  bracket at zero cluster time. Accepted cost, declared in advance: a `b0` that moves cannot
  separate drift from a placement effect, and is reported as ambiguous rather than read as
  either.

### Fixed
- **`BENCH_TAG` in #31 was stale at `90dd30a`.** `workload_gen.py` and `freeze_workloads.py` ship
  inside the bench image and both changed in the #36 re-land, so the 20-seed shakedown does not
  transfer. Ticket now reads `42e6a32` / `LOADAWARE_TAG=acf43d1`, validated by a 3-seed smoke
  (in-pod manifest check passed, 0.00% pooled errors, `utilization_coverage` 1.000 on all series).
- **"Final run" framing removed** from the #31 title and body. Re-runs are expensive, which is the
  reason to decide precisely now; it is not the project's last cluster time.

## 2026-08-05 (late) - #30 deletions executed

### Removed
- **`load_driver.py`'s closed-loop mode** - `run_closed_loop()`, `--concurrency`, the dispatch
  branch and the docstring advertising it. No caller ever passed `--concurrency`; `--rate` is now
  a plain required argument instead of half of a mutually-exclusive group. An untested second
  path through the driver that measured nothing is worse than no second path.
- `rate_pilot.sh`'s no-op `sys.path.insert` (under `python3 - <<PY`, `__file__` is `<stdin>`, so
  it inserted `"./."`). The real insert on the line above is untouched.
- `apply-router-patch.sh`'s `registration_controller.py` target - `patches/` holds exactly three
  files and the Dockerfile knows only those three.
- `dcgm_poll.py --duration` and its `t_end` branches - the sole caller passes only `--url`/`--out`
  and the process is killed by the cleanup trap.
- `load_gate.py`'s beta echo, which printed a grid including `0.25` that `run_sweep.sh` does not
  use and never reads.

### Changed
- **`plot_results.py --cand` is now REQUIRED.** It first defaulted to the literal
  `loadaware-b0.1`, then to an inference accepting exactly one non-b0 loadaware cell - which the
  standard `BETA_GRID="0 0.5 1.0 2.0"` never satisfies, since it always yields three. **The
  default path could not fire at all**, which is how it was found: regenerating figures on
  2026-08-05 raised every time. Both versions were the same shape of defect - an interface that
  looks like it has a working default. Which arm is the headline is a pre-registration decision,
  not something a plotting script should guess from whichever directories it was handed.
- `benchmarks/README.md` now documents the figure-generation step at all; the ticket noted the
  file never mentioned `plot_results.py`, which produces every committed figure.


## 2026-08-05 (late) - Stale-comment sweep (#30, non-destructive half)

### Fixed
- **`benchmarks/README.md` sweep description**, the operator's manual a grader follows. It
  advertised 6 cells x 6 seeds with a grid containing `loadaware-b0.25` and `loadaware-b4.0`;
  `run_sweep.sh` produces **5 cells at n=20** on `BETA_GRID="0 0.5 1.0 2.0"`. The statistics
  section still said "n=6 per cell" and named `loadaware-b0.1` as the headline - a cell name
  the retired per-rate beta calibration produced and which can no longer be generated. Retired
  names are now listed explicitly as un-generatable rather than silently corrected, since they
  still appear in older `results/` directories.
- **The layout table omitted seven executables**, including `plot_results.py`, which generates
  every committed figure and was mentioned nowhere in the file.
- `run_cell.sh` claimed the beta-sweep cells replay a 3-seed subset; `run_sweep.sh:85` says
  n=20 on every cell. The subset is retired.
- `plot_results.py`'s `--cand` help said beta is calibrated per-rate "so the cell name is not a
  fixed literal" - contradicting its own docstring, which says the opposite. It now states the
  real constraint: the 4-point grid has three non-b0 cells, so inference cannot fire.
- `plot_results.py` documented 5 figures while writing 10. `analyze.py` still described "the 6
  paired per-seed differences" and p<0.05; it is 20 paired differences at a Bonferroni 0.025.
- `values-baseline-kvaware.yaml` justified `gpuMemoryUtilization: 0.45` with arithmetic over a
  retired 64-prefix s=1.2 hot set. The **value is right and is kept**; the comment now says the
  derivation is historical - re-derive before changing it, not to justify it.
- `apply-router-patch.sh` and `deploy/dev/README.md` both referenced a `PATCH_TARGETS` map that
  is a `case` statement (bash 3.2 on macOS has no `declare -A`). `deploy/README.md` cited
  `values-baseline-kvaware.yaml:58`; the line is 74.

### Decided
- **Deletions in #30 are held pending sign-off**, per that ticket's step 0. All five
  "confidently dead" items were re-verified as still dead before proposing them.
- **The duplicated `job=vllm-engines` parse stays for now.** It is triplicated and only one copy
  is tested, but the copies differ (one windows by seed, one does not) and *this exact parse
  already corrupted 17 of 74 seed rows once*. Consolidating it while #31's sweep is about to
  produce the final numbers would change analysis code underneath the result.

## 2026-08-05 - PR #23 re-landed on the integration branch (#36)

### Changed
- **PR #23 was rebuilt, not replayed.** Its base moved three times underneath it (#26, #27, #29,
  #35), so the 19-commit rebase was abandoned in favour of re-applying the surviving work onto
  `feat/relative-load-normalization`. Two of its commits are **deliberately dropped**:
  `fig10-utilization` and its tests (#35 does this properly, with a shared `utilization.py`, a
  coverage gate, and KV-cache memory per engine), and the old `run_cell.sh` edits (#27 turned the
  replay into an in-cluster Job).
- **README rewritten for the post-α policy** (#32): one knob, `LOADAWARE_BETA`, the
  `1/(2β)` cancellation rule, in-cluster measurement, 168 tests, current provenance paths. The
  latency row states plainly that it is pending #31 rather than quoting WAN-polluted numbers.
- **Report rewritten** for the same: relative-load formula and the reason α was removed;
  Results carry the settled imbalance co-primary (**−43.7%, 20/20 seeds, p<0.0001** at β=0.5)
  and hold the latency row open; resource cost re-derived from `utilization.py`.

### Added
- **The novel-prompt profile re-landed** (#25) and re-verified against the new harness: both
  manifests still reproduce bit-identically after `9d14c95` changed `WorkloadConfig`'s defaults.
  `WORKLOAD_PROFILE` now threads run_cell.sh → bench_job.sh → pod env → verify_dataset.sh, and
  is recorded in `run.json`.
- **Micro-benchmarks rewritten for the post-α API** (#24): `score_endpoint` lost its α argument
  and `relative_loads` is new - it recomputes the fleet mean per request, so it is the piece
  whose cost grows with fleet size and it gets its own benchmark. Measured: the router's CPU is
  0.212 / 0.213 / 0.214 core-s/s across kvaware / β=0 / β=0.5, i.e. the policy is free at this
  scale.

### Fixed
- **A regression caught while porting: the `--profile` refactor resolved the manifest from the
  repo tree instead of from `--out-dir`.** That silently breaks `verify_dataset.sh`, which copies
  the committed manifest into a writable directory because `/app` is read-only under the
  restricted SCC - i.e. it would have broken the in-cluster Job. Each profile is now a directory
  **containing** its manifest, so `--out-dir` keeps its contract. All three paths verified.

### Decided
- **The report's latency co-primary stays empty until #31 runs.** Engine-side TTFT shows β=0.5
  improving ~9% at p=0.0053, but it was chosen after seeing the client-side null, so it is
  exploratory by construction and is reported as a named secondary only. Substituting it would be
  the same error as adding seeds until a p-value cooperates.
- **The relative-load normalization is recorded in §6 as a design change made in response to a
  measured weakness, not to a result** - β was previously tied to absolute concurrency and two
  probes at the same rate disagreed (0.034 vs 0.013). Future-work item 1 changed accordingly:
  the open question is now fleets larger than two engines, where the argmax can chase a single
  idle instance.

## 2026-08-05 - Sweep preflight: both image tags checked before the first helm upgrade

### Fixed
- **`run_sweep.sh` never mentioned `BENCH_TAG`.** Since #27 moved the measured replay
  in-cluster, every cell replays from the bench image on **both** arms - but the usage line
  still read `LOADAWARE_TAG=<sha> ./run_sweep.sh <rate>`. Following it literally, cell 1 died
  on `run_cell.sh`'s own guard and `set -e` took the rest of the batch with it. Both tags are
  now asserted in `run_sweep.sh` before the first helm upgrade, so a missing tag costs one
  second instead of aborting a sweep mid-batch. Found by reading the script, not by burning a
  cell; all three guard paths (`BENCH_TAG`, `LOADAWARE_TAG`, rate) verified to exit before any
  cluster action.

### Decided
- **The sweep's cell order stands as written: `kvaware` first, then beta ascending, with the
  manual closing `kvaware` as the drift bracket.** Reordering to put the headline pair adjacent
  was considered and rejected - proximity only *suppresses* drift, while the closing bracket
  *measures* it as kvaware-vs-kvaware. Consequence for planning: the confirmatory run is **6
  cells (~2 h 25 min), not 5**, and the pre-registration on #31 must state in advance which
  `kvaware` cell the headline pairs against (opening, closing, or both) - otherwise that
  becomes a choice made after seeing the data.

## 2026-08-05 - Utilization is §3's last unreported metric family, and it is a result (#35)

### Added
- **`benchmarks/utilization.py`** - the §3 utilization readout, plus its coverage gate.
  Most of what it reads was already collected in every cell going back weeks and read by
  **zero** committed code; the two LMCache memory gauges are new here. `report` prints
  GPU / GPU-memory / CPU / host-memory per cell; `coverage --update-run-json` records how
  much of the measured window each series actually observed. 22 tests, including the #27
  pilot's tail truncation as a regression case.
- **`fig10-utilization.png`** (`plot_results.py`) - four panels, one per §3 resource.
  KV-cache occupancy is the load-bearing one: it separates the arms monotonically in β
  (kvaware spreads **1.70×** across the two engines, b0.5 **1.18×**, b2.0 **1.11×**) because
  it is the resource the policy contends for.
- **`lmcache:local_cache_usage` + `lmcache:active_memory_objs_count`** added to `prom_dump.py`
  and both read. The former (~3.8 GB/engine, confirmed live) is the engine-side *memory*
  number the project believed it did not have. Deliberately only two: #35 exists because six
  series were collected for weeks and read by nothing, so adding unread ones would have
  reproduced the thing it fixes.

### Decided
- **DCGM stays the source of record for GPU utilization**, against the standing proposal to
  drop it for Prometheus. vLLM's `/metrics` exposes **113 metric names and no SM% or power**
  (checked at the endpoint), so there is no Prometheus substitute for that half of §3.
  Evidence and full metric map: [#35](https://github.com/BenEpstein/caching-in-llms/issues/35).
- **The `nvidia-gpu-operator` RBAC "wall" was never a permissions problem** - `oc whoami` is
  `kube:admin` and `can-i list pods -n nvidia-gpu-operator` returns yes. What a ServiceMonitor
  actually costs is **reproducibility**: `deploy/prometheus.yaml` is a namespaced `Role`, so a
  grader can `oc apply -f deploy/` into their own namespace; a foreign-namespace RoleBinding
  (or a cAdvisor `ClusterRole`) makes that need cluster-admin. Recorded because the old
  rationale would otherwise get re-litigated on a false premise.
- **Engine host-CPU and engine RSS are reported as unavailable, not substituted.** vLLM
  registers no `process_*` collector, so the two "CPU (engines) / Memory (engines)" rows that
  #35 opened with were never collected in any of the 28 cells on disk. The router's CPU is the
  number that matters anyway - it is the only component the extension changes, and it comes
  back **flat** across arms (0.199-0.220 core-s/s, RSS 1.013-1.028 GB, kvaware mid-pack), which
  is the §5 claim that the policy's routing work is free.
- **Utilization is reported from #31's cells, not the 28 on disk.** All 28 predate #27 (no
  `driver` block in any `run.json`); quoting latency from one set and utilization from another
  invites a provenance question in §6 for no gain. The analysis is cell-agnostic, so this costs
  nothing, and pre-#27 cells stay a valid fallback precisely because utilization is server-side.
- **The coverage gate warns, never fails.** The driver CSVs are the primary measurement and a
  cell with good latency data must not be discarded over utilization sampling.

### Fixed
- **DCGM port-forwards now run under supervisors that reconnect.** A bare `oc port-forward`
  dies for good when the VPN drops while `dcgm_poll` keeps polling a dead local port - which is
  how the #27 pilot silently lost the last **171 s of a 712 s cell** (24%, a clean tail
  truncation, no internal gaps > 12 s). Each supervisor traps `TERM` and kills its own forward,
  so cleanup cannot orphan one and leave a local port bound for the next cell. `wait "$pf" || true`
  is load-bearing: `run_cell.sh` runs under `set -e`, so a bare `wait` would kill the supervisor
  on the first dropped forward it exists to survive. This narrows the exposure; it cannot close
  it, which is why the coverage gate lands alongside it.
- **The coverage gate was blind to the failure the supervisors create, and to total loss.**
  Both found by adversarial review of this branch before merge. A *span* measure scores an
  internal gap as perfect - and reconnecting forwards turn tail truncations into exactly that
  shape, so on a real cell dropping 42% of samples mid-window scored **99.8%**. Coverage is now
  gap-aware. Separately, a source that produced *nothing* got no key at all rather than 0.0, so
  a cell with no GPU data was indistinguishable from a healthy one; total loss is now scored and
  flagged. The gap-aware measure also credits the window edges, which fixes a false-warn ceiling
  of `1 - step/window` that made any cell under ~100 s warn on perfect data.
- **`counter_rate` reported a restarted router as cheaper.** It differenced the endpoints, and
  the router is scraped through its Service so a restart does not change the series key. A
  synthetic restart mid-window read 0.114 against a true 0.2, unflagged - on the exact metric
  behind "router CPU is flat across arms". Now sums positive deltas.
- **`fig10` drew missing data as a missing bar**, which matplotlib renders identically to a
  measured zero. On the CPU panel that is the shape of the §5 no-overhead claim. Missing series
  are now labelled "no data".
- **Engine bars were labelled with ReplicaSet hashes** (`rrcpr`, `b2xv8`) and the GPU legend
  named two different *nodes'* `gpu0` as "GPU 0"/"GPU 1". Engines are now numbered, GPUs named
  from their own labels, and the GPU count derived rather than hardcoded to two.
- Also from review: `run.json` is rewritten via write-then-rename rather than truncated in
  place; series from one pod under different `worker_id`s no longer average together;
  `utilization.window()` renamed `manifest_window()` (it is a *different interval* from
  `load_gate._window()`); and `fig_imbalance` now calls `utilization.read_series` instead of
  carrying a third verbatim copy of the same parse.

## 2026-08-05 - The measured replay moves into the cluster: no WAN in TTFT (#27)

### Added
- **`Dockerfile.bench` + `.github/workflows/bench-image.yml`** - the in-cluster driver image
  (`python:3.12-slim` pinned by digest + httpx + the driver path), built and SHA-tagged in CI
  and pushed to the public `quay.io/rhl193000/bench-driver`, mirroring the router-image path.
  Its verify step runs `verify_dataset.sh` **inside the built image**, so "the frozen dataset
  is reconstructible from source" becomes a tested claim on every push rather than an asserted
  one (0.84 s for all 20 seeds).
- **`benchmarks/in_pod.sh` + `verify_dataset.sh` + `bench_job.sh` + `collect_job.py`** - the
  replay now runs as a Job against `stack-router-service.<ns>.svc.cluster.local:80`. The pod
  regenerates and SHA-256-verifies the 20 seed files rather than being shipped 126 MB, and
  returns CSVs through its log as one gzip+base64 frame per seed. 12 new tests (132 total).

### Changed
- **`run_cell.sh` step 8 only.** helm, cold start, registry probe, warm-up gate, Prometheus
  dump, DCGM and the validity gate are untouched and still laptop-side. `BENCH_TAG` joins
  `LOADAWARE_TAG` as required on **every** cell, both arms. `run.json` gains a `driver` block
  (location, node, image, target), which is what separates in-cluster cells from the WAN ones
  already under `results/`.
- **The measurement window now comes from the pod's clock**, not the laptop's: image pull plus
  dataset verification sit between warm-up and the first request, and a laptop-clock window
  would have dragged warm-up traffic into the Prometheus dump and contaminated the imbalance
  co-primary.

### Decided
- **Fix the instrument, keep the metric.** 45-59% of every recorded TTFT was WAN (RTT avg
  44.4 ms), and the non-engine component swung 121 -> 195 ms between two cells an hour apart -
  a per-cell systematic offset larger than the 10-60 ms effect under study, so more seeds could
  never fix it. That is the complete explanation for TTFT being a null in every sweep while
  load imbalance (server-side, therefore immune) reached p<0.0001. Switching to engine-side
  Prometheus histograms as the primary was **rejected**: it is metric-switching after seeing a
  null, and over half of all requests land in one 150 ms-wide bucket against a 10-30 ms effect,
  with p99 where under 2% of the mass lives - §3 requires per-request p95/p99 and interpolated
  percentiles there are manufactured. Subtracting an RTT baseline was rejected as an
  unverifiable per-request correction. Engine-side is retained as a labelled secondary and as
  the cross-check that the driver pod is not perturbing the engines.
- **Results travel through the pod log, framed per seed with a checksum.** One channel for
  progress and data, and it survives pod GC. Per-seed frames rather than one blob so truncation
  is detectable per seed; `collect_job.py` is all-or-nothing, because a partially-recovered cell
  would enter the paired stats looking like a real observation. Measured 2.4 MB per 20-seed cell
  against kubelet's 10 Mi rotation - the issue's original 1.4 MB estimate was optimistic by 1.7x.
- **No CPU limit on the driver pod, and no anti-affinity.** CFS throttling would inflate client
  TTFT exactly the way the WAN did. Anti-affinity is unsatisfiable anyway: gapu-2 has two
  schedulable nodes with an engine on each, and worker0 at 93% CPU / 99% memory requested means
  the driver deterministically lands on worker1. The control is the engine-side cross-check, not
  a scheduling rule; the node is recorded in `run.json`.

### Fixed (follow-on, same session)
- **The Job progress tail was silently dead - twice, for two different reasons.** The throwaway
  cell produced a fully correct result with zero pod output on the operator's terminal:
  `oc logs -f job/<name>` ran straight after `oc apply` and lost a race, because the Job exists
  before its pod does and kubectl's selector then matches nothing. Resolving the pod first walked
  into the second mode - `oc logs -f pod/<name>` against a still-`ContainerCreating` pod returns
  `BadRequest`, which is what a fresh (uncached) image tag guarantees. The first fix had passed
  only because that run's image happened to be cached from the previous cell. `--pod-running-timeout`
  fixes neither: it governs waiting when a **selector** resolves pods, not a pod named directly,
  so it was dead code that looked load-bearing. Now one loop waits for the pod to exist **and** to
  leave `Pending`. Cosmetic both times - collection is a plain `oc logs` at the end, which
  re-reads the whole log and is idempotent, so the b0.5 pilot collected all 20 seeds correctly
  with no tail at all.

### Verified
- **20-seed `loadaware-b0.5` pilot** closed the four gaps the 3-seed throwaway could not:
  log volume at full size (**2.35 MiB measured against 2.34 predicted**, 4.3x under kubelet's
  10 Mi), the dotted job name (`bench-loadaware-b0-5-<epoch>`) accepted by the API server,
  `LOADAWARE_TAG` + `BENCH_TAG` live together, and ~12 min of Job runtime without eviction. All
  20 seeds at 500 rows, pooled error rate 0.36%, validity gate exit 0. It also survived an
  unplanned network blip mid-run: the `oc wait` retry loop absorbed it and collection completed.

## 2026-08-05 - Doc truth sweep: handoffs removed, alpha purged, a claim un-inverted (#29)

### Decided
- **Session handoffs are not repo artifacts. All six were deleted and the path gitignored**
  (`docs/handoffs/`, `docs/handoff-*.md`). They were going to be frozen in place; deleting is
  the stronger move because it *enforces* the rule `CLAUDE.md` already states - rationale lives
  in GitHub issues + this changelog, `docs/` holds artifacts only. Five of the six still
  pre-registered **β=0.1 at rate 7.5/10.5** as the headline against a shipped operating point of
  **β=0.5 at rate 16**, and a grader finding a pre-registration that does not match the reported
  result will reasonably suspect post-hoc selection. Removed: `handoff-cache-sizing.md`,
  `handoff-core-implementation.md`, `handoffs/cache-sizing-decisions.md`,
  `handoffs/claude-wayfinder-3-e533a3{,-decisions}.md`, `handoffs/feat-relative-load-normalization.md`.
  All in git history.
- **The `upstream-findings.md` §6 drop-in paragraph is frozen, not re-derived.** It is quantified
  entirely at 7.5/10.5 req/s and is *written to be pasted into the report*, so as it stood it would
  have contradicted the report's own data. Re-deriving it at rate 16 is possible - all four
  quantities have backing on disk, including a 20-seed `roundrobin` cell - but doing it here would
  mean another ad-hoc in-session number feeding the report, which is the exact defect #28 exists to
  fix. The section now carries a do-not-paste banner naming the four quantities and the files they
  come from; reviving it belongs to #28, with committed code.

### Fixed
- **The `kv_aware_threshold` claim was stated backwards in three docs, and the code says the
  opposite.** `routing_logic.py:396` tests `matched_tokens < max(len(token_ids) - self.threshold, 0)`.
  At ISL **1578** with the threshold defaulting to **2000**, `max(1578-2000, 0) == 0`, so the test
  reduces to `matched_tokens < 0` and **can never fire**: kvaware takes the cache path for every
  request with any holder, at any `matched_tokens`. The three docs asserted that prompts must exceed
  2000 tokens or kvaware never takes the cache path, and presented that as *the reason* the workload
  uses long prefixes. Results are unaffected - kvaware takes the cache path either way - but the
  stated design rationale was wrong. All three sites were deleted with the handoffs; the corrected
  explanation now lives in `patches/README.md`, which held a **fourth, live** copy of the same
  inverted reasoning in the ⚠️ baseline-measurement warning. That warning's *conclusion* (revert the
  patch before measuring the baseline arm) stands - it costs one script and removes an assumption -
  but its stated mechanism did not.
- **`alpha` purged from every live doc.** `project-brief.md` (which `CLAUDE.md` calls the design
  source of truth) and `feasibility-verification.md` (the §2 deliverable's raw material) both still
  carried `score = α·matched_tokens − β·load`, un-normalized on both terms. Now
  `matched_tokens/prompt_tokens − β·relative_load`. Surviving `α` references are deliberate: the
  "there is no α" explanations, and `docs/decisions/second-optimization.md`, which is frozen history.
- **`project-brief.md` named the wrong model and the wrong offload buffer**: `llama8b` /
  `meta-llama/Llama-3.1-8B-Instruct` and `cpuOffloadingBufferSize: "20"`, against the shipped
  `Qwen/Qwen2.5-3B-Instruct` and `"4"` in `deploy/values-baseline-kvaware.yaml`.
- **`CONTEXT.md` defined two core terms against the code.** "Load Penalty" was "running **+ queued**
  requests"; the code is `in_prefill_requests + in_decoding_requests` with no queue term
  (`num_requests_waiting` is a run diagnostic and does not enter the score). "Cache-Hit Benefit" was
  a token *count*; it is a *fraction*, which is what makes β dimensionless. Added "Relative Load" as
  its own entry, since that is the term β actually weighs.
- **`CLAUDE.md`**: "Two people work on this repo" replaced with the parallel-branch reality plus a
  check-open-PRs instruction; the "(stretch) hot-prefix KV replication" line dropped, since
  `CONTEXT.md` records that policy parked.
- **Dangling references created by the deletion**, all repointed: `routing_logic.py:591`,
  `tests/test_kv_controller_lookup.py`, `tests/test_loadaware_routing.py`,
  `docs/decisions/second-optimization.md`. `README.md`'s repo-layout row still names
  `handoff-core-implementation.md`, but PR #23 already removes it, so it was left alone rather than
  conflict with #26.

### Changed
- **Status headers added to 5 files** (`CONTEXT.md`, `CLAUDE.md`, `patches/README.md`,
  `deploy/README.md`, `deploy/dev/README.md`) and refreshed on the 3 amended `docs/` files. Every
  tracked `.md` now carries one except `README.md` (owned by #26) and `CHANGELOG.md` (an
  append-only log, not a doc making claims). Note the pattern this sweep found: **every doc
  carrying a `live` header was stale**, so the header is worth only as much as the re-verification
  behind it.
- **Disambiguated a numeric collision in `upstream-findings.md`.** Dynamo's default converts to
  β ≈ 0.5 **on the retired absolute axis**, and the project ships β=0.5 **on the normalized axis**.
  The two are numerically equal and semantically unrelated, and as written the passage read as
  evidence against the shipped configuration.

### Fixed (follow-on, same session)
- **The stale workload parameters were chased to their last two homes.** The map's #3 entry recorded
  the frozen workload as "s=1.2, 20×2048 tok" and `WorkloadConfig` still *defaulted* to
  `prefix_pool_size=20, zipf_s=1.2` (duplicated again in `workload_gen.py`'s argparse). The frozen
  dataset is **128 prefixes at s=0.9, 500 requests, 20 seeds**; s=1.2 with a 20-prefix pool were
  exploratory values that outlived the freeze. No recorded result moves - `freeze_workloads.py`
  always passed every value explicitly, so the defaults never reached the data, and regenerating all
  20 seeds after the change reproduces the committed manifest SHA-256s exactly ("all workloads match
  the committed manifest - frozen dataset verified", exit 0).
- **The real skew is much gentler than the retired figures implied.** Measured over all 20 frozen
  seeds (10,000 requests): **top-1 prefix = 14.8% of requests, top-3 = 28.0%, top-10 = 47.9%**,
  matching Zipf theory at s=0.9/N=128 to within 0.1 pp. The figures quoted at s=1.2 were 35% / 57%.
  This matters for §3: the hot prefix carries about a seventh of traffic, not a third.
- **`test_defaults_match_the_frozen_manifest`** now pins `WorkloadConfig`'s defaults to
  `workloads/manifest.json` field by field, so the two cannot drift apart again, and the argparse
  defaults reference the dataclass instead of re-stating literals. 121 tests pass.

## 2026-08-05 - Front door part 1: figures are reproducible, benchmarks/README stops lying (#26)

### Fixed
- **`matplotlib` was missing from `requirements.txt`**, and `plot_results.py` is the sole
  generator of all 9 figures in `docs/figures/` - so on a clean clone every §5 figure died on
  `ModuleNotFoundError` while working on the author's machine. Verified in a fresh venv:
  install → `pytest benchmarks/ tests/ -q` 120 passed → all 9 figures regenerate, exit 0.
  Audited the rest: `httpx`, `pytest`, `matplotlib` are the only third-party imports across
  `benchmarks/` + `tests/`; `analyze.py` and `export_summary.py` are stdlib-only, confirmed.
- **`benchmarks/README.md` had the TTFT source inverted.** It argued engine-side histograms
  "miss router overhead" and that driver CSVs are the trustworthy source - the opposite of what
  was measured (45-59% of client `ttft_s` is laptop-to-cluster network; per-cell offset larger
  than the effect). Now states the WAN finding, keeps engine-side as the per-seed-windowable
  cross-check with its coarseness named, and points at #27 as the fix to the instrument.
- **`benchmarks/README.md` contradicted `.gitignore`** about where the data is: it claimed
  `results/` was gitignored with two artifacts force-added. `results/` is tracked in full -
  1069 files, ~67 MB, sole exclusion `results/**/*.jsonl`. A grader reading the old text would
  not have gone looking for raw data that is sitting in the repo.

### Decided
- **The `README.md` half of #26 is deferred, not dropped.** PR #23 already rewrote it (plus
  `requirements.txt`), so writing a second front door here would guarantee a conflict. #23's
  version is itself stale on this branch - it documents `LOADAWARE_ALPHA` and β=0.034, and α no
  longer exists - and with benchmarking and the final architecture still open, a README that
  declares the project complete would be wrong today. Tracked as its own ticket; evidence in
  [#26](https://github.com/BenEpstein/caching-in-llms/issues/26).

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

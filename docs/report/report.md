---
title: "Load-Aware Prefix Routing for the vLLM Production Stack"
subtitle: "Placement as a cache policy in a distributed KV cache"
author: "Eliad Bazak and Ben Epstein · Ben-Gurion University"
date: "August 2026"
geometry: margin=2.5cm
fontsize: 11pt
linkcolor: black
urlcolor: black
header-includes:
  - \usepackage{booktabs}
  - \usepackage{float}
  # Latin Modern has no Greek glyphs, so a bare alpha/beta in the source renders as
  # NOTHING in the PDF - silently, since pandoc only warns. Route them through math mode,
  # which is also the typographically correct form for what are mathematical symbols.
  - \usepackage{newunicodechar}
  - \newunicodechar{α}{\ensuremath{\alpha}}
  - \newunicodechar{β}{\ensuremath{\beta}}
  - \newunicodechar{≈}{\ensuremath{\approx}}
  - \newunicodechar{∈}{\ensuremath{\in}}
  - \let\origfigure\figure
  - \let\endorigfigure\endfigure
  - \renewenvironment{figure}[1][2] {\expandafter\origfigure\expandafter[H]} {\endorigfigure}
---

# Introduction

A KV cache stores the attention state of a prompt's prefix so a later request sharing that
prefix skips recomputing it. On a single instance, the cache policy is an eviction question:
what to keep. On a *fleet*, a second question comes first and dominates the answer — **which
instance's cache is even eligible to hit.** A request routed to instance B cannot benefit from
a prefix cached on instance A, no matter how good A's eviction policy is.

That makes placement a cache policy, not a load-balancing detail, and our measurements set the
scale of the lever. Under an identical workload, round-robin placement against the same cache
posts a median TTFT p95 of **11.528 s** against **0.426 s** for cache-aware placement — **27×**,
produced entirely by the routing decision — with a vLLM prefix-cache hit rate of **0.685**
against 0.912.

The instructive part is *why*, and it is not that round-robin balances badly. **Round-robin is
better balanced than the cache-aware baseline** — imbalance 1.723 against `kvaware`'s 2.680 —
and still 27× slower. It equalises request *counts*, not *work*: a request sent to the engine
that does not hold its prefix pays a full 2048-token prefill. Balanced counts, ruined locality.

That is the argument of this project in one comparison. Load-awareness has to be added **on top
of** cache-awareness, not substituted for it.

The baseline we build on already exploits half of that. The vLLM Production Stack ships a
`kvaware` router that asks LMCache's controller which instance holds a request's prefix and
routes there. It works, and it creates a new problem: routing *purely* by cache affinity
concentrates load. A popular prefix lives on one instance, so every request for it goes to that
instance, which then queues while its peer idles. Cache affinity and load balance pull in
opposite directions, and the stock router only pulls one way.

**This project adds the other term.** We contribute two changes:

1. **A multi-instance lookup in LMCache's controller.** The stock `lookup()` reports only the
   *first* instance holding each chunk — an acknowledged upstream TODO. No router above it can
   compare instances if it is only told about one.
2. **A `loadaware` routing strategy** that scores every endpoint by cache benefit against live
   load and takes the argmax.

Our headline result is that this **cuts load imbalance by 43.7% (p < 0.0001, 20 of 20 paired
seeds)**. The latency co-primary is **deliberately left open**: the pre-registered test was
measured over a wide-area network that turned out to contribute 45–59% of the number, so rather
than substitute a metric that flatters us, the instrument was rebuilt and the original test is
being re-run unchanged. The result that makes the mechanism credible is an ablation: with the
load term switched off (β = 0), the policy lands *on* the baseline — so the improvement comes
from the load term specifically, not from having rewritten the router.

## Related work

LMCache is vLLM's KV-cache backend, supplying chunked prefix storage across GPU, CPU and disk
tiers, a controller that tracks chunk residency per instance, and cross-instance KV movement.
Its default eviction policy is LRU, pluggable through `POLICY_MAPPING` (see `docs/baseline-justification.md`). The Production Stack contributes the router, whose existing
strategies are `roundrobin`, `session`, `kvaware`, and `prefixaware`.

NVIDIA's Dynamo KV-router solves the same tension with a deduplicated-block load accounting
scheme. We measured our workload against that design and chose not to adopt it; the Discussion explains why, and what it would have bought.

# Extension design

## Change 1 — per-instance match information

`KVController.lookup()` walks a request's token chunks and resolves each through
`registry.find_kv()`, which returns the **first** instance holding that chunk in dictionary
iteration order. The upstream source acknowledges the limitation directly: *"TODO: improve the
matching logic, return multi results."*

Two consequences follow, and together they motivate the whole project:

- **No router above it can rank instances.** Comparing A against B requires knowing what *both*
  hold. The stock API structurally cannot say.
- **Replication alone cannot balance load.** Even if a hot prefix is replicated onto both
  instances, lookup names only one, so a stock KV-aware router keeps sending all hot traffic
  there. Replication and routing have to be co-designed.

We extended `lookup()` to report matched-token counts for **every** holder. Prefix credit is
**contiguous per instance**: an instance stops earning credit at its first missing chunk, so a
reported match is a real, usable prefix rather than a count of scattered chunks. The change is
additions-only and preserves the existing return shape for existing callers.

## Change 2 — the `loadaware` policy

$$\text{score}(i) = \frac{\text{matched\_tokens}(i)}{\text{prompt\_tokens}} - \beta \cdot \text{relative\_load}(i)
\qquad
\text{relative\_load}(i) = \frac{\text{load}(i) - \overline{\text{load}}}{\max(1, \overline{\text{load}})}$$

The router computes this for **every** endpoint and takes the argmax, breaking ties by URL for
determinism. Three design decisions carry weight:

**Both terms are dimensionless, so β carries no unit from the deployment.** The benefit is a
fraction of *this prompt*; the load is a signed fraction of *this fleet's* mean, recomputed per
request from the same live statistics. That makes β a pure exchange rate between cache locality
and load, and it makes it portable: the router measures its own scale instead of inheriting one
from whoever tuned it last.

The arithmetic is worth stating because it is what the sweep grid means. With two engines, one
at $+r$ forces the other to $-r$, so the load gap is $2\beta r$ and a full cache hit is exactly
cancelled at $r = 1/(2\beta)$. At the shipped default β=1.0 that is $r = 0.5$: an engine
carrying 50% more than the fleet mean stops attracting cache-hit traffic.

Clamping the denominator at 1 is not cosmetic. Without it, a fleet mean of 0.1 turns a single
in-flight request into a relative load of 9.0, and the policy would thrash on noise at exactly
the load level where there is nothing worth balancing.

**An earlier design had a second weight, α, on the benefit term. It was removed.** Because the
benefit is already normalized to the cached fraction, α was a redundant scale factor — only the
*ratio* of the two weights sets the trade-off, so one knob expresses everything two did, and
the sweep grid halves.

**The load signal already existed and was already being ignored.** `EngineStats` scrapes
`num_running_requests`, `num_queuing_requests` and `gpu_cache_usage_perc` per instance and hands
them to *every* `route_request()` call. `KvawareRouter` uses none of it. Our scoring function
plugs in exactly there — no new collection path, no new failure mode.

**`kvaware` is left byte-identical.** The diff is additions-only: a new enum member, a factory
branch, and a new class. The baseline arm we measure against is the unmodified upstream code
path, not our code with a flag flipped.

One defect surfaced in review and is worth recording because it is invisible until an engine
restarts: the `instance_id → URL` bridge must refresh when an engine comes back under a fresh
id, and only the *live* id may be credited. Without that, placement silently degenerates to
least-loaded for the remaining life of the router.

## Tunable parameters

| Parameter | Env var | Default | Meaning |
|---|---|---|---|
| β | `LOADAWARE_BETA` | 1.0 | Exchange rate between cache benefit and relative load. A full cache hit is cancelled by an engine at $1/(2\beta)$ above the fleet mean |

One knob. **β = 0 reduces the policy to pure cache affinity** and is the ablation arm in the
Results — the arm that decides whether the load term does anything at all.

# Experimental setup

**Hardware and stack.** OpenShift, two worker nodes with one NVIDIA A10 (23 GB) each, serving
`Qwen/Qwen2.5-3B-Instruct` through the `vllm/vllm-stack` Helm chart pinned at 0.1.11. Baseline
arms run the stock router image `lmcache/lmstack-router:0.1.9.dev9-g37bafbcf5.d20260107`
against engines on the version-matched `vllm-openai:v0.3.9post2` (both LMCache 0.3.9post2).
Extended arms run an image built **in CI** from this repository's `Dockerfile` and pushed
SHA-tagged to Quay, so the measured artifact is always reproducible from the tree. The
development-loop ConfigMap overlay is never benchmarked, and each cell asserts its router
image against the expected label before recording anything.

**Workload.** One frozen dataset: a pool of **128 shared prefixes × 2048 tokens**, each request
drawing a prefix by **Zipf s = 0.9** and appending a 32-token unique suffix, 64 output tokens,
500 requests per seed, **20 seeds**. Seeds share the prefix pool and vary only sampling order
and suffixes. The pool is generated deterministically from `pool_seed=42` and pinned by a
SHA-256 manifest that every cell re-verifies, so a drifted workload fails loudly rather than
quietly invalidating a comparison. This is the *repetitive-prompt* profile — it stresses the
hit/miss ratio, which is the regime where placement matters.

**Operating point.** Requests arrive open-loop as a Poisson process at **16 req/s**. That rate
was chosen by a pilot and is the point where latency departs its plateau: TTFT p95 is 2.69× its
idle value and ITL p95 is 4.55×. This mattered more than we expected — an earlier sweep at
10.5 req/s produced `num_requests_waiting` = 0.00 on both engines, meaning nothing queued
anywhere and a load-aware router had no load to be aware of. **Choosing the operating point is
part of the experiment, not a preliminary to it.**

**Metrics.** Latency is client-observed and taken from the driver's per-request CSVs, with the
driver running **as a Job inside the cluster** so the measurement does not include a wide-area
network hop — TTFT,
inter-token latency, and end-to-end, at mean/p50/p90/p95/p99. The driver is the only
percentile-capable source: the router exposes average-latency gauges only, and engine-side
histograms start their clock at the engine and miss router overhead. Load imbalance is
busiest-over-idlest `vllm:num_requests_running`, scraped per engine at 5 s resolution. Cache
hit rate comes from `lmcache:lookup_hit_rate`, GPU utilization and power from DCGM.

**Statistics, pre-registered before the comparator ran.** One seed replay is **one
observation** (n = 20); per-request samples are queue-correlated and never treated as
independent evidence. Tests are one-sided **exact Wilcoxon signed-rank** on paired per-seed
differences, with effect sizes as the median relative difference and a seeded bootstrap 95% CI
over 10 000 resamples. Two co-primaries — TTFT p95 and load imbalance — give a Bonferroni
threshold of **0.025**. Throughout this report, a headline percentage is the *median over the
20 paired per-seed relative differences*, never a ratio of pooled means.

**Validity rules, also pre-registered.** Errored requests are excluded from latency statistics
but counted; a comparison is voided when error rates differ materially between arms (> 2× ratio
*and* > 1 pp absolute), with a 10% catastrophic ceiling. `analyze.py compare` **refuses** to
pair two runs whose recorded rate or workload manifest differ — "identical workload across
arms" is enforced by the tooling, not asserted in prose. Every cell restarts the engines so
each arm begins from an identical empty cache, then waits for both workers to re-register and
passes a registry probe before any measured request is sent.

# Results

## Headline

> **Status of this section.** The load-imbalance co-primary is settled and reported below. The
> **TTFT co-primary is not**, and is deliberately left open rather than filled with the number
> we happen to have — see *An instrument problem, not a result* below. The confirmatory re-run
> is pre-registered and pending.

| Co-primary | `kvaware` | `loadaware` β=0.5 | Median change | Verdict |
|---|---|---|---|---|
| Load imbalance | 2.630 | 1.262 | **−43.7%**, **20/20 seeds** | **p < 0.0001** — significant |
| TTFT p95 | — | — | — | **pending the in-cluster re-run** |

The β grid at the operating point, medians across 20 seeds each:

| Arm | Imbalance | vs `kvaware` |
|---|---|---|
| `kvaware` (baseline) | 2.630 | — |
| `loadaware` β = 0 (ablation) | 2.647 | null |
| `loadaware` β = 0.5 (**headline**) | 1.262 | **−43.7%**, 20/20 |
| `loadaware` β = 1.0 (shipped default) | 1.209 | −49.8%, 19/20 |
| `loadaware` β = 2.0 | 1.188 | −53.6%, 20/20 |

Imbalance falls monotonically with β and clears the Bonferroni-corrected threshold of 0.025 by
orders of magnitude. Note that it keeps falling past the headline arm: β is not being tuned to
the best imbalance number, it is being set where imbalance is bought at an acceptable price in
locality.

## An instrument problem, not a result

The pre-registered latency test was measured from a laptop over the public internet.
Reconstructing the path showed that **45–59% of the reported TTFT was network**, with a
per-cell offset larger than the effect being tested. The measurement was not precise enough to
answer its own question.

There were two ways out, and only one of them is legitimate:

- Switch to the engine-side TTFT metric, which shows β=0.5 improving latency ~9% at p=0.0053.
  **We do not report this as a result.** That metric was chosen *after* seeing the client-side
  null, which makes it exploratory by construction, and promoting it would be the same error as
  adding seeds until a p-value cooperates.
- **Fix the instrument and re-run the original test unchanged.** The driver now executes as an
  in-cluster Job, so per-request timestamps are taken inside the cluster with no wide-area
  network in the path — which is also what §3's per-request percentile requirement needs, and
  what a Prometheus histogram cannot provide.

The second is what the project did. The re-run is pre-registered — same primary, same
comparison, same test, same n, same stopping rule — and this section's latency row is filled
from it, whichever way it lands.

![Load balance across the two engines: what the policy actually changes.](../figures/fig6-load-balance.png)

## The ablation is the finding

| Arm | Median imbalance | vs `kvaware` |
|---|---|---|
| `kvaware` (baseline) | 2.630 | — |
| `loadaware` β = 0 | 2.647 | null — indistinguishable |
| `loadaware` β = 0.5 | 1.262 | **−43.7%**, 20/20 seeds |

With the load term switched off, the policy is statistically indistinguishable from the
baseline on imbalance *and* on latency. **The load term is the entire mechanism.** Rewriting the
router — the multi-instance lookup, the scoring path, the instance bridge — bought nothing on
its own; β bought everything.

This was pre-declared falsifiable: a β = 0 arm landing near 1.26 would have meant the
improvement came from some incidental difference in our implementation rather than from the
policy, and would have voided the headline. It did not. It landed at 2.647, on top of the
baseline's 2.630.

## Parameter sensitivity

**Imbalance falls monotonically with β, and keeps falling past the arm we ship.** Across
β ∈ {0, 0.5, 1.0, 2.0} the median imbalance runs 2.647 → 1.262 → 1.209 → 1.188 against the
baseline's 2.630. The returns flatten sharply after β = 0.5: the first half of the grid buys
1.39 of imbalance, the rest buys 0.07.

That shape is why β is not simply set to the largest value on the grid. Every increment buys
balance by diverting requests away from the engine holding their prefix, and past the knee it
is paying full prefills for imbalance improvements in the third decimal. β = 0.5 is where the
curve turns; β = 1.0 is the shipped default because it is the point where a full cache hit is
cancelled by an engine at 50% above the fleet mean, which is a defensible operating rule rather
than a fitted constant.

The latency side of this trade-off is exactly what the in-cluster re-run measures, and it is
not reported here on WAN-polluted data.

A caution on the *other* hit-rate metric: `lmcache:lookup_hit_rate` is scraped from the engines,
so it measures each engine against its **own** local cache rather than whether the router chose
the instance holding the KV. It saturates near 0.95 on every arm including round-robin and does
not discriminate between policies. Every hit-rate number in this report is the vLLM
prefix-cache counter.

![TTFT p95 against β at 16 req/s, with the cache-hit-rate trade-off. Note the hit rate is flat across this range — see the text.](../figures/fig7-beta-tradeoff.png)

## Resource cost

![Resource utilization by arm. Equal is the expected outcome: the policy changes *where* requests go, not how much work there is.](../figures/fig10-utilization.png)

The policy is not buying its result by spending more hardware. Both GPUs run at 82–93% SM
utilization on every arm and board power sits at 108–119 W per device throughout — which is
also the direct evidence for the compute-saturation argument in the Discussion. The router's own
CPU is **0.212, 0.213 and 0.214 core-seconds per second** for `kvaware`, β = 0 and β = 0.5:
scoring every endpoint and recomputing a fleet mean per request is, in practice, free at this
scale. Router memory is unchanged at ~1.02 GB.

One number here is a *result*, not a cost check. **KV-cache memory spread across the two engines
falls from 1.70× under `kvaware` to 1.18× at β = 0.5** — with the β = 0 ablation at 1.79×,
right beside the baseline. The policy is balancing the cache itself, not merely the request
count, which is the thing a load-balancer that ignored locality could not do.

Engine-side process CPU and RSS are **not available**: vLLM registers no process collector, so
those two series do not exist to be reported. Naming what is missing is part of the metric, and
`utilization.py` gates on series coverage rather than silently averaging over gaps.

## What we declined to claim

Two metrics were available that would have improved the story, and both were declined.

**Engine-side TTFT**, which shows β = 0.5 improving latency ~9% at p = 0.0053. It was examined
only *after* the client-side test returned a null, which makes it exploratory by construction.
Reporting it as the latency result would be metric substitution — the same error as adding
seeds until a p-value cooperates. It is named in the re-run's pre-registration as a secondary,
for diagnosis only.

**`itl_p95`**, which reached p = 0.0060 against a β = 0 arm in an earlier sweep but crossed the
significance line between replicate cells (0.0291 and 0.0570). A metric that changes verdict
between replicates of the same condition is measuring noise.

No seeds were added after any null appeared, at any operating point.

# Discussion

**Why latency did not move, and why that is coherent.** This system saturates on **compute**,
not memory. At the measured operating point the busiest engine ran 59–100 mean concurrent
requests with `num_requests_waiting` = 0.00, zero preemptions, and 0.0 ms of queue time.
Prefix caching makes concurrency nearly free in KV terms — roughly 530 KV tokens per in-flight
request against 1578-token prompts, measured on the load-gate probe — so against the
104,624-token pool, KV usage tops out near 0.70 and never exhausts. The baseline's pathology is therefore **decode-batch concentration**,
not queueing. Balancing in-flight requests fixes the concentration, which is exactly the metric
that moved; it does not fix a queue that was never there, which is exactly the metric that
did not.

**The honest shape of the contribution.** We changed how work is distributed, and we can prove
that with high confidence. We did not demonstrate that this makes the fleet faster at this
operating point. A reader deciding whether to adopt this policy should read the imbalance
result as the claim and the latency null as the boundary of it. Imbalance is not merely
cosmetic — it is the mechanism by which the fleet has headroom, and it is what determines
behaviour under a burst — but this experiment did not measure the burst.

**β used to be tied to absolute concurrency, and that was the design's sharpest flaw.** The
first version of the load term used raw in-flight counts, so the useful β moved with the offered
rate: two calibration probes at the *same* rate produced β = 0.034 and β = 0.013. A parameter
that has to be re-derived per deployment, from a probe that disagrees with itself, is not a
tunable — it is a liability.

The fix was to normalize load against the fleet mean the way the benefit term was already
normalized against prompt length, which made both terms dimensionless and β a pure exchange
rate. It also removed α outright: once the benefit is a fraction, a second weight on it is
redundant. **This is the one design change made in response to a measured weakness rather than
to a result**, and it is worth separating from the rest — it did not chase a p-value, it made
the knob mean something.

**Deduplicated-block load accounting, and why we passed.** NVIDIA's Dynamo KV-router accounts
for load in deduplicated blocks rather than requests. We measured the dedup factor in our own
workload (0.69 at 10.5 req/s, 0.45 at 7.5) and estimated the headroom at ~9% on imbalance and
~5% on hit rate. Adopting it requires per-worker block tracking plus a completion hook, which
would invalidate every run already recorded, and the gain only materializes under cache
scarcity — which our workload never reaches (KV usage max 33%, `num_requests_waiting` 0).
We document it as a limitation rather than a deficiency.

**Two measurement artifacts, both biasing against us.** Backend disconnects hit a path where
`on_request_complete` is skipped, drifting the router's own in-flight gauge by +4 to +7 on one
engine over a run. It biases *against* the extension and is ~10× smaller than the reported
effect, and a zero-failure cell reproduces the result. Separately, LMCache's KV registry has a
~40 s blind window after any router restart during which admissions are lost, and a prefix
first stored in that window stays invisible to the controller for the life of the engine
process. Both arms would then degrade to QPS routing and look identical *for the wrong reason*.
Every run in this report is gated on a registry probe that must pass before warm-up begins.

# Conclusion and future work

We extended LMCache's controller to report per-instance prefix-match information — closing an
acknowledged upstream TODO — and added a `loadaware` placement policy to the vLLM Production
Stack router that scores cache benefit against live load. Under a Zipfian shared-prefix
workload at the latency knee, it reduces load imbalance by **43.7%** (p < 0.0001, 20/20 paired
seeds), and an ablation shows the load term is the entire mechanism. The latency co-primary is
being re-measured on a fixed instrument rather than reported from data we know is dominated by
network noise.

The unexpected lesson was methodological. Our first sweep produced a plausible-looking 4.7%
latency improvement that was **entirely an artifact of the operating point**: nothing had
queued, so there was no load to be aware of, and what we had measured was residual cache
locality. Finding the knee, and gating each run on a measured degradation criterion rather than
on a counter that can fire without harming anyone, turned a flattering non-result into a
defensible one. The rate is not a parameter of the experiment; it *is* the experiment.

Three follow-ups, in order of value:

1. **Fleets larger than two.** The relative-load formula generalizes on paper — it is defined
   against the fleet mean, not against a peer — but every measurement here has n = 2 engines,
   where one engine above the mean forces exactly one below it. With three or more, the argmax
   can chase a single idle instance, and whether β needs to scale with fleet size is untested.
2. **Measure under cache scarcity.** Every effect we care about should be larger when the cache
   cannot hold the working set, and that is where deduplicated-block accounting would pay off.
3. **Measure under bursts.** Imbalance is a claim about headroom; a burst workload is where
   headroom converts into latency, and it is the most likely place for the latency null to
   become a latency win.

\newpage

# Appendix A — Code and data artifacts

Every figure and statistic in this report is recomputable from the repository with no cluster
and no GPU.

| Artifact | Location |
|---|---|
| Router + LMCache modifications | `patches/` (mirrors in-image paths) |
| Unit tests (168, all offline) | `tests/`, `benchmarks/test_*.py` |
| Workload generators + SHA-256 manifests | `benchmarks/workload_gen.py`; `workloads/manifest.json` (Zipfian), `workloads/novel/manifest.json` (no-reuse) |
| In-cluster replay Job | `benchmarks/bench_job.sh`, `in_pod.sh`, `verify_dataset.sh` |
| Utilization (§3) | `benchmarks/utilization.py` |
| Load driver, gates, collectors | `benchmarks/load_driver.py`, `load_gate.py`, `scarcity_gate.py`, `collectors/` |
| Statistics (Wilcoxon + bootstrap) | `benchmarks/analyze.py` |
| Figure generation | `benchmarks/plot_results.py` |
| Per-seed derived table | `results/summary-per-seed.csv` |
| Baseline justification (guidelines §2) | `docs/baseline-justification.md` |
| Requirements audit | `docs/requirements-audit.md` |

## Run provenance

| Arm | Run directory | Rate | Seeds |
|---|---|---|---|
| `kvaware` (baseline) | `results/20260805-005210-kvaware` | 16 | 20 |
| `loadaware` β = 0 (ablation) | `results/20260805-011148-loadaware-b0` | 16 | 20 |
| `loadaware` β = 0.5 (**headline**) | `results/20260805-013208-loadaware-b0.5` | 16 | 20 |
| `loadaware` β = 1.0 (shipped default) | `results/20260805-015202-loadaware-b1.0` | 16 | 20 |
| `loadaware` β = 2.0 | `results/20260805-021215-loadaware-b2.0` | 16 | 20 |
| `roundrobin` | `results/20260804-191644-roundrobin` | 16 | 3 |
| Earlier absolute-β sweeps (superseded) | `results/20260803-*`, `results/20260804-*` | 7.5–16 | 3–20 |

Runs before `20260805` used the pre-normalization policy (absolute load, with an α term) and a
laptop-side driver. They are kept because they are the evidence behind the design change, not
because they are comparable to the current arms.

Each run directory contains the per-request driver CSVs, the Prometheus scrapes, `dcgm.csv`,
and a `run.json` recording the arm, β, rate, workload profile, router image and image ID, git
commit, and the workload manifest with per-seed SHA-256 checksums.

## Reproducing

```bash
pip install -r requirements.txt
pytest benchmarks/ tests/ -q

python3 benchmarks/analyze.py compare \
  results/20260805-013208-loadaware-b0.5 results/20260805-005210-kvaware
python3 benchmarks/utilization.py report results/20260805-*
python3 benchmarks/plot_results.py results/20260805-0* \
  --cand loadaware-b0.5 --out docs/figures
```

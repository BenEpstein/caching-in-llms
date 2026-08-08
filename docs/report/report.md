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
posts a median TTFT p95 of **11.004 s** against **0.320 s** for cache-aware placement - **34×**,
produced entirely by the routing decision - with a vLLM prefix-cache hit rate of **0.682**
against 0.912.

The instructive part is *why*, and it is not that round-robin balances badly. **Round-robin is
better balanced than the cache-aware baseline** - imbalance 1.490 against `kvaware`'s 2.358 -
and still 34× slower. It equalises request *counts*, not *work*: a request sent to the engine
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

Our headline result is that this **cuts load imbalance by 48.1% (p < 0.0001, n = 20 paired
seeds)**, reproduced at 49.4% by an independent sweep two days later. The latency co-primary **returned a null (−2.7%, p = 0.1153) and we report it**: the
first attempt was measured over a wide-area network that contributed 45–59% of the number, so
rather than substitute a metric that flatters us, the instrument was rebuilt and the original
test re-run unchanged. It did not reach significance, because at this operating point the fleet
never queued - `vllm:num_requests_waiting` was zero in 284 of 284 scrapes - and with both
engines below saturation, better placement has no queueing delay to remove. The full sequence,
including that null, is reported in Results. The result that makes the mechanism credible is an
ablation: with the load term switched off (β = 0), the policy lands *on* the baseline - so the
improvement comes from the load term specifically, not from having rewritten the router.

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

The first term is the **cache-hit benefit**: the fraction of a request's prompt tokens that
instance already holds across its cache tiers. The second is its load penalty. The router
computes the score for **every** endpoint and takes the argmax, breaking ties by URL for
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

**`kvaware` is left byte-identical.** Three of the four patched files take pure additions — a
new enum member, a factory branch, and a new class. The fourth is not an addition and is
load-bearing: `parsers/parser.py` hard-codes `--routing-logic`'s `choices` list, so without a
one-line widening argparse rejects the flag and the router exits before the factory ever runs.
The baseline arm we measure against is the unmodified upstream code path, not our code with a
flag flipped.

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

## What each experiment tells us

Seven measurements, each answering one question. They are laid out here first so the sections
that follow read as a single argument rather than a list of numbers.

| # | The question it asks | What it answered |
|---|---|---|
| 1 | Is placement a cache policy at all? | Round-robin is **better balanced** than the cache-aware baseline and still **34× slower** (hit rate 0.682 against 0.912). Placement sets the fleet's effective hit rate — the lever everything else operates on |
| 2 | Does the policy actually redistribute work? | **Yes.** Imbalance 2.358 → 1.249, **−48.1%**, p < 0.0001 over 20 paired seeds. This is the claim |
| 3 | Does redistributing work make the fleet faster *here*? | **No.** TTFT p95 −2.7%, p = 0.1153 — a null, on the pre-registered primary. This is the boundary of the claim, not a footnote to it |
| 4 | Is the gain the load term, or just our rewrite? | **The load term.** At β = 0 the policy lands *on* the baseline (2.662 vs 2.358, p = 0.9734). Pre-declared falsifiable; it is what makes 2 credible |
| 5 | What does the knob cost, and where should it sit? | Imbalance falls monotonically in β while the hit rate falls 91.2% → 86.1%. At β = 2.0 the lost locality shows up as latency. That reversal locates the knee and makes β = 0.5 a defended optimum |
| 6 | Is the gain bought with more hardware? | **No.** GPU utilization, power and router CPU are flat across arms. One number is a result rather than a cost check: KV-cache spread falls 1.70× → 1.18×, so the policy balances the *cache*, not just request counts |
| 7 | Does any of it reach a user-visible objective? | Goodput, a declared secondary: **19.0% fewer** requests missing a 150 ms first token, p = 0.0021, with the β = 0 ablation null across the whole 50–400 ms sweep |
| 8 | Does it hold up when run again? | **Yes.** An independent seven-cell sweep reproduces the headline at **−49.4%** against −48.1%, ablation null again, and adds β = 0.25 — which finds the floor of the trend in 5 |

**The general picture.** 1 sets the scale of the lever, 2 shows we can pull it, 4 proves it is
the load term doing the pulling and 5 says how hard to pull. 6 says the pull is free and 7 says
it is visible from outside. 3 is where the picture stops: at this operating point the fleet
never queued, so redistribution had no queueing delay to remove. The contribution is a
mechanism, measured and bounded — not a speed-up we could not substantiate.

## Headline

> **Status of this section.** Both co-primaries are settled. The in-cluster confirmatory re-run
> has been executed and its verdicts are reported below, **including the one that did not go our
> way**: load imbalance is significant, TTFT p95 is a null. Neither was revised after the fact.

| Co-primary | `kvaware` | `loadaware` β=0.5 | Median change | Verdict |
|---|---|---|---|---|
| Load imbalance | 2.358 | 1.249 | **−48.1%**, CI [37.7%, 56.3%] | **p < 0.0001** - significant |
| TTFT p95 | - | - | −2.7%, CI [−4.3%, 15.4%] | **p = 0.1153** - **not significant** |

The TTFT row is the pre-registered primary and it did not reach significance. It is reported
here rather than replaced. The diagnosis is in *An instrument problem, not a result* below: at
the operating point used, `vllm:num_requests_waiting` was zero in **284 of 284 scrapes**, so the
fleet never queued and there was no queueing delay for better placement to remove.

A secondary metric from the same cells is reported alongside them:

| Secondary | Median change | Verdict |
|---|---|---|
| Goodput - missed requests at a 150 ms TTFT objective | **−19.0%**, CI [10.7%, 22.1%] | p = 0.0021 |
| Same, β = 0 ablation | −3.6% (wrong way) | p = 0.8058 - null |

Goodput is the fraction of requests *sent* whose first token arrived within the objective; the
denominator is requests sent, so an errored request counts as a miss rather than vanishing from
the metric. It is a secondary, not a co-primary: it was not pre-registered. It is reportable rather
than a threshold hunt because the objective is **swept, not pinned** - `fig12` draws 50–400 ms
and the arms separate across the whole range, so 150 ms is a reporting choice and not a
load-bearing threshold - and because the β = 0 ablation runs negative across that same range,
which no measurement artefact produces. It is reported *beside* the TTFT null, never in place
of it.

The β grid at the operating point, medians across 20 seeds each:

| Arm | Imbalance | vs `kvaware` |
|---|---|---|
| `kvaware` (baseline) | 2.358 | - |
| `loadaware` β = 0 (ablation) | 2.662 | −12.8%, **p = 0.9734** - null, wrong way |
| `loadaware` β = 0.5 (**headline**) | 1.249 | **−48.1%**, p < 0.0001 |
| `loadaware` β = 1.0 (shipped default) | 1.186 | −53.7%, p < 0.0001 |
| `loadaware` β = 2.0 | 1.099 | −53.9%, p < 0.0001 |

Imbalance falls monotonically with β across the tested grid and clears the Bonferroni-corrected
threshold of 0.025 by orders of magnitude. Note that it keeps falling past the headline arm: β is
not being tuned to the best imbalance number, it is being set where imbalance is bought at an
acceptable price in locality. A later sweep added a β = 0.25 cell and found the floor of that
trend — see *Parameter sensitivity*.

## The headline replicates

The whole grid was re-run on 2026-08-08 (02:39–05:02) as an independent seven-cell sweep at the
same rate, same frozen workload, same router and driver images, `results/gen3-7cell/`. Nothing
about the policy changed; only the cell set and the window did.

**The claim replicates, and so does the ablation.** `kvaware` 2.452 against β = 0.5's 1.272 is
a **−49.4% reduction, p < 0.0001** — within 1.3 points of the reported −48.1% — and β = 0 is
null again (p = 0.43), so in both sweeps the load term is the entire mechanism. The
prefix-cache hit rate reproduces its shape too: 91.2%, 90.5%, 88.0%, 86.9%.

**The re-run's latency reading is reported here as exploratory and is not a change to the
headline.** On the new sweep the TTFT p95 comparison returns 18.7% at p = 0.0107, where the
first returned a null. We are not promoting it, for the reason stated under *What we declined to
claim*: it was examined *after* a null, with no fresh pre-registration, which makes it
exploratory by construction no matter which way it points. Its own bootstrap interval agrees
that caution is warranted — [−3.2%, +29.2%] includes zero. The pre-registered latency result of
this report remains the null.

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

The second is what the project did. The re-run was pre-registered - same primary, same
comparison, same test, same n, same stopping rule - and the latency row above is filled from it.

**It landed as a null: −2.7%, p = 0.1153.** We report it.

The instrument was genuinely fixed. Moving the driver in-cluster cut the TTFT p10 floor from
240.6 ms to 96.8 ms while engine-side TTFT *rose*, so the non-engine term collapsed from
~226 ms to ~21 ms - from three to four times the effect under study, to well below it.[^wan] The
measurement can now answer its own question. The answer is that at this operating point there
is no latency effect to find.

The reason is visible in the instrumentation rather than inferred: `vllm:num_requests_waiting`
was zero in **284 of 284 scrapes** on every cache-aware arm. The fleet never queued, so there
was no queueing delay for better placement to remove. Placement changed *where* work ran, which
load imbalance measures directly, but with both engines below saturation that did not convert
into a faster first token.

This is a limitation of the operating point, not evidence against the policy - and the honest
form of that statement is the null above, not a metric chosen afterwards to replace it.

[^wan]: These two figures are the one exception to "every number here regenerates from committed
data". They were measured on the 2026-08-04 cells, which were pruned from the working tree and
are in git history. The retained WAN sweep supports the same argument on committed data: p10
157.4 → 101.9 ms, non-engine term 124.6 → 48.5 ms, and the 45–59% share above. See
`results/README.md`.

![Load balance across the two engines: what the policy actually changes.](../figures/fig6-load-balance.png){width=70%}

## The ablation is the finding

Against the baseline's 2.358, the ablation lands at 2.662 (null, p = 0.9734, and the wrong way)
while β = 0.5 lands at 1.249. With the load term switched off, the policy is statistically indistinguishable from the
baseline on imbalance *and* on latency. **The load term is the entire mechanism.** Rewriting the
router — the multi-instance lookup, the scoring path, the instance bridge — bought nothing on
its own; β bought everything.

This was pre-declared falsifiable: a β = 0 arm landing near 1.25 would have meant the
improvement came from some incidental difference in our implementation rather than from the
policy, and would have voided the headline. It did not. It landed at 2.662, on top of the
baseline's 2.358 - if anything slightly worse, which is the opposite of an implementation
artefact.

## Parameter sensitivity

**Imbalance falls monotonically with β once β is large enough to bind, and keeps falling past
the arm we ship.** Across β ∈ {0, 0.5, 1.0, 2.0} the median imbalance runs
2.662 → 1.249 → 1.186 → 1.099 against the baseline's 2.358. The returns flatten sharply after
β = 0.5: the first half of the grid buys 1.41 of imbalance, the rest buys 0.15.

**Below that threshold the policy is not a weaker version of itself — it is the baseline.** The
re-run added β = 0.25 and it does not balance at all (imbalance 3.218 against that sweep's
`kvaware` 2.452, not significant, p = 0.8988). Its prefix-cache hit rate says why: **0.9119,
indistinguishable from `kvaware`'s 0.9115 and the ablation's 0.9108** — it places requests
exactly where pure cache affinity would. The design's own arithmetic predicts this. A full cache
hit is cancelled at $r = 1/(2\beta)$, so β = 0.25 needs an engine **200% above the fleet mean**
before the load term can override a cached prefix, which on two engines is unreachable. The
curve has a floor, and a cell that did not exist when the formula was written found it.

That shape is why β is not simply set to the largest value on the grid. Every increment buys
balance by diverting requests away from the engine holding their prefix, and past the knee it
is paying full prefills for imbalance improvements in the third decimal. β = 0.5 is where the
curve turns; β = 1.0 is the shipped default because it is the point where a full cache hit is
cancelled by an engine at 50% above the fleet mean, which is a defensible operating rule rather
than a fitted constant.

The latency side of this trade-off is exactly what the in-cluster re-run measures, and it is
not reported here on WAN-polluted data.

**The cost side is measurable, and it is what turns the trade-off over at β = 2.0.** The vLLM
prefix-cache hit rate falls monotonically as β rises: 91.2% for `kvaware` and 91.3% at β = 0,
then 90.7%, 87.9% and 86.1% at β = 0.5, 1.0 and 2.0. Up to the knee that is nearly free —
β = 0.5 cuts imbalance 48.1% for half a percentage point of locality. Past it the exchange rate
worsens sharply, and at β = 2.0 the lost hits surface as latency: TTFT p95 rises to 336 ms
against the baseline's 320 ms, the only arm carrying the load term that lands worse than the
baseline it is trying to beat. That reversal is the reason the grid stops at β = 2.0 and the
reason β = 0.5 is a defended optimum rather than an arbitrary small number.

A caution on the *other* hit-rate metric: `lmcache:lookup_hit_rate` is scraped from the engines,
so it measures each engine against its **own** local cache rather than whether the router chose
the instance holding the KV. It saturates near 0.95 on every arm including round-robin and does
not discriminate between policies. Every hit-rate number in this report is the vLLM
prefix-cache counter.

![TTFT p95 against β at 16 req/s, against the cache-hit-rate cost. The hit rate falls monotonically as β rises, 91.2% to 86.1%, which is the mechanism behind the β = 2.0 latency reversal — see the text.](../figures/fig7-beta-tradeoff.png){width=58%}

## Resource cost

![Resource utilization by arm. Equal is the expected outcome: the policy changes *where* requests go, not how much work there is.](../figures/fig10-utilization.png){width=58%}

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

**The re-run's TTFT p95**, at p = 0.0107 where the first sweep returned a null. A significant
p-value found on the second look is the same object as a favourable metric found on the second
look; which way it points does not change what it is. It is reported under *The headline
replicates* as exploratory.

No seeds were added to any cell after a null, at any operating point, and no metric was
substituted for the pre-registered one. What was done once is a complete independent re-run of
the grid, reported in the order it happened — including the reading that would have flattered us.

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
workload (0.69 at 10.5 req/s) and estimated the headroom at ~9% on imbalance. It needs
per-worker block tracking plus a completion hook, would invalidate every recorded run, and only
pays under cache scarcity our workload never reaches (KV usage max 33%). A limitation, not a
deficiency.

**Two measurement artifacts, both biasing against us.** Backend disconnects skip
`on_request_complete`, drifting the router's in-flight gauge by +4 to +7 on one engine over a
run — against the extension, ~10× smaller than the reported effect, and a zero-failure cell
reproduces the result. Separately, LMCache's KV registry loses admissions for ~40 s after any
router restart, and a prefix first stored in that window stays invisible to the controller for
the life of the engine process; both arms then degrade to QPS routing and look identical *for
the wrong reason*. Every run here is gated on a registry probe that must pass before warm-up.

# Conclusion and future work

We extended LMCache's controller to report per-instance prefix-match information — closing an
acknowledged upstream TODO — and added a `loadaware` placement policy to the vLLM Production
Stack router that scores cache benefit against live load. Under a Zipfian shared-prefix
workload, it reduces load imbalance by **48.1%** (p < 0.0001, n = 20 paired seeds), and an
ablation shows the load term is the entire mechanism. **The latency co-primary returned a null**
(−2.7%, p = 0.1153) on a rebuilt instrument, and we report it: at this operating point the fleet
never queued, so there was no queueing delay for better placement to remove. A secondary metric
from the same cells - missed requests against a 150 ms TTFT objective - falls 19.0%
(p = 0.0021), with the β = 0 ablation null and running the wrong way.

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

## Run provenance

| Arm | Run directory | Rate | Seeds |
|---|---|---|---|
| `kvaware` (baseline) | `results/20260805-230541-kvaware` | 16 | 20 |
| `loadaware` β = 0 (ablation) | `results/20260806-002645-loadaware-b0` | 16 | 20 |
| `loadaware` β = 0.5 (**headline**) | `results/20260805-232541-loadaware-b0.5` | 16 | 20 |
| `loadaware` β = 1.0 (shipped default) | `results/20260805-234559-loadaware-b1.0` | 16 | 20 |
| `loadaware` β = 2.0 | `results/20260806-000626-loadaware-b2.0` | 16 | 20 |
| `roundrobin` (comparator) | `results/20260806-144135-roundrobin` | 16 | 20 |

These six cells are the evidence base for every **reported** number and figure, and
`scripts/reproduce.sh` regenerates all of it from exactly these directories.

The **independent re-run** behind *The headline replicates* is a seven-cell sweep in its own
directory, with its own per-seed summary table and its own figure set:

| Arm | Run directory (under `results/gen3-7cell/`) | Rate | Seeds |
|---|---|---|---|
| `kvaware` | `20260808-023919-kvaware` | 16 | 20 |
| `loadaware` β = 0 | `20260808-042133-loadaware-b0` | 16 | 20 |
| `loadaware` β = 0.25 | `20260808-040053-loadaware-b0.25` | 16 | 20 |
| `loadaware` β = 0.5 | `20260808-025932-loadaware-b0.5` | 16 | 20 |
| `loadaware` β = 1.0 | `20260808-031955-loadaware-b1.0` | 16 | 20 |
| `loadaware` β = 2.0 | `20260808-034018-loadaware-b2.0` | 16 | 20 |
| `roundrobin` | `20260808-044202-roundrobin` | 16 | 20 |

Figures in `docs/figures-gen3/`. It is kept separate rather than pooled with the six above:
pooling two windows would inflate `n` without the seeds being exchangeable, and the point of a
replication is that it was analysed on its own. `reproduce.sh` walks every
`summary-per-seed.csv` in the tree, so this sweep regenerates from committed data on the same
terms as the reported one.

The superseded **WAN sweep** is kept alongside them, because *An instrument problem, not a
result* is an argument about measurement and the measurement is its evidence:

| Arm | Run directory | Rate | Seeds |
|---|---|---|---|
| `kvaware` | `results/20260805-005210-kvaware` | 16 | 20 |
| `loadaware` β = 0 | `results/20260805-011148-loadaware-b0` | 16 | 20 |
| `loadaware` β = 0.5 | `results/20260805-013208-loadaware-b0.5` | 16 | 20 |
| `loadaware` β = 1.0 | `results/20260805-015202-loadaware-b1.0` | 16 | 20 |
| `loadaware` β = 2.0 | `results/20260805-021215-loadaware-b2.0` | 16 | 20 |

Same arms, same frozen workload, same offered rate - driven from a laptop over the public
internet instead of from inside the cluster. They are **not results**, and they are deliberately
excluded from `results/summary-per-seed.csv` so that one table means one instrument. They get
their own full figure set (`docs/figures-wan/`), whose underlying series `reproduce.sh`
regenerates and diffs on every run, so the instrument argument is as reproducible as the results
it justifies. As everywhere else here, the check is on the numbers behind the figures, not on
the PNG bytes.

Earlier generations still - the 2026-08-03 characterization sweeps at 7.5-16 req/s and the
2026-08-04 absolute-β cells, which used the pre-normalization policy with an α term - are in git
history rather than the working tree. `results/README.md` indexes what is here and what is not.

Each run directory contains the per-request driver CSVs, the Prometheus scrapes, `dcgm.csv`,
and a `run.json` recording the arm, β, rate, workload profile, router image and image ID, git
commit, and the workload manifest with per-seed SHA-256 checksums.

## Reproducing

```bash
pip install -r requirements.txt
pytest benchmarks/ tests/ -q

./scripts/reproduce.sh   # regenerates every statistic and figure below from the six runs

# ...or by hand. The headline pair, the utilization report over the five policy cells, and
# the figures. `roundrobin` is passed as --comparator, never positionally: it is the framing
# cell for fig12, not a point on the β grid.
python3 benchmarks/analyze.py compare \
  results/20260805-232541-loadaware-b0.5 results/20260805-230541-kvaware
python3 benchmarks/utilization.py report results/20260805-2* results/20260806-0*
python3 benchmarks/plot_results.py results/20260805-2* results/20260806-0* \
  --comparator results/20260806-144135-roundrobin \
  --cand loadaware-b0.5 --out docs/figures
```

---
title: "Load-Aware Prefix Routing for the vLLM Production Stack"
subtitle: "Placement as a cache policy in a distributed KV cache"
author: "Eliad Bazak and Ben Epstein · Ben-Gurion University"
date: "August 2026"
geometry: margin=2.5cm
fontsize: 10pt
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

An LLM server answers a request in two phases. In the *prefill* phase, the server reads the full
prompt and builds an attention state for each token. This state is the KV cache. In the *decode*
phase, the server makes the output tokens one by one. For a long prompt, the prefill is the
expensive part. The KV cache removes this cost for repeated content. An example with one server:
many requests share one system prompt of 2048 tokens, and each request adds a short question. The
first request pays the full prefill, the server keeps the KV state of the shared prefix, and each
later request reuses that state and pays only for its own question. On one server, the cache
policy answers one question: which data to keep when the memory is full. That question is
eviction.

A fleet changes this picture. Put two servers behind a router, each with its own cache. The
first request with a given prefix goes to server A, which pays the prefill and keeps the KV
state. Then a second request with the same prefix arrives. If the router sends it to server A,
the request hits the cache. If the router sends it to server B, the request misses, because
server B holds nothing for this prefix. Server B pays the full 2048-token prefill again. The
eviction policy on server A cannot help, because the request never reached server A. On a fleet, a new question therefore comes before eviction: which server holds the
cache that can give a hit? **Placement is a cache policy.** It is not only a load-balancing
detail.

Our measurements show the size of this effect. We sent the same workload through two routing
policies and measured the time to first token (TTFT) at the client. Round-robin placement ignores
the cache. It gives a median TTFT p95 of **11.004 s**. Cache-aware placement gives **0.320 s**.
This is a factor of **34**, and the routing decision causes all of it: the vLLM prefix-cache hit
rate is 0.682 for round-robin against 0.912 for cache-aware placement.

A second measurement completes the problem. Round-robin does not lose because it balances the
load badly. The load imbalance is the number of running requests on the busiest server divided by
the number on the most idle server, so a value of 1.0 is a perfect balance. Round-robin gives
1.490. The cache-aware `kvaware` router of the Production Stack gives 2.358. **Round-robin
balances better than the cache-aware baseline and is still 34 times slower.** It makes the number
of requests equal, not the work, because each request on the wrong server pays the full prefill
again.

The two goals therefore pull in opposite directions. Cache affinity alone concentrates the load:
all requests for a popular prefix go to the one server that holds it, while the other server
stays idle. Load balance alone destroys the locality. A good router must weigh both. The
upstream `kvaware` router weighs only cache affinity: it reads no load signal, and it cannot
compare servers, because the LMCache controller lookup names only the *first* server that holds
a prefix.

**This project adds the missing half.** It makes two changes:

1. **A multi-instance lookup in the LMCache controller.** The function `lookup()` now reports,
   for each server, how many prompt tokens that server already holds. The upstream code records
   the old one-server limit as a TODO.
2. **A `loadaware` routing policy in the Production Stack router.** The policy gives each server
   a score: the cache benefit minus a load penalty. One parameter, β, sets the exchange rate
   between the two. The router sends the request to the server with the best score.

The Results section supports four statements. Together they show that the optimization matters
and that it works:

- **It works.** The policy decreases the load imbalance by **48.1%** (p < 0.0001, n = 20 paired
  seeds). An independent repetition gives **49.4%**.
- **The mechanism is proven.** With the load term off (β = 0), the policy equals the baseline.
  Thus the load term, and not some other part of the new code, causes the improvement.
- **It is free.** GPU use, GPU power, throughput and router CPU are equal on all cache-aware
  arms. The policy also balances the KV-cache memory itself: its spread across the servers
  decreases from 1.70 times to 1.18 times.
- **A user can see it.** The latency medians improve (TTFT p95 from 320 ms to 295 ms, end-to-end
  p95 from 6.78 s to 5.96 s), and 19.0% fewer requests miss a first-token objective of 150 ms
  (p = 0.0021, a declared secondary metric).

We registered two co-primary metrics before the comparison ran: load imbalance and TTFT p95. The
imbalance result is significant. The paired TTFT test is not (−2.7%, p = 0.1153). We report this
null result, and the Results section explains it: the fleet never made a queue at this operating
point, so a better placement had no queueing delay to remove. Both changes are open upstream pull
requests (see the Conclusion).

## Related work

LMCache is the KV-cache layer of vLLM. It keeps prefixes in chunks across GPU memory, CPU memory
and disk. A controller records which server holds which chunk. LMCache can also move KV data
between servers. Its default eviction policy is LRU, and `POLICY_MAPPING` can change it. The
one-page baseline justification (`docs/baseline-justification.md`) gives the full argument for
this choice of baseline. The Production Stack supplies the router. Its stock policies are
`roundrobin`, `session`, `kvaware` and `prefixaware`.

The Dynamo KV-router of NVIDIA attacks the same problem in a different way. It counts the load in
deduplicated KV blocks instead of requests. We measured our workload against that design and did
not adopt it. The Discussion gives the reason.

# Extension design

This section gives the motivation and the technical description of each change. The changes add
code and do not modify the existing policies. Thus the baseline arm in every experiment is the
unchanged upstream code.

## Change 1: match information for each server

The function `KVController.lookup()` reads the token chunks of a request and resolves each chunk
through the registry. The registry gives the **first** server that holds that chunk. Which server
is first is only the iteration order of a dictionary. The upstream code records this limit:
*"TODO: improve the matching logic, return multi results."*

This limit blocks a load-aware router in two ways:

- **A router above this function cannot rank the servers.** To compare server A with server B,
  the router must know what *both* servers hold. The standard interface cannot give this data.
- **Replication alone cannot balance the load.** If a popular prefix is on both servers, the
  lookup still names one server only, and a standard KV-aware router sends all of the traffic to
  that one server. Thus replication and routing must be designed together.

We changed `lookup()`. It now gives the number of matched tokens for **each** server that holds
the prefix. The credit for each server is **continuous**: a server gets no more credit after its
first missing chunk. Thus a match is a real prefix that the server can use, not a count of
separated chunks. The change only adds code, and it keeps the same return shape for the callers
that exist.

## Change 2: the `loadaware` policy

The policy turns the placement decision into one score for each server $i$. The score adds what
the server can save (the cache benefit, from Change 1) and subtracts what the server costs now
(the load penalty):

$$\text{score}(i) = \frac{\text{matched\_tokens}(i)}{\text{prompt\_tokens}} - \beta \cdot \text{relative\_load}(i)
\qquad
\text{relative\_load}(i) = \frac{\text{load}(i) - \overline{\text{load}}}{\max(1, \overline{\text{load}})}$$

The router calculates the score for each server and selects the highest score. If two scores are
equal, the router selects by URL, so the result stays deterministic. Four decisions in this
design are important.

**Both terms are fractions, so β needs no unit from the deployment.** The benefit is a fraction
of *this prompt*. The load is a signed fraction of the mean of *this fleet*, and the router
calculates that mean again for each request. Thus β is an exchange rate between locality and
load. This makes β portable: the router measures its own scale and does not import a scale from a
different deployment.

**The cancellation point gives β a physical meaning.** With two servers, a load of $+r$ on one
server gives $-r$ on the other server, so the load difference is $2\beta r$. A full cache hit is
cancelled at $r = 1/(2\beta)$. At the default of β = 1.0, this is $r = 0.5$: a server with 50%
more load than the fleet mean stops attracting more cache hits. The limit of 1 in the denominator
is also necessary. Without it, a fleet mean of 0.1 turns one request in flight into a relative
load of 9.0, and the policy would react to noise at a load level that needs no balance.

**The load signal was already available, and the baseline ignored it.** The class `EngineStats`
scrapes `num_running_requests`, `num_queuing_requests` and `gpu_cache_usage_perc` for each
server, and gives them to each call of `route_request()`. The class `KvawareRouter` uses none of
them. Our score function uses them at exactly that point. There is no new collection path and no
new failure mode.

**The policy `kvaware` does not change.** Three of the four changed files only receive additions:
a new enum member, a new branch in the factory, and a new class. The fourth file is different and
is necessary. The file `parsers/parser.py` has a fixed list of values for `--routing-logic`.
Without one added value in that list, `argparse` refuses the option and the router stops before
the factory. Thus the baseline arm is the unchanged upstream code. It is not our code with a
different option.

A code review found one defect worth recording. It is not visible until a server restarts: the
bridge from `instance_id` to URL must refresh when a server returns with a new identifier, and
the router must give credit to the live identifier only. Without this, the placement silently
becomes least-loaded routing for the remaining life of the router.

## Tunable parameters

| Parameter | Env var | Default | Function |
|:----------|:-----------------|:--------|:---------------------------------------------|
| β | `LOADAWARE_BETA` | 1.0 | The exchange rate between the cache benefit and the relative load. A server at $1/(2\beta)$ above the fleet mean cancels a full cache hit |

The policy has one parameter. **A value of 0 makes the policy pure cache affinity.** This is the
ablation arm in the Results. It shows if the load term does anything.

# Experimental setup

This section gives the environment, the workload, the metrics and the statistical rules, so a
reader can repeat the runs and can judge which parts are portable.

**Vocabulary.** One **arm** is one router configuration (for example `loadaware` with β = 0.5).
One **seed** is one replay of the workload with its own request order. One **cell** is one arm
measured with all 20 seeds, and one **sweep** is a group of cells that ran as one campaign.

## The environment

| Layer | What we ran |
|:------------|:----------------------------------------------------------------------|
| Cluster | OpenShift (Kubernetes), namespace `cache-llm`, two GPU worker nodes |
| GPUs | 2 × NVIDIA A10, 24 GB each. One vLLM server on each GPU |
| GPU stack | NVIDIA GPU Operator. Its DCGM exporter gives each GPU measurement |
| Model | `Qwen/Qwen2.5-3B-Instruct`. The model is not gated. No token is necessary |
| Serving stack | `vllm/vllm-stack` Helm chart, version 0.1.11. LMCache 0.3.9post2 |
| Baseline images | `lmcache/lmstack-router:0.1.9.dev9-g37bafbcf5.d20260107` with servers on `vllm-openai:v0.3.9post2`. This is the official pair |
| Extended image | `quay.io/rhl193000/lmstack-router-loadaware`. CI builds it from the `Dockerfile` of this repository and adds the SHA tag |
| Load driver | `quay.io/rhl193000/bench-driver`. CI builds it from `Dockerfile.bench`. It runs as a Job in the cluster |
| Observability | Our Prometheus (`deploy/prometheus.yaml`) scrapes the servers and the router at 5 s. A collector polls the DCGM exporters into `dcgm.csv` |
| Offline reproduction | Python 3.10+, `pytest` and `matplotlib`. **197 tests** and each reported statistic run on a laptop with no GPU |
| CI | GitHub Actions: the unit tests, the upstream conformance tests, both images, and this PDF |

Two properties of this environment give the reproducibility. First, CI builds the router that we
measure from the tree, and we never measure a pod with manual changes. **Each cell compares its
router image with the expected label before it records data.** Second, the two arms differ in the
router only. The chart, the servers, the workload and the rate are the same.

## Workload and operating point

**Workload.** There is one frozen dataset. It has a pool of **128 shared prefixes of 2048
tokens**. Each request selects a prefix with a **Zipf distribution, s = 0.9**, then adds a unique
suffix of 32 tokens and asks for 64 output tokens. Each seed sends 500 requests. There are **20
seeds**. All seeds use the same pool. Only the order and the suffixes change. The program builds
the pool from `pool_seed=42`, and a SHA-256 manifest describes it. Each cell verifies this
manifest, so a changed workload stops the run instead of invalidating a comparison without a
warning. This is the *repetitive-prompt* profile: it stresses the hit/miss ratio, which is the
condition where placement matters.

**Operating point.** The requests arrive open-loop as a Poisson process at **16 req/s**. A pilot
selected that rate, because at that rate the latency leaves its plateau: the TTFT p95 is 2.69
times its idle value and the inter-token latency (ITL) p95 is 4.55 times its idle value. An
earlier sweep at 10.5 req/s gave `num_requests_waiting` = 0.00 on both servers. Nothing made a
queue, so a load-aware router had no load to see. **The selection of the operating point is a
part of the experiment.**

## Metrics

The driver measures the latency at the client. The data comes from the CSV file of each request.
The metrics are TTFT, inter-token latency and end-to-end latency, at mean, p50, p90, p95 and p99.
Only the driver can give these percentiles: the router exposes average gauges only, and the
histograms of the servers start at the server, so they miss the router time. The load imbalance
is `vllm:num_requests_running` of the busiest server divided by the most idle server. The hit
rate is the vLLM prefix-cache counter. The throughput comes from the request rate and the token
rate of the driver, for each seed. The GPU use and the GPU power come from DCGM.

## Statistics and validity rules, registered first

**Statistics.** We registered the rules before the comparison ran. One seed replay is **one
observation** (n = 20). The requests inside a seed are related through the queue, so we never use
them as independent data. The tests are one-sided **exact Wilcoxon signed-rank tests** on the
paired differences of each seed. The effect size is the median relative difference. The
confidence interval is a seeded bootstrap 95% CI with 10 000 resamples. There are two co-primary
metrics, TTFT p95 and load imbalance, so the Bonferroni threshold is **0.025**. Each headline
percentage in this report is the *median of the 20 paired relative differences*, never a ratio
of pooled means.

**Validity rules.** We also registered them first. We remove failed requests from the latency
statistics, but we count them. A comparison is not valid if the error rates of the two arms are
very different: the limit is a ratio of more than 2 *and* a difference of more than 1 percentage
point. The command `analyze.py compare` **refuses** to pair two runs with a different rate or a
different workload manifest. Thus the tools make the workload identical, and the report does not
only say it. Each cell restarts the servers, so each arm starts with an empty cache, waits for
both workers to register again, and passes a registry probe before the first measured request.

# Results

We compare the unchanged upstream baseline with the extension under the identical frozen
workload. There are eight measurements, and each one answers one question. The sections below
follow the order of this table, so they give one argument and not a list of numbers.

| # | The question | The answer |
|:--|:-----------------------|:-------------------------------------------------------|
| 1 | Is placement a cache policy? | **Yes.** Round-robin balances better than the baseline and is still 34 times slower (Introduction) |
| 2 | Does the policy move the work? | **Yes.** The imbalance goes from 2.358 to 1.249: **−48.1%**, p < 0.0001, n = 20 paired seeds. This is the claim |
| 3 | Is the gain from the load term or from the new code? | **From the load term.** At β = 0 the policy gives the baseline result (2.662 against 2.358, p = 0.9734) |
| 4 | Does the result repeat? | **Yes.** An independent seven-cell sweep gives **−49.4%**, and the ablation is null again |
| 5 | What is the cost of the parameter? | The hit rate goes from 91.2% to 86.1% across the grid. At β = 2.0 the lost locality becomes latency. The knee is at β = 0.5 |
| 6 | Does this make the fleet faster here? | **No.** TTFT p95 is −2.7%, p = 0.1153: a null result on the registered primary metric, and the limit of the claim |
| 7 | Is there an effect that a user sees? | **19.0% fewer** requests miss a first token at 150 ms (secondary, p = 0.0021). The ablation is null from 50 ms to 400 ms |
| 8 | Does the gain need more hardware? | **No.** GPU, power, throughput and router CPU do not change. The KV-cache spread goes from 1.70 to 1.18 times |

**The general picture.** Item 1 gives the size of the effect, item 2 shows that we can control
it, items 3 and 4 show that the mechanism is real and repeats, item 5 shows how much of the
parameter to use, and items 7 and 8 show that the gain is visible from outside and free. Item 6
shows where the picture stops: the contribution is a mechanism, we measured it, and we give its
limits.

## The headline: the policy halves the load imbalance

Both co-primary metrics are complete, from the confirmatory run in the cluster. We did not change
either verdict after the measurement.

| Co-primary | `kvaware` | `loadaware` β=0.5 | Median change | Verdict |
|:------------------|:----------|:-------------|:---------------------------|:-------------------------|
| Load imbalance | 2.358 | 1.249 | **−48.1%**, CI [37.7%, 56.3%] | **p < 0.0001**, significant |
| TTFT p95 | 320 ms | 295 ms | −2.7%, CI [−4.3%, 15.4%] | **p = 0.1153**, **not significant** |

The imbalance row is the claim of this report, far below the corrected threshold of 0.025. The
TTFT row is not significant. We give it here and we did not replace it; the section *The latency
co-primary* gives the diagnosis. Note that the arm columns are medians of all seeds, while the
change column is the median of the 20 paired differences. Only the paired value is the result.

![The load balance across the two servers, as mean requests in flight on the busiest and the most idle server. This is what the policy changes: the `kvaware` bars are far apart, the `loadaware` bars are close.](../figures/fig6-load-balance.png){width=50%}

## The ablation shows that the load term is the mechanism

The ablation arm runs the full new code with the load term off (β = 0). The baseline gives 2.358.
The ablation gives 2.662: a null result, p = 0.9734, and in the wrong direction. The arm at
β = 0.5 gives 1.249. Thus, when the load term is off, the policy and the baseline are
statistically the same, for the imbalance and for the latency. **The load term is the full
mechanism.** The new code alone, which includes the multi-instance lookup, the score path and the
identifier bridge, gives nothing by itself. The parameter β gives all of the improvement.

We declared this test before the measurement, and it could have failed: an arm at β = 0 near
1.25 would mean that the improvement comes from some difference in our code, and that result
would cancel the headline. The arm gave 2.662, a little above the baseline. That is the opposite
of an artefact of the code.

## The result repeats in an independent sweep

We ran the full grid again as an independent sweep of seven cells, with the same rate, the same
frozen workload and the same router and driver images. Only the set of cells and the time
changed. The data is in `results/gen3-7cell/`.

**The claim repeats, and the ablation repeats.** The value for `kvaware` is 2.452 and the value
for β = 0.5 is 1.272. This is a **decrease of 49.4%, p < 0.0001**, within 1.3 points of the
reported 48.1%. The arm at β = 0 is null again (p = 0.43). Thus the load term is the full
mechanism in both sweeps. The prefix-cache hit rate also has the same shape: 91.2%, 90.5%, 88.0%
and 86.9%. Two independent sweeps of 20 seeds that agree within 1.3 points are stronger evidence
than one sweep.

**The latency value of the second sweep is exploratory and does not change the headline.** In the
new sweep, the TTFT p95 comparison gives 18.7% at p = 0.0107. The first sweep gave a null result.
We do not use this value: we examined it *after* a null result and without a new registration,
so it is exploratory. Its bootstrap interval is [−3.2%, +29.2%], and this interval includes
zero. The registered latency result of this report is still the null result.

## Parameter sensitivity: the knee is at β = 0.5

This is the β grid at the operating point. The values are medians of 20 seeds.

| Arm | Imbalance | Against `kvaware` |
|:--------------------------------|:----------|:----------------------------------------|
| `kvaware` (baseline) | 2.358 | baseline |
| `loadaware` β = 0 (ablation) | 2.662 | −12.8%, **p = 0.9734**, null, wrong direction |
| `loadaware` β = 0.5 (**headline**) | 1.249 | **−48.1%**, p < 0.0001 |
| `loadaware` β = 1.0 (default) | 1.186 | −53.7%, p < 0.0001 |
| `loadaware` β = 2.0 | 1.099 | −53.9%, p < 0.0001 |

The imbalance decreases with β across the whole grid, but the returns decrease quickly after
β = 0.5: the first half of the grid gives 1.41 of imbalance, the remainder gives 0.15. Thus we do
not tune β for the best imbalance value. We set β at the point where the cost in locality is
acceptable.

**Below the knee the policy is not a weaker version of itself. It is the baseline.** The second
sweep added a cell at β = 0.25. That cell does not balance the load: its imbalance is 3.218
against 2.452 for `kvaware` in that sweep (p = 0.8988). Its prefix-cache hit rate gives the
reason. The rate is **0.9119, while `kvaware` gives 0.9115 and the ablation gives 0.9108.** These
values are the same, so the cell puts the requests exactly where pure cache affinity puts them.
The arithmetic of the design predicts this: a full cache hit is cancelled at $r = 1/(2\beta)$, so
at β = 0.25 the load term needs a server at **200% above the fleet mean** before it can override
a cached prefix, and two servers cannot reach that condition. A cell that did not exist when we
wrote the formula found the floor of the curve at the position that the formula gives.

**Above the knee the exchange rate turns bad, and at β = 2.0 the cost becomes latency.** The vLLM
prefix-cache hit rate decreases when β increases: 91.2% for `kvaware`, 91.3% at β = 0, then
90.7%, 87.9% and 86.1% at β = 0.5, 1.0 and 2.0. Below the knee this cost is very small: β = 0.5
buys 48.1% of the imbalance for one half of a percentage point of locality. Each further increase
moves requests away from the server that holds the prefix and pays full prefills for gains in the
third decimal. At β = 2.0 the lost hits appear as latency: the TTFT p95 increases to 336 ms
against the baseline of 320 ms. This is the only arm with a load term that is worse than the
baseline, it is the reason that the grid stops at β = 2.0, and it is the reason that β = 0.5 is a
defended optimum and not a small value that we selected. The default β = 1.0, where a server at
50% above the fleet mean cancels a full cache hit, is an operating rule and not a fitted
constant.

A warning about the *other* hit-rate metric: the counter `lmcache:lookup_hit_rate` comes from the
servers. It measures each server against its **own** local cache, not whether the router selected
the server that holds the KV data, and its value is near 0.95 on each arm, round-robin included.
Each hit-rate value in this report is therefore the vLLM prefix-cache counter.

![The TTFT p95 against β at 16 req/s, with the cost in the cache hit rate. The hit rate decreases when β increases, from 91.2% to 86.1%. This is the mechanism of the change of direction at β = 2.0.](../figures/fig7-beta-tradeoff.png){width=51%}

## The latency co-primary: a null result, measured correctly

The TTFT verdict has a history, and we report it in the order of the events. We measured the
first registered latency test from a laptop across the public internet. When we reconstructed the
path, the network gave **45% to 59% of the reported TTFT**. The offset of each cell was larger
than the effect under test, so the instrument was not accurate enough to answer its own question.
**We repaired the instrument instead of changing the metric.** The driver now runs as a Job in
the cluster, so the timestamps of each request come from inside the cluster and no wide-area
network is in the path. This is also necessary for per-request percentiles, which a Prometheus
histogram cannot give. We registered the second run before it ran, with the same metric,
comparison, test, n and stopping rule. The latency row in the headline table comes from that
run. **The result is a null result: −2.7%, p = 0.1153. We report
it.**

The repair is real. The driver in the cluster decreased the TTFT p10 floor from 240.6 ms to
96.8 ms while the TTFT of the servers increased, so the term that is not from the server
decreased from approximately 226 ms to approximately 21 ms.[^wan] Before the repair that term was
three to four times the effect under test. Now it is much smaller than the effect, so the
measurement can answer its own question.

The instrumentation also shows the reason, so we do not have to guess it. The counter
`vllm:num_requests_waiting` was zero in **284 of 284 scrapes** on each cache-aware arm. The fleet
never made a queue, so there was no queueing delay for a better placement to remove. Placement
changed *where* the work ran, and the load imbalance measures that directly. But both servers
stayed below saturation, so the change did not give a faster first token. This is a limit of the
operating point, not evidence against the policy. The correct statement is the null result above,
not a different metric that we select after the test.

[^wan]: These two values are the one exception to "each number here regenerates from committed
data". They come from earlier cells. Those cells are in the git history and not in the
tree. The WAN sweep that we keep gives the same argument from committed data: p10 157.4 ms to
101.9 ms, the term that is not from the server 124.6 ms to 48.5 ms, and the fraction of 45% to
59% above.

![The TTFT distribution for each policy, from p50 to p99. The bars are the seed medians of 20 seeds. Both `loadaware` arms are below the baseline at each percentile, but the spreads of the seeds overlap. This is the reason that the registered test gives a null result and not a latency improvement.](../figures/fig5-percentiles.png){width=68%}

## Goodput: the effect that a user sees

Goodput is the fraction of the requests that we *sent* whose first token arrived before an
objective. A failed request is a miss, so it does not disappear from the metric. We declared
goodput as a secondary metric. From the same cells as the headline:

| Secondary | Median change | Verdict |
|:-------------------------------------|:-----------------------------|:--------------|
| Goodput: missed requests at a TTFT objective of 150 ms | **−19.0%**, CI [10.7%, 22.1%] | p = 0.0021 |
| The same, β = 0 ablation | −3.6% (the wrong direction) | p = 0.8058, null |

We did not register goodput first, but it is not a search for a good threshold, for two reasons.
First, we **sweep** the objective instead of selecting one value: Figure 4 shows 50 ms to 400 ms,
and the arms are different across the full range. Second, the β = 0 ablation is negative across
that same range, and a measurement artefact does not produce that pattern. We give this metric
**beside** the TTFT null result, never instead of it.

![A secondary metric, not a claim. Goodput against the TTFT objective, from 50 ms to 400 ms. The upper shaded band shows what the load term gives against the baseline. The grey band below shows the loss when the load term is off. The policy `roundrobin` is the floor that has no cache data. The marker is at the **documented objective of 150 ms**, not at the best point of the sweep.](../figures/fig12-goodput.png){width=53%}

## Resource cost: the gain is free

The policy does not get its result with more hardware. The throughput is equal across the
cache-aware arms: 14.0 to 14.5 requests/s and 897 to 929 output tokens/s, as medians for each
seed. Round-robin completes 10.4 requests/s and 665 tokens/s at the same offered rate, so the
cache-blind arm loses throughput as well as latency. Both GPUs run at 82% to 93% SM use on each
arm, and the board power stays between 108 W and 119 W for each device. The CPU of the router is **0.212,
0.213 and 0.214 core-seconds for each second** for `kvaware`, β = 0 and β = 0.5, so the score of
each server and the new fleet mean for each request are free at this scale. The router memory
does not change. It stays at approximately 1.02 GB.

One value here is a *result* and not a cost. **The spread of the KV-cache memory across the two
servers decreases from 1.70 times with `kvaware` to 1.18 times at β = 0.5.** The ablation at
β = 0 gives 1.79 times, near the baseline value of 1.70. Thus the policy balances the cache
itself, not only the request count. A load balancer that ignores locality cannot do this.

The process CPU and the RSS of the servers are **not available**: vLLM has no process collector,
so those two series do not exist. A statement about the missing data is a part of the metric. The
program `utilization.py` stops if the series coverage is not sufficient. It does not calculate an
average across the gaps.

![The resource use for each arm. Equal values are the expected result. The policy changes *where* the requests go. It does not change the quantity of work.](../figures/fig10-utilization.png){width=51%}

## What we did not claim

Three values were available, and each one tells a better story. We claim none of them:

- **The TTFT of the servers** (β = 0.5 is better by approximately 9%, p = 0.0053). We examined it
  only *after* the null result at the client. That makes it exploratory. To report it as the
  latency result is a substitution of the metric.
- **The metric `itl_p95`** gave p = 0.0060 against a β = 0 arm in an earlier sweep. But it
  crossed the significance limit between two cells of the same condition (0.0291 and 0.0570). A
  metric that changes its verdict between two identical conditions measures noise.
- **The TTFT p95 of the second sweep** (p = 0.0107). A significant p-value that you find on the
  second examination is the same object as a good metric that you find on the second examination.

We did not add seeds after a null result, and we did not replace the registered metric. We ran
the full grid again one time and report it in the order of the events, including the value that
is good for us.

# Discussion

**Why the latency did not change, and why the balance still matters.** This system saturates on
**compute** and not on memory. At the operating point, the busiest server held 59 to 100
concurrent requests as a mean, `num_requests_waiting` was 0.00, there were no preemptions, and
the queue time was 0.0 ms. Prefix caching makes concurrency almost free for the KV data: each
request in flight holds approximately 530 KV tokens against prompts of 1578 tokens, the pool has
104,624 tokens, and the KV use stops near 0.70 and never becomes full. The problem of the
baseline is therefore **the concentration of the decode batch**, not a queue. A balance of the
requests in flight corrects the concentration, and that is exactly the metric that changed. It
cannot correct a queue that did not exist, and that is exactly the metric that did not change.

This gives the contribution its correct shape. We changed the distribution of the work, and we
prove that with high confidence. We did not show that this makes the fleet faster at this
operating point. Read the imbalance result as the claim, and read the latency null result as its
limit. The imbalance is not cosmetic. A balanced fleet has reserve capacity: no server sits near
saturation, so a burst of traffic meets headroom instead of a hot server. This experiment did not
measure such a burst; the goodput sweep in the Results shows the part of this value that is
visible without one.

**β used raw concurrency at first, and that was the largest defect of the design.** The first
version of the load term used the raw counts of requests in flight, so the correct β changed with
the offered rate. Two calibration probes at the *same* rate gave β = 0.034 and β = 0.013. A
parameter that you must calibrate again for each deployment is a risk, not a tunable parameter.
The correction divides the load by the fleet mean, in the same way that the benefit term already
divides by the prompt length. This made both terms fractions and made β an exchange rate. It
also removed a second weight, α, on the benefit term, because a benefit that is already a
fraction needs no weight of its own. **This is the one design change that came from a measured
weakness and not from a result.** It gave a meaning to the parameter.

**Load counted in deduplicated blocks, and why we did not use it.** The Dynamo KV-router of
NVIDIA counts the load in deduplicated blocks and not in requests. We measured the deduplication
factor of our workload: it is 0.69 at 10.5 req/s, and the possible gain is approximately 9% of
the imbalance. The design needs block data for each worker and a completion hook, so it would
make each recorded run invalid. It also gives a benefit only when the cache is too small, and our
workload never reaches that condition: the maximum KV use is 33%. This is a limit of our work,
not a defect.

**Two measurement artefacts. Both are against us.** First, a disconnection at the backend does
not call `on_request_complete`, so the gauge of the requests in flight moves by +4 to +7 on one
server during a run. This counts against the extension, and it is approximately 10 times smaller
than the reported effect. A cell with no failures gives the same result. Second, the KV registry
of LMCache loses admissions for approximately 40 s after each restart of the router. A prefix
stored in that time stays invisible to the controller for the full life of the server process,
and both arms then degrade to QPS routing and look the same for the wrong reason. Each run
therefore passes a registry probe before the warm-up.

# Conclusion and future work

We changed the LMCache controller so that it gives prefix-match data for each server, which
closes a TODO in the upstream code, and we added a `loadaware` placement policy to the Production
Stack router. With a Zipf workload of shared prefixes, the policy decreases the load imbalance by
**48.1%** (p < 0.0001, n = 20 paired seeds), an independent sweep gives **49.4%**, the ablation
shows that the load term is the full mechanism, and the gain has no measurable resource cost.
**The latency co-primary metric gives a null result** (−2.7%, p = 0.1153) on the repaired
instrument, and we report it: the fleet never made a queue at this operating point.

The unexpected lesson is about method. Our first sweep gave a latency improvement of 4.7%. The
value looked correct, and it was **fully an artefact of the operating point**: nothing made a
queue, so there was no load to see. The rate is not a parameter of the experiment. It **is** the
experiment.

**What went upstream, and what is ready.** The installation of the baseline showed a defect in
the chart: `vllm-stack` does not expose the reply port and the heartbeat port of the LMCache
controller (9001 and 9002) on the router Service. No server registers, each lookup fails, there
is no error message, and the KV-aware baseline silently becomes round-robin routing.
[production-stack#1029](https://github.com/vllm-project/production-stack/pull/1029) corrects the
chart, and each cell here applies the same patch before its first request. The two changes of
this project are also upstream PRs:
[production-stack#1035](https://github.com/vllm-project/production-stack/pull/1035) gives the
`loadaware` policy on the current upstream main, with 46 offline tests, and
[LMCache#4471](https://github.com/LMCache/LMCache/pull/4471) gives the multi-holder lookup, with
new tests. Both keep the existing interfaces: `kvaware` does not change, and `lookup()` keeps
its return shape for the callers that exist.

There are three follow-up tasks, in the order of their value:

1. **Fleets with more than two servers.** The formula uses the fleet mean, so it generalises on
   paper, but each measurement here uses two servers. With three or more servers, the selection
   can move to one idle server, and we did not test if β must change with the fleet size.
2. **Measurement with a cache that is too small.** Each effect here must become larger when the
   cache cannot hold the working set. Deduplicated-block accounting gives a benefit in that
   condition.
3. **Measurement with bursts.** A burst changes reserve capacity into latency. It is the most
   probable condition where the latency null result becomes an improvement.

\newpage

# Appendix A: code and data artifacts

You can compute each figure and each statistic in this report again from the repository. You
need no cluster and no GPU:

```bash
pip install -r requirements.txt && pytest benchmarks/ tests/ -q && ./scripts/reproduce.sh
```

| Artifact | Location |
|:--------------------------------|:-------------------------------------------------|
| Router and LMCache changes | `patches/` (the same paths as in the image) |
| Unit tests (197, all offline) | `tests/`, `benchmarks/test_*.py` |
| Workload generators, SHA-256 manifests | `benchmarks/workload_gen.py`, `workloads/manifest.json` |
| Load driver, cluster Job, gates, collectors | `benchmarks/load_driver.py`, `bench_job.sh`, `load_gate.py`, `collectors/` |
| Statistics (Wilcoxon, bootstrap), resource use | `benchmarks/analyze.py`, `utilization.py` |
| Figures, per-seed tables | `benchmarks/plot_results.py`, `results/*/summary-per-seed.csv` |
| Benchmark manual (guidelines §3) | `benchmarks/README.md` |
| Deployment values, cluster notes | `deploy/` |
| Baseline justification (guidelines §2) | `docs/baseline-justification.md` |

## Run provenance

Each cell ran at 16 req/s with 20 seeds against the same frozen workload.

| Sweep | Directory | Function |
|:-------|:------------------------|:----------------------------------------------|
| gen-2 | `results/gen2-confirmatory/` | **The reported results**, and `docs/figures/`. Six cells: `kvaware` (`20260805-230541`), β = 0 (`20260806-002645`), β = 0.5 (`20260805-232541`, headline), β = 1.0 (`20260805-234559`), β = 2.0 (`20260806-000626`), `roundrobin` (`20260806-144135`) |
| gen-3 | `results/gen3-7cell/` | The independent repetition. The same arms and β = 0.25. It has its own tables and `docs/figures-gen3/` |
| gen-1 | `results/gen1-wan/` | The superseded WAN sweep. **Not a result.** It is the evidence for *The latency co-primary* |

We do not pool gen-3 with gen-2: two time windows increase `n`, but the seeds are not
exchangeable. We keep gen-1 out of the reported table, so one table gives one instrument. The
file `results/README.md` gives an index of what is here and what is not.

Each run directory holds the driver CSV file of each request, the Prometheus scrapes, `dcgm.csv`,
and a `run.json` file. The `run.json` file records the arm, β, the rate, the workload profile, the
router image and its image ID, the git commit, and the SHA-256 checksum of each seed. The script
`reproduce.sh` reads each `summary-per-seed.csv` file in the tree. Thus all three sweeps
regenerate on the same terms. The script stops with an error if a number is different. Thus the
report cannot disagree with its data. To calculate the headline pair, the resource-use report and
the figures manually:

```bash
R=results/gen2-confirmatory; B=benchmarks
python3 $B/analyze.py compare $R/*-loadaware-b0.5 $R/*-kvaware
python3 $B/utilization.py report $R/20260805-2* $R/20260806-0*
python3 $B/plot_results.py $R/20260805-2* $R/20260806-0* \
  --comparator $R/*-roundrobin --cand loadaware-b0.5 --out docs/figures
```

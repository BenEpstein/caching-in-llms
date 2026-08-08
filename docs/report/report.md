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

A KV cache keeps the attention state of a prompt prefix. A later request with the same prefix
does not calculate that state again. On one server, the cache policy answers one question: which
data to keep. On a fleet of servers, a different question comes first. **Which server holds the
cache that can give a hit?** If the router sends a request to server B, the request cannot use a
prefix that is in the cache of server A. The eviction policy of server A does not help.

Placement is therefore a cache policy. It is not only a load-balancing detail. Our measurements
show how large this effect is. We used the same workload for two routing policies. Round-robin
placement gives a median TTFT p95 of **11.004 s**. Cache-aware placement gives **0.320 s**. This
is a factor of **34**. The routing decision causes all of this difference. The vLLM prefix-cache
hit rate is **0.682** for round-robin and 0.912 for cache-aware placement.

The reason is important. Round-robin does not balance the load badly. **Round-robin balances
better than the cache-aware baseline.** Its imbalance is 1.490. The imbalance of `kvaware` is
2.358. But round-robin is still 34 times slower. Round-robin makes the number of requests equal.
It does not make the work equal. If a request goes to the server that does not hold its prefix,
that server must calculate a full prefill of 2048 tokens. The counts are equal, but the locality
is lost. You must add load-awareness **to** cache-awareness. You must not replace one with the
other.

The baseline uses one half of this. The vLLM Production Stack has a `kvaware` router. This router
asks the LMCache controller which server holds the prefix of a request. Then it sends the request
to that server. This works, but it makes a new problem. Routing only by cache affinity
concentrates the load. A popular prefix is on one server. All requests for that prefix go to that
server. That server becomes busy while the other server stays idle. Cache affinity and load
balance pull in opposite directions. The standard router pulls in one direction only.

**This project adds the second direction.** The project gives two changes:

1. **A multi-instance lookup in the LMCache controller.** The standard `lookup()` gives only the
   *first* server that holds each chunk. The upstream code records this limit as a TODO. A router
   above this function cannot compare servers if the function tells it about one server only.
2. **A `loadaware` routing policy.** It gives a score to each server. The score uses the cache
   benefit and the live load. The router selects the server with the best score. One parameter
   controls the policy. The parameter has no unit.

**What we proved.** The policy decreases the load imbalance by **48.1%** (p < 0.0001, n = 20
paired seeds). An independent sweep two days later gives **49.4%**. An ablation shows that the
mechanism is correct. When the load term is off (β = 0), the policy gives the same result as the
baseline. The load term causes the improvement. The new code does not cause it. The policy costs
nothing that we can measure. The GPU use, the GPU power and the router CPU are equal on all arms.
The policy also balances the cache itself, not only the request counts. The spread of the KV
cache decreases from 1.70 times to 1.18 times. On a declared secondary metric, 19.0% fewer
requests miss a first-token objective of 150 ms (p = 0.0021).

**Where the claim stops.** The latency co-primary metric **gives a null result (−2.7%,
p = 0.1153). We report it.** The first measurement used a wide-area network. That network caused
45% to 59% of the number. We did not change to a metric that gives a better result. We repaired
the instrument and did the same test again. The result is not significant, because the fleet
never made a queue at this operating point. The counter `vllm:num_requests_waiting` was zero in
284 of 284 scrapes. Better placement cannot remove a queueing delay that does not exist.

## Related work

LMCache is the KV-cache layer of vLLM. It keeps prefixes in chunks across GPU memory, CPU memory
and disk. A controller records which server holds which chunk. LMCache can also move KV data
between servers. Its default eviction policy is LRU. You can change this policy through
`POLICY_MAPPING`. For the full analysis, refer to `docs/baseline-justification.md`. The
Production Stack gives the router. Its policies are `roundrobin`, `session`, `kvaware` and
`prefixaware`.

The Dynamo KV-router of NVIDIA solves the same problem in a different way. It counts the load in
deduplicated blocks. We measured our workload against that design. We did not use it. The
Discussion gives the reason.

# Extension design

## Change 1: match information for each server

The function `KVController.lookup()` reads the token chunks of a request. It resolves each chunk
through the registry. The registry gives the **first** server that holds that chunk. The order is
the iteration order of a dictionary. The upstream code records this limit: *"TODO: improve the
matching logic, return multi results."*

There are two results of this limit. Together they give the reason for this project:

- **A router above this function cannot rank the servers.** To compare server A with server B,
  the router must know what *both* servers hold. The standard interface cannot give this data.
- **Replication alone cannot balance the load.** If a popular prefix is on both servers, the
  lookup gives one server only. A standard KV-aware router then sends all of the traffic to that
  server. You must design replication and routing together.

We changed `lookup()`. It now gives the number of matched tokens for **each** server that holds
the prefix. The credit for a prefix is **continuous for each server**. A server gets no more
credit after its first missing chunk. Thus a match is a real prefix that the server can use. It is
not a count of separated chunks. The change only adds code. It keeps the same return shape for
the callers that exist.

## Change 2: the `loadaware` policy

$$\text{score}(i) = \frac{\text{matched\_tokens}(i)}{\text{prompt\_tokens}} - \beta \cdot \text{relative\_load}(i)
\qquad
\text{relative\_load}(i) = \frac{\text{load}(i) - \overline{\text{load}}}{\max(1, \overline{\text{load}})}$$

The first term is the **cache benefit**. It is the fraction of the prompt tokens that the server
already holds in its cache tiers. The second term is the **load penalty**. The router calculates
the score for each server. Then it selects the highest score. If two scores are equal, the router
selects by URL. This keeps the result deterministic. Four decisions in this design are important.

**Both terms are fractions. Thus β has no unit from the deployment.** The benefit is a fraction
of *this prompt*. The load is a signed fraction of the mean of *this fleet*. The router
calculates the mean again for each request. Thus β is an exchange rate between locality and load.
This makes β portable. The router measures its own scale. It does not use a scale from a
different deployment.

**The cancellation point gives the meaning of the sweep grid.** With two servers, a load of $+r$
on one server gives $-r$ on the other server. The load difference is $2\beta r$. A full cache hit
is cancelled at $r = 1/(2\beta)$. At the default of β = 1.0, this is $r = 0.5$. A server with 50%
more load than the fleet mean does not attract more cache hits. The limit of 1 in the denominator
is necessary. Without it, a fleet mean of 0.1 makes one request in flight a relative load of 9.0.
The policy would then react to noise at a load level that does not need a balance.

**The policy has one parameter, not two.** An earlier design had a second weight α on the benefit
term. The benefit is already a fraction. Only the *ratio* of the two weights sets the trade-off.
One parameter gives all of the behaviour of two parameters. It also makes the sweep grid smaller.

**The load signal was already available, and the baseline ignored it.** The class `EngineStats`
scrapes `num_running_requests`, `num_queuing_requests` and `gpu_cache_usage_perc` for each
server. It gives them to each call of `route_request()`. The class `KvawareRouter` uses none of
them. Our score function uses them at that point. There is no new collection path. There is no
new failure mode.

**The policy `kvaware` does not change.** Three of the four changed files only receive additions:
a new enum member, a new branch in the factory, and a new class. The fourth file is different and
is necessary. The file `parsers/parser.py` has a fixed list of values for `--routing-logic`. If
you do not add one value to that list, `argparse` refuses the option. The router then stops
before the factory. Thus the baseline arm is the unchanged upstream code. It is not our code with
a different option.

A review found one defect. The defect is not visible until a server restarts. The bridge from
`instance_id` to URL must refresh when a server returns with a new identifier. The router must
give credit to the live identifier only. Without this, the placement becomes least-loaded
routing. It stays in that condition for the remaining life of the router.

## Tunable parameters

| Parameter | Env var | Default | Function |
|:----------|:-----------------|:--------|:---------------------------------------------|
| β | `LOADAWARE_BETA` | 1.0 | The exchange rate between the cache benefit and the relative load. A server at $1/(2\beta)$ above the fleet mean cancels a full cache hit |

The policy has one parameter. **A value of 0 makes the policy pure cache affinity.** This is the
ablation arm in the Results. It shows if the load term does anything.

# Experimental setup

## The environment the project runs on

We produced each number in this report on one environment. This section gives that environment in
full. Thus a reader can see which parts are portable and which parts are ours.

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
measure from the tree. We never measure a pod with manual changes. **Each cell compares its
router image with the expected label before it records data.** Second, the two arms are different
in the router only. The chart, the servers, the workload and the rate are the same.

**Workload.** There is one frozen dataset. It has a pool of **128 shared prefixes of 2048
tokens**. Each request selects a prefix with a **Zipf distribution, s = 0.9**. Then it adds a
unique suffix of 32 tokens. Each request has 64 output tokens. Each seed sends 500 requests.
There are **20 seeds**. The seeds use the same pool. Only the order and the suffixes change. The
program makes the pool from `pool_seed=42`. A SHA-256 manifest holds the pool. Each cell verifies
this manifest. Thus a changed workload stops the run. It cannot make a comparison invalid without
a warning. This is the *repetitive-prompt* profile. It stresses the hit/miss ratio. Placement is
important in that condition.

**Operating point.** The requests arrive open-loop as a Poisson process at **16 req/s**. A pilot
selected that rate. At that rate the latency leaves its plateau. The TTFT p95 is 2.69 times its
idle value. The ITL p95 is 4.55 times its idle value. This was more important than we expected.
An earlier sweep at 10.5 req/s gave `num_requests_waiting` = 0.00 on both servers. Nothing made a
queue. A load-aware router had no load to see. **The selection of the operating point is a part
of the experiment.**

**Metrics.** The driver measures the latency at the client. The data comes from the CSV file of
each request. The metrics are TTFT, inter-token latency and end-to-end latency, at mean, p50,
p90, p95 and p99. Only the driver can give percentiles. The router gives average gauges only. The
histograms of the servers start at the server. They do not include the router time. The load
imbalance is `vllm:num_requests_running` of the busiest server divided by the most idle server.
The hit rate is the vLLM prefix-cache counter. The throughput comes from the request rate and
the token rate of the driver, for each seed. The GPU use and the GPU power come from DCGM.

**Statistics. We registered them before the comparison ran.** One seed replay is **one
observation** (n = 20). The samples of each request are related through the queue. We never use
them as independent data. The tests are one-sided **exact Wilcoxon signed-rank tests** on the
paired differences of each seed. The effect size is the median relative difference. The
confidence interval is a seeded bootstrap 95% CI with 10 000 resamples. There are two co-primary
metrics: TTFT p95 and load imbalance. Thus the Bonferroni threshold is **0.025**. Each headline
percentage in this report is the *median of the 20 paired relative differences*. It is never a
ratio of pooled means.

**Validity rules. We also registered them first.** We remove failed requests from the latency
statistics, but we count them. A comparison is not valid if the error rates of the two arms are
very different. The limit is a ratio of more than 2 *and* a difference of more than 1 percentage
point. The command
`analyze.py compare` **refuses** to pair two runs with a different rate or a different workload
manifest. Thus the tools make the workload identical. The report does not only say it. Each cell
restarts the servers. Thus each arm starts with an empty cache. Then it waits for both workers to
register again. It also passes a registry probe before the first measured request.

# Results

## What each experiment tells us

There are eight measurements. Each one answers one question. This table comes first. Thus the
sections below give one argument and not a list of numbers.

| # | The question | The answer |
|:--|:-----------------------|:-------------------------------------------------------|
| 1 | Is placement a cache policy? | **Yes.** Round-robin balances better than the baseline and is still 34 times slower. Its hit rate is 0.682 against 0.912 |
| 2 | Does the policy move the work? | **Yes.** The imbalance goes from 2.358 to 1.249. This is **−48.1%**, p < 0.0001, n = 20 paired seeds. This is the claim |
| 3 | Does this make the fleet faster here? | **No.** TTFT p95 is −2.7%, p = 0.1153. This is a null result on the registered primary metric. It is the limit of the claim |
| 4 | Is the gain from the load term or from the new code? | **From the load term.** At β = 0 the policy gives the baseline result (2.662 against 2.358, p = 0.9734) |
| 5 | What is the cost of the parameter? | The imbalance decreases with β. The hit rate decreases from 91.2% to 86.1%. At β = 2.0 the lost locality becomes latency. This gives the knee |
| 6 | Does the gain need more hardware? | **No.** The GPU, the power, the throughput and the router CPU do not change. The KV-cache spread also goes from 1.70 to 1.18 times. The policy balances the *cache* |
| 7 | Is there an effect that a user sees? | Secondary metric: **19.0% fewer** requests miss a first token at 150 ms, p = 0.0021. The ablation is null across the full sweep of 50 ms to 400 ms |
| 8 | Does the result repeat? | **Yes.** An independent seven-cell sweep gives **−49.4%**. The ablation is null again |

**The general picture.** Item 1 gives the size of the effect. Item 2 shows that we can control
it. Item 4 shows that the load term does the work. Item 5 shows how much to use. Item 6 shows
that the gain is free. Item 7 shows that it is visible from outside. Item 8 shows that it
repeats. Item 3 shows where the picture stops. The contribution is a mechanism. We measured it
and we give its limits. It is not a speed increase that we cannot prove.

## Headline

Both co-primary metrics are complete. We did the confirmatory run in the cluster. The verdicts
are below. This includes the verdict that is not good for us. We did not change either verdict
after the measurement.

| Co-primary | `kvaware` | `loadaware` β=0.5 | Median change | Verdict |
|:------------------|:----------|:-------------|:---------------------------|:-------------------------|
| Load imbalance | 2.358 | 1.249 | **−48.1%**, CI [37.7%, 56.3%] | **p < 0.0001**, significant |
| TTFT p95 | 320 ms | 295 ms | −2.7%, CI [−4.3%, 15.4%] | **p = 0.1153**, **not significant** |

The TTFT row is the registered primary metric. It is not significant. We give it here. We did not
replace it. The section *An instrument problem, not a result* gives the diagnosis. Note that the
two arm columns are medians of all seeds. The change column is the median of the 20 paired
differences. Only the paired value is the result.

![The TTFT distribution for each policy, from p50 to p99. The bars are the seed medians of 20 seeds. Both `loadaware` arms are below the baseline at each percentile. The spreads of the seeds overlap. This is the reason that the registered arm is a null result and not a latency improvement.](../figures/fig5-percentiles.png){width=88%}

We give one secondary metric from the same cells:

| Secondary | Median change | Verdict |
|:-------------------------------------|:-----------------------------|:--------------|
| Goodput: missed requests at a TTFT objective of 150 ms | **−19.0%**, CI [10.7%, 22.1%] | p = 0.0021 |
| The same, β = 0 ablation | −3.6% (the wrong direction) | p = 0.8058, null |

Goodput is the fraction of the requests that we *sent* whose first token arrived before the
objective. A failed request is therefore a miss. It does not disappear from the metric. Goodput
is a secondary metric. We did not register it first. But it is not a search for a good threshold.
There are two reasons. First, we **sweep** the objective. We do not select one value. Figure 2
shows 50 ms to 400 ms. The arms are different across the full range. Second, the β = 0 ablation
is negative across that same range. A measurement artefact does not give that result. We give
this metric **beside** the TTFT null result. We never give it instead of the null result.

![A secondary metric. It is not a claim. Goodput against the TTFT objective, from 50 ms to 400 ms. The upper shaded band shows what the load term gives against the baseline. The grey band below shows the loss when the load term is off. The policy `roundrobin` is the floor that has no cache data. The marker is at the **documented objective of 150 ms. It is not at the best point of the sweep**.](../figures/fig12-goodput.png){width=54%}

This is the β grid at the operating point. The values are medians of 20 seeds.

| Arm | Imbalance | Against `kvaware` |
|:--------------------------------|:----------|:----------------------------------------|
| `kvaware` (baseline) | 2.358 | baseline |
| `loadaware` β = 0 (ablation) | 2.662 | −12.8%, **p = 0.9734**, null, wrong direction |
| `loadaware` β = 0.5 (**headline**) | 1.249 | **−48.1%**, p < 0.0001 |
| `loadaware` β = 1.0 (default) | 1.186 | −53.7%, p < 0.0001 |
| `loadaware` β = 2.0 | 1.099 | −53.9%, p < 0.0001 |

The imbalance decreases with β across the grid. The result is far below the corrected threshold
of 0.025. The imbalance continues to decrease after the headline arm. Thus we do not tune β for
the best imbalance value. We set β at the point where the cost in locality is acceptable.

## The headline result repeats

We did the full grid again on 2026-08-08. This was an independent sweep of seven cells. It used
the same rate, the same frozen workload and the same router and driver images. The data is in
`results/gen3-7cell/`. The policy did not change. Only the set of cells and the time changed.

**The claim repeats. The ablation also repeats.** The value for `kvaware` is 2.452. The value for
β = 0.5 is 1.272. This is a **decrease of 49.4%, p < 0.0001**. It is within 1.3 points of the
reported 48.1%. The arm at β = 0 is null again (p = 0.43). Thus the load term is the full
mechanism in both sweeps. The prefix-cache hit rate also has the same shape: 91.2%, 90.5%, 88.0%
and 86.9%. Two independent sweeps of 20 seeds that agree within 1.3 points are stronger than one
sweep.

**The latency value of the second run is exploratory. It does not change the headline.** In the
new sweep, the TTFT p95 comparison gives 18.7% at p = 0.0107. The first sweep gave a null result.
We do not use this value. We examined it *after* a null result and without a new registration.
That makes it exploratory, and the direction of the result does not change this. Its bootstrap
interval is [−3.2%, +29.2%]. This interval includes zero. The registered latency result of this
report is still the null result.

## An instrument problem, not a result

We measured the first registered latency test from a laptop across the public internet. We
reconstructed the path. The network gave **45% to 59% of the reported TTFT**. The offset of each
cell was larger than the effect of the test. The measurement was not accurate enough to answer
its own question. There were two solutions: change to a metric that gives a better result, or
repair the instrument and do the same test again. **We repaired the instrument.** The driver now
runs as a Job in the cluster. Thus the timestamps of each request come from inside the cluster.
There is no wide-area network in the path. This is also necessary for the percentiles of each
request. A Prometheus histogram cannot give them. We registered the second run first. It has the
same primary metric, the same comparison, the same test, the same n and the same stopping rule.
The latency row above comes from that run. **The result is a null result: −2.7%, p = 0.1153. We
report it.**

The repair is real. The driver in the cluster decreased the TTFT p10 floor from 240.6 ms to
96.8 ms. At the same time the TTFT of the servers increased. Thus the term that is not from the
server decreased from approximately 226 ms to approximately 21 ms.[^wan] Before the repair it was
three to four times the effect of the test. Now it is much smaller than the effect. The
measurement can answer its own question. The answer is that there is no latency effect at this
operating point.

The instrumentation shows the reason. We do not calculate it. The counter
`vllm:num_requests_waiting` was zero in **284 of 284 scrapes** on each cache-aware arm. The fleet
never made a queue. Thus there was no queueing delay for better placement to remove. Placement
changed *where* the work ran. The load imbalance measures that directly. But both servers were
below saturation. Thus the change did not give a faster first token. This is a limit of the
operating point. It is not evidence against the policy. The correct statement is the null result
above. It is not a metric that we select after the test.

[^wan]: These two values are the one exception to "each number here regenerates from committed
data". They come from the cells of 2026-08-04. Those cells are in the git history and not in the
tree. The WAN sweep that we keep gives the same argument from committed data: p10 157.4 ms to
101.9 ms, the term that is not from the server 124.6 ms to 48.5 ms, and the fraction of 45% to
59% above.

![The load balance across the two servers. This is what the policy changes.](../figures/fig6-load-balance.png){width=50%}

## The ablation is the important result

The baseline gives 2.358. The ablation gives 2.662. This is a null result, p = 0.9734, and it is
in the wrong direction. The arm at β = 0.5 gives 1.249. Thus, when the load term is off, the
policy and the baseline are statistically the same. This is true for the imbalance and for the
latency. **The load term is the full mechanism.** The new code alone gives nothing. This includes
the multi-instance lookup, the score path and the identifier bridge. The parameter β gives all of
the improvement.

We declared this test before the measurement, and the test can fail. If the arm at β = 0 gave a
value near 1.25, the improvement would come from some difference in our code. It would not come
from the policy. That result would cancel the headline. The arm gave 2.662. This is above the
baseline value of 2.358. It is a little worse. That is the opposite of an artefact of the code.

## Parameter sensitivity

**The returns decrease quickly after β = 0.5.** In the grid above, the first half gives 1.41 of
imbalance. The remainder gives 0.15.

**Below that point the policy is not a weaker version of itself. It is the baseline.** The second
run added a cell at β = 0.25. That cell does not balance the load. Its imbalance is 3.218 against
2.452 for `kvaware` in that sweep (p = 0.8988). Its prefix-cache hit rate gives the reason. The
rate is **0.9119. The rate of `kvaware` is 0.9115 and the rate of the ablation is 0.9108.** These
values are the same. Thus the cell puts the requests where pure cache affinity puts them. The
arithmetic of the design gives this result. A full cache hit is cancelled at $r = 1/(2\beta)$. At
β = 0.25 the load term needs a server at **200% above the fleet mean** before it can override a
cached prefix. Two servers cannot give that condition. A cell that did not exist when we wrote
the formula found the floor of the curve at the position that the formula gives.

That shape is the reason that we do not use the largest value in the grid. Each increase gives
balance because it moves requests away from the server that holds the prefix. After the knee, it
pays for full prefills to get gains in the third decimal. The value β = 0.5 is at the knee. The
default value is β = 1.0. At that value, a server at 50% above the fleet mean cancels a full
cache hit. This is an operating rule and not a fitted constant.

**We can measure the cost. The cost changes the trade-off at β = 2.0.** The vLLM prefix-cache hit
rate decreases when β increases: 91.2% for `kvaware`, 91.3% at β = 0, then 90.7%, 87.9% and 86.1%
at β = 0.5, 1.0 and 2.0. Below the knee this cost is very small. The value β = 0.5 decreases the
imbalance by 48.1% for one half of a percentage point of locality. Above the knee the exchange
rate becomes much worse. At β = 2.0 the lost hits become latency. The TTFT p95 increases to
336 ms. The baseline is 320 ms. This is the only arm with a load term that is worse than the
baseline. That change of direction is the reason that the grid stops at β = 2.0. It is also the
reason that β = 0.5 is a defended optimum and not a small value that we selected.

A warning about the *other* hit-rate metric: the counter `lmcache:lookup_hit_rate` comes from the
servers. It measures each server against its **own** local cache. It does not measure if the
router selected the server that holds the KV data. Its value is near 0.95 on each arm. This
includes round-robin. Each hit-rate value in this report is the vLLM prefix-cache counter.

![The TTFT p95 against β at 16 req/s, with the cost in the cache hit rate. The hit rate decreases when β increases, from 91.2% to 86.1%. This is the mechanism of the change of direction at β = 2.0.](../figures/fig7-beta-tradeoff.png){width=52%}

## Resource cost

The policy does not get its result with more hardware. The throughput is equal across the
cache-aware arms: 14.0 to 14.5 requests/s and 897 to 929 output tokens/s, as medians for each
seed. Round-robin completes 10.4 requests/s and 665 tokens/s at the same offered rate. Thus the
cache-blind arm loses throughput as well as latency. Both GPUs run at 82% to 93% SM use on each
arm. The board power stays between 108 W and 119 W for each device. This is also the direct
evidence for the compute-saturation argument in the Discussion. The CPU of the router is **0.212,
0.213 and 0.214 core-seconds for each second** for `kvaware`, β = 0 and β = 0.5. Thus the score
of each server and the new fleet mean for each request are free at this scale. The router memory
does not change. It stays at approximately 1.02 GB.

One value here is a *result* and not a cost. **The spread of the KV-cache memory across the two
servers decreases from 1.70 times with `kvaware` to 1.18 times at β = 0.5.** The ablation at
β = 0 gives 1.79 times. This is at the baseline. Thus the policy balances the cache itself. It
does not balance the request count only. A load balancer that ignores locality cannot do this.

The process CPU and the RSS of the servers are **not available**. vLLM has no process collector.
Thus those two series do not exist. A statement about the missing data is a part of the metric.
The program `utilization.py` stops if the series coverage is not sufficient. It does not
calculate an average across the gaps.

![The resource use for each arm. Equal values are the expected result. The policy changes *where* the requests go. It does not change the quantity of work.](../figures/fig10-utilization.png){width=52%}

## What we did not claim

Three values were available. Each one gives a better story. We claim none of them:

- **The TTFT of the servers** (β = 0.5 is better by approximately 9%, p = 0.0053). We examined it
  only *after* the null result at the client. That makes it exploratory. To report it as the
  latency result is a substitution of the metric.
- **The metric `itl_p95`** gave p = 0.0060 against a β = 0 arm in an earlier sweep. But it crossed
  the significance limit between two cells of the same condition (0.0291 and 0.0570). A metric
  that changes its verdict between two identical conditions measures noise.
- **The TTFT p95 of the second run** (p = 0.0107). A significant p-value that you find on the
  second examination is the same object as a good metric that you find on the second examination.

We did not add seeds to a cell after a null result. We did not replace the registered metric. We
did the full grid again one time. We report it in the order of the events. This includes the
value that is good for us.

# Discussion

**Why the latency did not change, and why this is correct.** This system saturates on **compute**
and not on memory. At the operating point, the busiest server had 59 to 100 concurrent requests
as a mean. The counter `num_requests_waiting` was 0.00. There were no preemptions. The queue time
was 0.0 ms. Prefix caching makes concurrency almost free for the KV data. There are approximately
530 KV tokens for each request in flight, against prompts of 1578 tokens. The pool has 104,624
tokens. Thus the KV use stops near 0.70 and never becomes full. The problem of the baseline is
therefore **the concentration of the decode batch**. It is not a queue. A balance of the requests
in flight corrects the concentration. That is the metric that changed. It does not correct a
queue that did not exist. That is the metric that did not change.

**The correct shape of the contribution.** We changed the distribution of the work. We can prove
this with high confidence. We did not show that this makes the fleet faster at this operating
point. Read the imbalance result as the claim. Read the latency null result as its limit. The
imbalance is not only cosmetic. It is the mechanism that gives the fleet its reserve capacity.
But this experiment did not measure a burst that uses that capacity.

**β used raw concurrency, and that was the largest defect of the design.** The first version of
the load term used the raw counts of requests in flight. Thus the correct β changed with the
offered rate. Two calibration probes at the *same* rate gave β = 0.034 and β = 0.013. A parameter
that you must calculate again for each deployment is not a tunable parameter. It is a risk. This
is worse when the probe does not agree with itself. The correction was to divide the load by the
fleet mean. The benefit term already used the prompt length in the same way. This made both terms
fractions. It made β an exchange rate. It also removed α. **This is the one design change that we
made because of a measured weakness and not because of a result.** It did not follow a p-value.
It gave a meaning to the parameter.

**Load counted in deduplicated blocks, and why we did not use it.** The Dynamo KV-router of
NVIDIA counts the load in deduplicated blocks and not in requests. We measured the deduplication
factor of our workload. It is 0.69 at 10.5 req/s. We calculated the possible gain at
approximately 9% of the imbalance. The design needs block data for each worker and a completion
hook. It would make each recorded run invalid. It gives a benefit only when the cache is too
small. Our workload never gets to that condition. The maximum KV use is 33%. This is a limit of
our work. It is not a defect.

**Two measurement artefacts. Both are against us.** First, a disconnection at the backend does
not call `on_request_complete`. Thus the gauge of the requests in flight moves by +4 to +7 on one
server during a run. This is against the extension. It is approximately 10 times smaller than the
reported effect. A cell with no failures gives the same result. Second, the KV registry of
LMCache loses admissions for approximately 40 s after each restart of the router. A prefix that
the system stores in that time stays invisible to the controller. It stays invisible for the full
life of the server process. Both arms then use QPS routing and look the same for the wrong
reason. Each run here uses a probe. The probe must pass before the warm-up.

# Conclusion and future work

We changed the LMCache controller. It now gives prefix-match data for each server. This closes a
TODO in the upstream code. We also added a `loadaware` placement policy to the Production Stack
router. The policy gives a score to the cache benefit against the live load. With a Zipf workload
of shared prefixes, the policy decreases the load imbalance by **48.1%** (p < 0.0001, n = 20
paired seeds). An independent sweep gives **49.4%**. There is no cost that we can measure in GPU
use, GPU power or router CPU. An ablation shows that the load term is the full mechanism. A
declared secondary metric, the missed requests at a TTFT objective of 150 ms, decreases by 19.0%
(p = 0.0021). **The latency co-primary metric gives a null result** (−2.7%, p = 0.1153) on the
repaired instrument. We report it. The fleet never made a queue at this operating point. Thus
better placement had no queueing delay to remove.

The unexpected lesson is about method. Our first sweep gave a latency improvement of 4.7%. The
value looked correct. It was **fully an artefact of the operating point**. Nothing made a queue.
Thus there was no load to see. The rate is not a parameter of the experiment. It **is** the
experiment.

**What went upstream, and what is ready.** The installation of the baseline showed a defect in the
chart. The chart `vllm-stack` does not expose the reply port and the heartbeat port of the
LMCache controller (9001 and 9002) on the router Service. Thus no server registers and each
lookup fails. There is no error message. The KV-aware baseline becomes round-robin routing
without a warning. This would make both arms of this experiment look the same for the wrong
reason. We opened
[production-stack#1029](https://github.com/vllm-project/production-stack/pull/1029) to correct
the chart. Each cell here applies the same patch before its first request. We also wrote the
policy for upstream use. The policy `kvaware` does not change. The policy `loadaware` is a new
enum member, a new factory branch and a new class. The controller change keeps the return shape
for the callers that exist.

There are three follow-up tasks. They are in the order of their value:

1. **Fleets with more than two servers.** The formula uses the fleet mean and not a second
   server. Thus it generalises on paper. But each measurement here uses two servers. With two
   servers, one server above the mean puts one server below the mean. With three or more servers,
   the selection can move to one idle server. We did not test if β must change with the size of
   the fleet.
2. **Measurement with a cache that is too small.** Each effect here must become larger when the
   cache cannot hold the working set. Deduplicated-block accounting gives a benefit in that
   condition.
3. **Measurement with bursts.** A burst changes reserve capacity into latency. It is the most
   probable condition where the latency null result becomes an improvement.

\newpage

# Appendix A: code and data artifacts

You can calculate each figure and each statistic in this report again from the repository. You do
not need a cluster and you do not need a GPU:

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
| Deployment values, cluster notes | `deploy/` |
| Baseline justification (guidelines §2) | `docs/baseline-justification.md` |

## Run provenance

Each cell ran at 16 req/s with 20 seeds against the same frozen workload.

| Sweep | Directory | Function |
|:-------|:------------------------|:----------------------------------------------|
| gen-2 | `results/gen2-confirmatory/` | **The reported results**, and `docs/figures/`. Six cells: `kvaware` (`20260805-230541`), β = 0 (`20260806-002645`), β = 0.5 (`20260805-232541`, headline), β = 1.0 (`20260805-234559`), β = 2.0 (`20260806-000626`), `roundrobin` (`20260806-144135`) |
| gen-3 | `results/gen3-7cell/` | The independent repetition, two days later. The same arms and β = 0.25. It has its own tables and `docs/figures-gen3/` |
| gen-1 | `results/gen1-wan/` | The superseded WAN sweep. **Not a result.** It is the evidence for *An instrument problem* |

We do not pool gen-3 with gen-2. Two different time windows increase `n`, but the seeds are not
exchangeable. We also keep gen-1 out of the reported table. Thus one table gives one instrument.
The earlier generations are in the git history. The file `results/README.md` gives an index of
what is here and what is not.

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

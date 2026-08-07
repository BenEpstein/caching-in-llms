# Load-Aware Prefix Routing for the vLLM Production Stack

BGU final project. We extend the [vLLM Production Stack](https://github.com/vllm-project/production-stack)
router with a **`loadaware`** placement policy that scores KV-cache-hit benefit against live
instance load, using per-instance prefix-match information we added to
[LMCache](https://github.com/LMCache/LMCache)'s controller.

In a distributed KV cache, **placement is the cache policy**: the router decides which
instance's cache is even eligible to hit, so it sets the fleet's effective hit rate.

And the lever is large. Round-robin placement against the same cache is **better balanced**
than the cache-aware baseline - imbalance 1.49 against `kvaware`'s 2.36 - and still an order of
magnitude slower, because it equalises request *counts*, not *work*: a request sent to the
engine that does not hold its prefix pays a full prefill. Balanced counts, ruined locality.
That is why load-awareness has to be added **on top of** cache-awareness rather than
substituted for it. (`results/20260806-144135-roundrobin`, n=20, offered rate 16 - a descriptive
cell, not a hypothesis test.)

```
score(instance) = matched_tokens / prompt_tokens  −  β · relative_load(instance)

relative_load = (load − fleet_mean) / max(1, fleet_mean)
```

Both terms are dimensionless — a fraction of *this prompt* against a fraction of *this fleet's*
mean — so **β is a pure exchange rate that carries no unit from the deployment**. The router
recomputes the fleet mean per request instead of inheriting a constant from whoever tuned it
last, which is what makes β portable across offered rates.

## Result

Measured on 2×A10 (OpenShift), Qwen2.5-3B-Instruct, one frozen Zipfian shared-prefix workload,
20 seeds per arm, replayed **from inside the cluster** so no wide-area network sits inside the
latency numbers.

| Co-primary | Status |
|---|---|
| **Load imbalance** | **Settled.** `loadaware` β=0.5 cuts imbalance **48.1%** vs `kvaware` (CI [37.7%, 56.3%]), **18 of 20 seeds**, p < 0.0001 |
| **TTFT p95** | **Settled as a null.** 2.7% median reduction, CI [−4.3%, 15.4%], p = 0.115 - no latency effect at this operating point |

The ablation is what makes the mechanism credible: **β=0 does not move imbalance** (2.662
against the baseline's 2.358 - if anything slightly worse, p = 0.9734), while β=0.5 sits at
1.249. The load term is the entire mechanism - the routing rewrite on its own does nothing.
That was pre-declared falsifiable before the comparator ran.

> **The latency null is the pre-registered result, not a fallback.** The original TTFT test was
> measured from a laptop, and 45–59% of that number turned out to be laptop-to-cluster network,
> with a per-cell offset larger than the effect. Rather than switch to the engine-side metric
> that happens to look better - chosen *after* seeing the null, so exploratory by construction - > the instrument was fixed: the driver moved in-cluster and the originally pre-registered test
> was re-run unchanged ([#31](https://github.com/BenEpstein/caching-in-llms/issues/31)). It came
> back null. Goodput against a 150 ms SLO, a reported secondary, does move: **19.0% fewer misses**
> (CI [10.7%, 22.1%], p = 0.002).

Provenance: `results/20260805-230541-kvaware`, `results/20260806-002645-loadaware-b0`,
`results/20260805-232541-loadaware-b0.5`. Every number above is recomputable with no cluster - see [Verify without a cluster](#verify-without-a-cluster).

## What we changed upstream

Three files, all resident in the **router pod** (both `vllm_router` and the LMCache
`cache_controller` are installed there as plain Python). `patches/` holds our modified copies
**mirroring their path inside the image** under `/opt/venv/lib/python3.12/site-packages/`, so
the tree the §6 image `COPY`s and the tree the tests import are the same bytes:

| File | Change | Ticket |
|---|---|---|
| `lmcache/v1/cache_controller/controllers/kv_controller.py` | Multi-instance lookup: `lookup()` reports per-instance matched-token counts for every holder, not just `kv_pool[key][0]` | [#4](https://github.com/BenEpstein/caching-in-llms/issues/4) |
| `vllm_router/routers/routing_logic.py` | `loadaware` placement policy: `LOADAWARE` enum + factory branch + a `LoadAwareRouter` scoring every endpoint. Additions only - `KvawareRouter` is byte-identical | [#5](https://github.com/BenEpstein/caching-in-llms/issues/5) |
| `vllm_router/parsers/parser.py` | One-line widening of `--routing-logic`'s hard-coded `choices` list to accept `loadaware`. Without it argparse rejects the flag and the router exits before the factory runs | [#5](https://github.com/BenEpstein/caching-in-llms/issues/5) |

Each file started as a verbatim copy from the router image `Dockerfile` pins by digest
(lmcache 0.3.9post2), so `git diff` against the stock file is the real diff, and every change
carries a `LOADAWARE PATCH` comment.

## Repo layout

| Path | What it is |
|---|---|
| `patches/` | Our modifications to the router and LMCache, mirroring their in-image paths |
| `tests/` | Unit tests for the two changes; loads `patches/` directly with `lmcache` stubbed |
| `benchmarks/` | Workload generators, load driver, in-cluster Job, gates, collectors, analysis, plots |
| `docs/figures/` | Every figure in the report, regenerated by `plot_results.py` |
| `results/` | Every run's raw per-request CSVs, Prometheus scrapes, and `run.json` provenance |
| `deploy/` | Helm values + OpenShift notes for the 2×A10 cluster. `deploy/dev/` holds the ~60 s dev loop *and* two gates the measured runs call: `registry-probe.sh`, `revert-router-patch.sh` |
| `Dockerfile` | Router image: pinned upstream base + our `patches/` overlay, built in CI |

## Setup

Python ≥ 3.10. **No GPU and no cluster needed** for the test suite or for re-deriving every
published number:

```bash
pip install -r requirements.txt
```

## Tests

```bash
pytest benchmarks/ tests/ -q
```

190 tests, all offline. `.github/workflows/ci.yml` runs the same suite on every push, plus a
timed micro-benchmark of the router's placement path.

The cluster sweep is deliberately **not** in CI: it needs two A10 GPUs, an OpenShift namespace,
and ~40 minutes per cell. What reruns per commit is everything that is pure computation — the
scoring path, the workload generators, the statistics, the collectors' parsers — which is also
where a regression would otherwise be silent.

## Tunable parameter

One knob, read at router startup from the environment or the constructor
(`patches/vllm_router/routers/routing_logic.py`).

| Parameter | Env var | Default | Meaning |
|---|---|---|---|
| β | `LOADAWARE_BETA` | `1.0` | Exchange rate between cache benefit and relative load. **β=0 reduces the policy to pure cache-affinity** — the ablation arm |

With two engines the arithmetic is worth stating, because it is what the sweep grid means: one
engine at `+r` forces the other to `−r`, so the load gap is `2·β·r`, and a full cache hit is
exactly cancelled at `r = 1/(2β)`. At the default β=1.0 that is r=0.5 — an engine carrying 50%
more than the fleet mean stops attracting cache-hit traffic.

On a running deployment:

```bash
oc set env deploy/stack-deployment-router LOADAWARE_BETA=0.5
```

There is no `α`. An earlier design had one; since the benefit term is already normalized to the
cached fraction, α was a redundant scale factor and only β sets the trade-off.

## Verify without a cluster

Every figure and both statistical tests are recomputable from committed artifacts:

```bash
./scripts/reproduce.sh   # all of the below, diffed against the committed baselines

python3 benchmarks/export_summary.py results/2026080[56]-* --out /tmp/summary.csv
python3 benchmarks/analyze.py compare results/20260805-232541-loadaware-b0.5 \
                                      results/20260805-230541-kvaware
python3 benchmarks/plot_results.py results/20260805-2* results/20260806-0* \
  --comparator results/20260806-144135-roundrobin --cand loadaware-b0.5 --out /tmp/figures
```

`analyze.py compare` **refuses** to pair two runs whose `run.json` records a different rate or
a different workload manifest — "identical workload across arms" is enforced, not assumed.

## Reproduce the benchmarks on a cluster

Requires a 2-GPU Kubernetes/OpenShift cluster with the vLLM Production Stack chart. Full
procedure, deployment steps, cluster gotchas, metrics, validity rules, and the
pre-registered statistics are in [`benchmarks/README.md`](benchmarks/README.md).

```bash
python3 benchmarks/freeze_workloads.py          # regenerate the SHA-pinned workload
benchmarks/rate_pilot.sh                        # find the latency knee
LOADAWARE_TAG=<image-sha> benchmarks/run_sweep.sh <rate>
```

The measured replay runs as an **in-cluster Job**, so client-observed latency is timed from
inside the cluster rather than across the internet. The router image is built **in CI** from
this repo's `Dockerfile` on every `patches/**` push and pushed SHA-tagged to
`quay.io/rhl193000/lmstack-router-loadaware`, so the measured artifact is always reproducible
from the tree. Measured cells only ever use built images — the `deploy/dev/` ConfigMap overlay
is a development convenience and is never benchmarked.

## Documents

| Doc | What it answers |
|---|---|
| `docs/baseline-justification.md` | Why this baseline (§2): features, default eviction policy, fit |
| `docs/report/report.md` | The §6 report; CI builds it to PDF on every push |
| `benchmarks/README.md` | The §3 operator's manual |

# Load-aware prefix routing for the vLLM Production Stack

## What this project does

A KV cache keeps the attention state of a prompt prefix. A later request with the same prefix
does not calculate that state again. On a fleet of servers, the router decides which cache can
give a hit. If the router sends a request to the wrong server, the cache on the correct server
gives no benefit.

The standard router sends each request to the server that holds its prefix. This makes a new
problem. A popular prefix is on one server. All requests for that prefix go to that server. The
server becomes busy. The other server stays idle.

This project adds a new routing policy. The policy is called `loadaware`. It gives each server
a score. The score uses the cache benefit and the current load. The router selects the server
with the best score.

The policy decreases the load imbalance between the two servers by 48.1%. An independent
benchmark run gives 49.4%. The two results agree.

## The baseline projects

The project uses two open-source projects. It does not replace them. It adds to them.

| Project | Function |
|---|---|
| [LMCache](https://github.com/LMCache/LMCache) | The KV cache layer. It stores prefixes in GPU memory, CPU memory and disk. A controller records which server holds which prefix. |
| [vLLM Production Stack](https://github.com/vllm-project/production-stack) | The Helm chart and the router. The router selects a server for each request. |

LMCache uses LRU as the default eviction policy. The router has these routing policies:
`roundrobin`, `session`, `kvaware` and `prefixaware`. The project adds `loadaware`.

For the full analysis of the baseline, refer to
[`docs/baseline-justification.md`](docs/baseline-justification.md).

## Our changes

The project changes three files. The directory `patches/` holds the changed files. Each file is
in the same path as in the container image. Each change has a `LOADAWARE PATCH` comment.

| File | Change |
|---|---|
| `lmcache/v1/cache_controller/controllers/kv_controller.py` | The function `lookup()` gives the matched token count for each server. The standard function gives only the first server. |
| `vllm_router/routers/routing_logic.py` | The new class `LoadAwareRouter`. It gives a score to each server. The class `KvawareRouter` does not change. |
| `vllm_router/parsers/parser.py` | The option `--routing-logic` accepts the value `loadaware`. |

### Upstream contributions

| Pull request | Status |
|---|---|
| [production-stack#1029](https://github.com/vllm-project/production-stack/pull/1029): expose the LMCache controller ports on the router Service | Open |
| The `loadaware` policy into production-stack | Not sent |

### The score

```
score(server) = matched_tokens / prompt_tokens  -  beta * relative_load(server)

relative_load = (load - fleet_mean) / max(1, fleet_mean)
```

Both terms are fractions. Thus `beta` does not have a unit. The router calculates the fleet
mean for each request.

### The tunable parameter

The policy has one parameter. Set it with an environment variable.

| Parameter | Variable | Default | Function |
|---|---|---|---|
| beta | `LOADAWARE_BETA` | `1.0` | The exchange rate between the cache benefit and the load. A value of `0` gives cache affinity only. |

To change the value on a live deployment, use this command:

```bash
oc set env deploy/stack-deployment-router LOADAWARE_BETA=0.5
```

With two servers, a full cache hit is equal to the load penalty at `r = 1/(2*beta)`. At the
default value of 1.0, `r` is 0.5. A server with 50% more load than the mean does not attract
more cache hits.

## Repository structure

| Path | Contents |
|---|---|
| `patches/` | The three changed files. This is the extension. |
| `tests/` | The unit tests for the changes. They run on a laptop. |
| `conformance/` | Tests that run the upstream test suite against the changed files. CI only. |
| `benchmarks/` | The workload generator, the load driver, the collectors and the analysis. |
| `results/` | The measurement data. One directory for each sweep. |
| `docs/` | The report, the baseline analysis and the figures. |
| `deploy/` | The Helm values and the cluster notes. |
| `scripts/reproduce.sh` | Calculates each reported number again from the data in this repository. |
| `Dockerfile` | The router image. CI builds it. |

## Our environment

The benchmark runs on this hardware and software:

| Item | Value |
|---|---|
| Platform | OpenShift, with the NVIDIA GPU Operator |
| GPUs | 2 x NVIDIA A10, 24 GB each. The cluster reports 23 GB as available. |
| Servers | One vLLM engine on each GPU |
| Model | `Qwen/Qwen2.5-3B-Instruct`. The model is not gated. |
| Software | vLLM Production Stack Helm chart 0.1.11, LMCache 0.3.9post2 |
| Router image | `quay.io/rhl193000/lmstack-router-loadaware`. CI builds it from this repository. |

The GPU Operator supplies the DCGM exporter. All GPU measurements come from that exporter.

## Run the benchmark

### Option 1: verify the results without hardware

This procedure calculates each reported number again from the data in this repository. It needs
no GPU and no cluster. It takes approximately two minutes.

```bash
git clone https://github.com/BenEpstein/caching-in-llms.git
cd caching-in-llms
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pytest benchmarks/ tests/ -q       # 190 tests
./scripts/reproduce.sh             # calculates each reported number again
```

Python 3.10 or later is necessary. On macOS the default `python3` is 3.9. Use
`python3.12 -m venv .venv` on macOS.

The script `reproduce.sh` stops with an error if a number is different. Thus the report cannot
disagree with its data.

To get the two PDF documents, use these commands:

```bash
gh run download -n report-pdf                 -R BenEpstein/caching-in-llms
gh run download -n baseline-justification-pdf -R BenEpstein/caching-in-llms
```

### Option 2: run the full benchmark on a cluster

This procedure needs the environment in the table above. A full sweep of 7 cells takes
approximately 2.5 hours.

1. Install the stack with Helm. Use the values file `deploy/values-baseline-kvaware.yaml`.
2. Build the router image. CI builds it for each change to `patches/`.
3. Find the offered rate. Run `benchmarks/rate_pilot.sh`.
4. Run the sweep with this command:

   ```bash
   LOADAWARE_TAG=<image-sha> BENCH_TAG=<image-sha> benchmarks/run_sweep.sh 16
   ```

5. Make the figures and the statistics. Run `benchmarks/plot_results.py` and
   `benchmarks/analyze.py`.

For each step, for the deployment commands and for the known cluster problems, refer to
[`benchmarks/README.md`](benchmarks/README.md).

## Our runs and results

The project has three benchmark sweeps. Each cell in a sweep uses 20 seeds and 500 requests for
each seed.

| Sweep | Directory | Function |
|---|---|---|
| Generation 1 | `results/gen1-wan/` | 5 cells. The driver was outside the cluster. Superseded. |
| Generation 2 | `results/gen2-confirmatory/` | 6 cells. These are the reported results. |
| Generation 3 | `results/gen3-7cell/` | 7 cells. An independent repetition. |

### The results

| Measurement | Result |
|---|---|
| Load imbalance | Decreased by 48.1%. The result is significant (p < 0.0001). 18 of 20 seeds show an improvement. |
| Load imbalance, generation 3 | Decreased by 49.4%. The result is significant (p < 0.0001). |
| TTFT p95 | Decreased by 2.7%. The result is not significant (p = 0.115). The report gives this null result. |
| Goodput at 150 ms | 19.0% fewer requests are late (p = 0.0021). This is a secondary measurement. |
| Ablation, beta = 0 | No change in the load imbalance. Thus the load term causes all of the improvement. |

### The metrics that the benchmark collects

| Metric | Source |
|---|---|
| Latency for each request: TTFT, end-to-end, inter-token | The driver CSV files |
| Throughput: requests and tokens for each second | The driver CSV files |
| Cache hit rate | Prometheus, `vllm:prefix_cache_hits_total` |
| Queue depth and preemptions | Prometheus, `vllm:num_requests_waiting` |
| KV cache use | Prometheus, `vllm:kv_cache_usage_perc` |
| GPU use, GPU power, GPU memory | The DCGM exporter, `dcgm.csv` |
| Router CPU and router memory | Prometheus |

### The figures

The script `benchmarks/plot_results.py` makes 12 figures. The report uses these four:

| Figure | Contents |
|---|---|
| `fig6-load-balance` | The load on the busiest server against the load on the most idle server |
| `fig7-beta-tradeoff` | The latency against the cache hit rate, for each value of beta |
| `fig10-utilization` | The GPU use, the GPU memory, the CPU and the host memory |
| `fig12-goodput` | The goodput against the latency objective, from 50 ms to 400 ms |

The other 8 figures show the latency distributions, the percentiles, the paired seeds, the hit
rate, the throughput and the in-flight requests. The figures are in `docs/figures/` for
generation 2, in `docs/figures-gen3/` for generation 3 and in `docs/figures-wan/` for
generation 1.

For the full analysis, refer to the report, [`docs/report/report.md`](docs/report/report.md).
For the method and the data, refer to [`benchmarks/README.md`](benchmarks/README.md).

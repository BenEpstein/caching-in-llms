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
kubectl set env deploy/stack-deployment-router LOADAWARE_BETA=0.5
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
| `Dockerfile` | The router image. It holds the changed files. CI builds it. |
| `Dockerfile.bench` | The benchmark driver image. It sends the requests from inside the cluster. CI builds it. |

## Our environment

The benchmark runs on this hardware and software:

| Item | Value |
|---|---|
| Platform | OpenShift (a Kubernetes cluster), with the NVIDIA GPU Operator |
| GPUs | 2 x NVIDIA A10, 24 GB each. The cluster reports 23 GB as available. |
| Servers | One vLLM engine on each GPU |
| Model | `Qwen/Qwen2.5-3B-Instruct`. The model is not gated. |
| Software | vLLM Production Stack Helm chart 0.1.11, LMCache 0.3.9post2 |
| Router image | `quay.io/rhl193000/lmstack-router-loadaware`. CI builds it from `Dockerfile`. |
| Driver image | `quay.io/rhl193000/bench-driver`. CI builds it from `Dockerfile.bench`. |

The GPU Operator supplies the DCGM exporter. All GPU measurements come from that exporter.

## Deploy the stack

This procedure installs the stack with our `loadaware` router. That is the default here. The
baseline router is one values file less. Refer to "Switching between arms".

A benchmark cell does not use this procedure. Each cell installs its own arm, sets the
parameter, patches the Service and starts the stack cold. Use this procedure to get a working
deployment, or to run the router outside a benchmark. For the full list of what you need
first, refer to [`benchmarks/README.md`](benchmarks/README.md), "What you need before you
start".

### 1. Install

```bash
kubectl create namespace cache-llm
helm repo add vllm https://vllm-project.github.io/production-stack
helm install stack vllm/vllm-stack -n cache-llm --version 0.1.11 \
  -f deploy/values-baseline-kvaware.yaml \
  -f deploy/values-loadaware-image.yaml \
  --set routerSpec.tag=acf43d1
kubectl apply -n cache-llm -f deploy/prometheus.yaml
```

The first values file holds the whole deployment. The second file changes two values: the
router image and the routing policy. The tag comes from `--set`, because the file gives no
tag. Every other value is the same on both arms. Therefore only the router changes between the
arms.

Change `storageClass` before you install. Our value is not portable. The table is in
`benchmarks/README.md`, "Values you must change for your cluster".

The tag `acf43d1` is the image that CI built and that our measurements used. For your own
image, add `--set routerSpec.repository=<your-repo>`. Never use `latest`: the router and the
servers must carry the same LMCache version, and a floating tag cannot be audited after the
run.

The chart does not install a Prometheus that we can use. The last command installs ours.
Grafana and the benchmark both read it. Without it the load imbalance has no data.

The release name must be `stack`. The scripts derive the names `stack-deployment-router`,
`stack-llm-deployment-vllm` and `stack-router-service` from it.

The model is `Qwen/Qwen2.5-3B-Instruct`. It is not gated. No token is necessary.

### 2. Add the controller ports to the router Service

```bash
kubectl patch svc stack-router-service -n cache-llm --type=json -p '[
  {"op":"add","path":"/spec/ports/-","value":{"name":"lmcache-reply","port":9001,"targetPort":9001,"protocol":"TCP"}},
  {"op":"add","path":"/spec/ports/-","value":{"name":"lmcache-heartbeat","port":9002,"targetPort":9002,"protocol":"TCP"}}]'
```

The chart does not expose the ports 9001 and 9002. Without them the servers do not register and
each lookup fails. There is no error message. The pull request
[production-stack#1029](https://github.com/vllm-project/production-stack/pull/1029) fixes this
in the chart. `run_cell.sh` applies the same patch before each cell.

### 3. Set the environment of the router

```bash
kubectl set env deploy/stack-deployment-router -n cache-llm HF_HOME=/tmp/hf LOADAWARE_BETA=1.0
```

`LOADAWARE_BETA` is the parameter of the policy. The router reads it one time, when it starts.
Therefore a new value needs a restart of the router. This command makes that restart.

`HF_HOME` is necessary on both arms. Without it the router has no writable cache directory. The
tokenizer then fails for each request, and the router accepts approximately 4 requests for each
second.

Chart 0.1.11 has no `routerSpec.env`. Therefore neither value can go in a values file.

### 4. Start cold and check the registry

```bash
benchmarks/cold_start.sh
kubectl port-forward -n cache-llm svc/stack-router-service 8000:80 &
./deploy/dev/registry-probe.sh $(date +%s)
```

The first install takes several minutes. Each server loads the model before it is ready.

`cold_start.sh` scales the servers to zero, restarts the router into an empty cluster, then
starts the servers again. This order is necessary. A server that registers with the old router
gives an identifier that outlives it, and the baseline router then answers 500 for a lookup
that returns it. The script also verifies that both servers registered and that no dead
identifier remains.

Open the port-forward after the cold start, not before. The cold start restarts the router, and
a forward binds to one pod, so a forward opened first is already dead. An Ingress or an
OpenShift route fronts the Service and not a pod, so it survives the restart. With one of
those, set `BASE_URL` to it and use no forward.

`registry-probe.sh` sends the same long prefix four times. If the registry holds the prefix,
the router sends all four requests to one server. If the registry is empty, the requests
spread. Exit code 0 means the registry is live.

After every router restart the registry is blind for approximately 40 seconds. A prefix that is
first stored in that window stays invisible to the controller for the life of the server
process. Therefore always give the probe a seed that you did not use before on these servers.

### 5. Check the deployment

```bash
curl http://localhost:8000/v1/models
```

### Switching between arms

To go back to the baseline router, install with the first values file only, then remove the
parameter:

```bash
helm upgrade --install stack vllm/vllm-stack -n cache-llm --version 0.1.11 \
  -f deploy/values-baseline-kvaware.yaml
kubectl set env deploy/stack-deployment-router -n cache-llm HF_HOME=/tmp/hf LOADAWARE_BETA-
```

Helm reads the values files again for each upgrade. Therefore the router image and the routing
policy return to the baseline when you omit the second file. A value that `kubectl set env`
wrote does not behave in this way. Helm keeps it across an upgrade. Therefore remove
`LOADAWARE_BETA` with the final `-`. The baseline router does not read that value, but a value
that stays becomes the value of the next `loadaware` deployment.

The policy also cannot change without the image. The option `--routing-logic` has a fixed list
of values in the baseline, and `loadaware` is not in that list. The router stops at the
argument parser. Our image widens the list.

Every change of arm restarts the router. Repeat step 4 after each change.

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
pytest benchmarks/ tests/ -q       # 194 tests
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

You need a Kubernetes cluster with two GPUs, the NVIDIA GPU Operator and a `ReadWriteMany`
storage class. On your laptop you need `kubectl`, `helm` version 3, `podman` or `docker`, and
Python 3.10 or later. A full sweep of 7 cells takes approximately 2.5 hours.

The procedure has six steps:

| Step | What it does |
|---|---|
| 1 | Installs the stack with Helm, and our Prometheus |
| 2 | Gets the two images: the router (`Dockerfile`) and the driver (`Dockerfile.bench`) |
| 3 | Makes the workload files from the manifest |
| 4 | Finds the offered rate for your cluster |
| 5 | Runs the sweep, seven cells |
| 6 | Makes the statistics and the 12 figures |

Step 5 is one command. It needs the tag of each image:

```bash
LOADAWARE_TAG=<router-image-tag> BENCH_TAG=<driver-image-tag> benchmarks/run_sweep.sh 16
```

If you built the images into your own registry, give their names also with `ROUTER_REPO` and
`BENCH_REPO`.

One value in `deploy/values-baseline-kvaware.yaml` is specific to our cluster. Change
`storageClass` to a `ReadWriteMany` class on your cluster before step 1.

For every step, and for the full list of what you need, refer to
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
| TTFT p95, beta = 0.5 against `kvaware` | Decreased by 2.7%, not significant (p = 0.115). This is the reported arm. It was selected before the measurement, so the report gives this null result. |
| TTFT p95, beta = 1.0 against `kvaware` | Decreased by 9.3% (p = 0.0053). |
| Goodput at 150 ms | 19.0% fewer requests are late (p = 0.0021). |
| Ablation, beta = 0 | No change in the load imbalance. Thus the load term causes all of the improvement. |

The figure `fig5-percentiles` shows lower TTFT bars for both `loadaware` arms. The bars are seed
medians and the seed spreads overlap, which is why the reported arm is a null.

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

The driver CSV files are our own measurement. The program `benchmarks/load_driver.py` writes
them. This program runs in the driver image, in a pod in the cluster. It writes one row for each
request that it sends. One file holds one seed: `driver-seed<N>.csv`.

Prometheus and the DCGM exporter are collectors of the cluster. This project does not change
them. They are the source of the fleet metrics, because the driver sees only its own requests.

### The figures

The script `benchmarks/plot_results.py` makes 12 figures.

| Figure | Contents | In the report |
|---|---|---|
| `fig1-ttft-p95-vs-beta` | The TTFT p95 for each value of beta | |
| `fig2-ttft-ecdf` | The full distribution of the TTFT, for each policy | |
| `fig3-hit-rate` | The LMCache lookup hit rate, for each policy | |
| `fig4-paired-seeds` | The 20 paired seeds. This is the data of the statistical test. | |
| `fig5-percentiles` | The TTFT p50, p90, p95 and p99, for each policy | |
| `fig6-load-balance` | The load on the busiest server against the load on the most idle server | yes |
| `fig7-beta-tradeoff` | The latency against the cache hit rate, for each value of beta | yes |
| `fig8-itl-percentiles` | The inter-token latency percentiles, for each policy | |
| `fig9-throughput` | The requests and the tokens for each second | |
| `fig10-utilization` | The GPU use, the GPU memory, the CPU and the host memory | yes |
| `fig11-inflight-vs-time` | The requests in flight on each server against time | |
| `fig12-goodput` | The goodput against the latency objective, from 50 ms to 400 ms | yes |

The figures are in `docs/figures/` for generation 2, in `docs/figures-gen3/` for generation 3
and in `docs/figures-wan/` for generation 1.

For the full analysis, refer to the report, [`docs/report/report.md`](docs/report/report.md).
For the method and the data, refer to [`benchmarks/README.md`](benchmarks/README.md).

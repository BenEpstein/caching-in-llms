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

### The words of the score

| Word | Meaning |
|---|---|
| Cache benefit | The fraction of the prompt that a server has in its cache. The first term of the score. |
| Load penalty | The count of the requests in flight on a server: prefill plus decode. The queue does not enter the score. |
| Relative load | The load penalty as a signed fraction of the fleet mean. A value of 0.0 is the fleet average. A value of +1.0 is two times the average. This is the term that `beta` weighs. |
| Load imbalance | The outcome that the benchmark measures. It is the mean load of the busiest server divided by the mean load of the most idle server, in the measurement window. A value of 1.0 is even. |

"Relative load" and "load imbalance" are not the same. Relative load is an input of the
policy, calculated for each request. Load imbalance is the measured outcome, and one of the
two tested claims. The policy weighs the first to decrease the second.

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
| GPUs | 2 x NVIDIA A10, 24 GB each |
| Servers | One vLLM engine on each GPU |
| Model | `Qwen/Qwen2.5-3B-Instruct`. The model is not gated. |
| Software | vLLM Production Stack Helm chart 0.1.11, LMCache 0.3.9post2 |
| Router image | `quay.io/rhl193000/lmstack-router-loadaware`. CI builds it from `Dockerfile`. |
| Driver image | `quay.io/rhl193000/bench-driver`. CI builds it from `Dockerfile.bench`. |

The GPU Operator supplies the DCGM exporter. All GPU measurements come from that exporter.

## Deploy the stack

This procedure installs the stack with the `loadaware` router. Use it to run the extension. A
benchmark cell does not use it: each cell installs its own arm. For the tools and the cluster
that you need, refer to [`benchmarks/README.md`](benchmarks/README.md), "What you need before
you start".

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

The first values file holds the deployment. The second changes only the router image and the
routing policy. The tag `acf43d1` is the image that CI built. For your own image, add
`--set routerSpec.repository=<your-repo>`.

Change `storageClass` before you install. Our value is not portable. The chart installs no
Prometheus that we can use, so the last command installs ours. Keep the release name `stack`.
The scripts derive the other names from it.

### 2. Add the controller ports

```bash
kubectl patch svc stack-router-service -n cache-llm --type=json -p '[
  {"op":"add","path":"/spec/ports/-","value":{"name":"lmcache-reply","port":9001,"targetPort":9001,"protocol":"TCP"}},
  {"op":"add","path":"/spec/ports/-","value":{"name":"lmcache-heartbeat","port":9002,"targetPort":9002,"protocol":"TCP"}}]'
```

The chart does not expose the ports 9001 and 9002. Without them the servers do not register and
every lookup fails, with no error message. `run_cell.sh` applies the same patch.

### 3. Set the router environment

```bash
kubectl set env deploy/stack-deployment-router -n cache-llm HF_HOME=/tmp/hf LOADAWARE_BETA=1.0
```

`LOADAWARE_BETA` is the parameter of the policy. `HF_HOME` gives the router a writable cache
directory. Without it the tokenizer fails on each request and the router accepts approximately
4 requests for each second. Chart 0.1.11 has no `routerSpec.env`, so neither value can go in a
values file. This command restarts the router, which is how a new value takes effect.

### 4. Start cold, then check the registry

```bash
benchmarks/cold_start.sh
kubectl port-forward -n cache-llm svc/stack-router-service 8000:80 &
until curl -fsS -o /dev/null http://localhost:8000/v1/models; do sleep 1; done
./deploy/dev/registry-probe.sh $(date +%s)
```

`cold_start.sh` scales the servers to zero, restarts the router, then starts the servers again.
This order keeps a dead server identifier away from the new router. The servers need several
minutes, because each one loads the model.

Open the port-forward after the cold start. The cold start restarts the router, and a forward
binds to one pod. The `until` loop waits for the forward, which needs a moment to bind. An
Ingress or a route survives a restart: set `BASE_URL` to it and open no forward.

`registry-probe.sh` sends one long prefix four times. All four requests on one server means the
registry is live. Give the probe a seed that you did not use before. After a router restart the
registry is blind for approximately 40 seconds, and a prefix stored in that window stays
invisible.

### Switch back to the baseline

```bash
helm upgrade --install stack vllm/vllm-stack -n cache-llm --version 0.1.11 \
  -f deploy/values-baseline-kvaware.yaml
kubectl set env deploy/stack-deployment-router -n cache-llm HF_HOME=/tmp/hf LOADAWARE_BETA-
```

Helm reads the values files again, so the image and the policy return to the baseline when you
omit the second file. A value from `kubectl set env` does not. It survives the upgrade. Remove
`LOADAWARE_BETA` with the final `-`, or it becomes the value of your next `loadaware` deploy.

The image and the policy move together. The baseline `--routing-logic` does not accept
`loadaware`, and the router then stops at the argument parser. Every change of arm restarts the
router, so do step 4 again.

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

A full sweep of 7 cells takes approximately 2.5 hours. It needs the cluster in the table above.

[`benchmarks/README.md`](benchmarks/README.md) gives the procedure: what you need, the two
images, the six steps and how to read the data.

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

For the full analysis, refer to the report, [`docs/report/report.md`](docs/report/report.md).
For the metrics, the 12 figures, the method and the data, refer to
[`benchmarks/README.md`](benchmarks/README.md).

# The benchmark harness

> status: live · 2026-08-08 · this file tells you how to run the benchmark and how to read the
> data. For the results and the analysis, refer to `docs/report/report.md`.

## Overview

The benchmark compares routing policies on a cluster with two GPUs. It sends a fixed set of
requests to the router. It records the latency of each request. It also records the load on each
server.

A Kubernetes Job sends the requests from inside the cluster. Thus the network between a laptop
and the cluster is not part of the measurement.

The project has three sweeps. A sweep is one batch of cells. A cell is one routing policy at one
offered rate.

| Sweep | Directory | Cells | Function |
|---|---|---|---|
| Generation 1 | `results/gen1-wan/` | 5 | A laptop sent the requests. Superseded. The report uses it only to show the problem with that method. |
| Generation 2 | `results/gen2-confirmatory/` | 6 | The reported results. |
| Generation 3 | `results/gen3-7cell/` | 7 | An independent repetition of generation 2, with two more cells. |

Generation 2 and generation 3 use the same instrument and the same images. They use different
time windows. Do not compare a cell from one sweep with a cell from a different sweep. The tool
`analyze.py` refuses to do this. It compares the `sweep_id` value in each `run.json` file.

To check the reported numbers without a cluster, do the Python setup in "On your laptop"
below, then run one command:

```bash
./scripts/reproduce.sh
```

The section "The data is in the repository" explains what it checks. To run the benchmark
itself, on a cluster, continue reading.

## The files

| File | Function |
|---|---|
| `workload_gen.py` | Makes the workload. The generator is deterministic. |
| `freeze_workloads.py` | Makes the workload files again and compares them with the manifest. |
| `load_driver.py` | Sends the requests. Writes one CSV row for each request. |
| `in_pod.sh` | Runs the measurement inside the cluster. |
| `verify_dataset.sh` | Makes the workload again inside the pod and checks the checksums. |
| `bench_job.sh` | Applies the Kubernetes Job and collects the log. |
| `collect_job.py` | Extracts the CSV files from the Job log. |
| `warmup.py` | Sends requests before the measurement. These requests fill the cache. |
| `run_cell.sh` | Runs one cell. Deploys, checks the gates, warms up, measures and collects. |
| `run_sweep.sh` | Runs all seven cells in one batch. |
| `rate_pilot.sh` | Finds the offered rate for the sweep. |
| `analyze.py` | Makes the results table and the statistics. |
| `export_summary.py` | Makes `summary-per-seed.csv` for a sweep. |
| `plot_results.py` | Makes the 12 figures. |
| `utilization.py` | Makes the utilization report. |
| `load_gate.py` | Shows if the offered rate causes load on the servers. |
| `cold_start.sh` | Restarts the servers before a cell. |
| `router_forward.sh` | Opens the port-forward from the laptop to the router. Sourced, not run. |
| `collectors/prom_dump.py` | Gets the Prometheus data for the measurement window. |
| `collectors/dcgm_poll.py` | Gets the GPU data from the DCGM exporter. |

### Which file calls which file

You start one script: `run_sweep.sh`. It calls the other scripts in this order.

```
run_sweep.sh                     one batch, seven cells
└── run_cell.sh                  one cell, called seven times
    ├── freeze_workloads.py      1. makes the workload files
    │   └── workload_gen.py
    ├── cold_start.sh            2. empties the cache and restarts the servers
    ├── router_forward.sh        3. opens the port-forward, after the restart
    ├── ../deploy/dev/registry-probe.sh
    │                            4. checks that the KV registry has data
    ├── warmup.py                5. fills the cache before the measurement
    ├── collectors/dcgm_poll.py  6. starts to record the GPU data
    ├── bench_job.sh             7. applies the Kubernetes Job
    │   └── in_pod.sh                 runs in the driver image, in the cluster
    │       ├── verify_dataset.sh     checks the workload checksums in the pod
    │       └── load_driver.py        sends the requests, one CSV row for each
    ├── collect_job.py           8. reads the Job log, writes driver-seed<N>.csv
    └── collectors/prom_dump.py  9. gets the Prometheus data for the window
```

The result is one directory for each cell. Four scripts then read those directories. They do
not need the cluster.

```
results/<sweep>/<cell>/  ──┬── export_summary.py   makes summary-per-seed.csv
                           ├── analyze.py          makes the statistics
                           ├── plot_results.py     makes the 12 figures
                           └── utilization.py      makes the utilization report
```

Two scripts are not part of the sweep. Run `rate_pilot.sh` before the sweep, to find the offered
rate. Run `load_gate.py` after a cell, to see if the rate made load on the servers.

## What you need before you start

### On your laptop

| Tool | Version | Function |
|---|---|---|
| `git` | any | Gets this repository |
| `python3` | 3.10 or later | Makes the workload and reads the results. On macOS the default `python3` is 3.9, so use `python3.12`. |
| `kubectl` | any | All scripts use `kubectl`. `oc` is not necessary. |
| `helm` | 3 | Installs the stack |
| `podman` or `docker` | any | Builds the two images |

```bash
git clone https://github.com/BenEpstein/caching-in-llms.git
cd caching-in-llms
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### On the cluster

| Item | Value | Why |
|---|---|---|
| GPUs | 2, on 2 nodes | One vLLM engine on each. The comparison needs two servers. |
| GPU memory | 23 GB or more on each GPU | The model and a KV pool of 99000 tokens |
| NVIDIA GPU Operator | installed | It supplies the DCGM exporter, which is the source of every GPU number |
| A storage class with `ReadWriteMany` | any | The two engine pods share one model volume |
| Permission | make a namespace, install a Helm chart, patch a Service, read pod logs | The scripts do all of these |

### Values you must change for your cluster

`deploy/values-baseline-kvaware.yaml` holds the values of our cluster. One of them is not
portable. Change it before you install:

| Field | Our value | Change it to |
|---|---|---|
| `servingEngineSpec.modelSpec[0].storageClass` | `ocs-external-storagecluster-cephfs` | A `ReadWriteMany` storage class on your cluster. `kubectl get storageclass` lists them. |

The two engine pods share one model volume, so the class must be `ReadWriteMany`. A
`ReadWriteOnce` class leaves the second pod in `Pending` with no clear reason. The volume needs
50 GB, which is the value of `pvcStorage`.

The scripts read the DCGM exporter from the namespace `nvidia-gpu-operator`, with the label
`app=nvidia-dcgm-exporter`. This is where the GPU Operator puts it. If your exporter is
somewhere else, the GPU numbers are missing. The cell does not stop, because the driver CSV
files are the primary measurement.

## How to run the benchmark

This is the full run with our public images. Each command is explained in the steps below.

```bash
# One time for each cluster. Set the storage class first (see the section above).
kubectl create namespace cache-llm
helm repo add vllm https://vllm-project.github.io/production-stack
helm install stack vllm/vllm-stack -n cache-llm --version 0.1.11 \
  -f deploy/values-baseline-kvaware.yaml
kubectl apply -n cache-llm -f deploy/prometheus.yaml
kubectl get pods -n cache-llm -w          # Ctrl-C when the 5 pods are Running
benchmarks/rate_pilot.sh                  # find the knee rate of your cluster (step 4)

# One time for each run.
export LOADAWARE_TAG=acf43d1 BENCH_TAG=acf43d1        # our CI-built images (step 2)
RATE=16                                               # our knee; use the rate from the pilot
python3 benchmarks/freeze_workloads.py                # early workload check (step 3)
SEEDS="1 2" benchmarks/run_cell.sh loadaware-b0.5 $RATE results/smoke   # 5-minute test (step 5)
rm -rf results/smoke
benchmarks/run_sweep.sh $RATE                         # 7 cells, approximately 2.3 hours

# After the sweep, on the laptop. The cluster is not necessary.
# <sweep> is the directory that run_sweep.sh printed, results/sweep-<timestamp>.
python3 benchmarks/analyze.py table results/<sweep>/*       # results table, one row per cell
python3 benchmarks/plot_results.py results/<sweep>/* --cand loadaware-b0.5 \
  --comparator results/<sweep>/*-roundrobin --out docs/figures-<sweep>
python3 benchmarks/utilization.py report results/<sweep>/*
python3 benchmarks/analyze.py compare results/<sweep>/*-loadaware-b0.5 results/<sweep>/*-kvaware   # the pre-registered test
```

### Step 1: install the stack

```bash
kubectl create namespace cache-llm
helm repo add vllm https://vllm-project.github.io/production-stack
helm install stack vllm/vllm-stack -n cache-llm --version 0.1.11 \
  -f deploy/values-baseline-kvaware.yaml
```

Use chart version 0.1.11. Later versions have a different schema.

The chart does not install a Prometheus that we can use. Install ours:

```bash
kubectl apply -n cache-llm -f deploy/prometheus.yaml
```

This step is necessary. Prometheus is the source of the load imbalance, which is the main
result. Without it, each cell writes a warning and the imbalance cannot be calculated.

Now wait for the pods:

```bash
kubectl get pods -n cache-llm -w
```

The install is complete when you see these pods. The two engine pods need approximately 5
minutes, because each one loads the model.

```
NAME                                         READY   STATUS    RESTARTS   AGE
stack-deployment-router-5c9c9d4f6-6gxnk      1/1     Running   0          6m
stack-grafana-7f57cd99bb-th8j7               3/3     Running   0          6m
stack-llm-deployment-vllm-58c7ddcdd5-ctpsb   1/1     Running   0          6m
stack-llm-deployment-vllm-58c7ddcdd5-k8qgf   1/1     Running   0          6m
stack-prometheus-5b74f7d9d5-5bcxw            1/1     Running   0          6m
```

The two `stack-llm-deployment-vllm` pods are the servers, one on each GPU. Grafana is optional.
It shows the dashboards of the chart. The benchmark does not use it.

This install gives the baseline router. That is correct for the sweep. The first cell is
`kvaware`, and each cell installs its own arm before it measures. To deploy the `loadaware`
router by itself, outside a benchmark, refer to `README.md`, "Deploy the stack".

You do not need an Ingress or an OpenShift route. The Job sends the measured requests to the
router Service, inside the cluster. The warm-up and the registry probe run from your laptop.
For them, each cell opens its own port-forward and closes it at the end. Do not open a
port-forward by hand. Every cell restarts the router, and the restart kills a port-forward.

If you have an Ingress or a route to the router, set `BASE_URL` to its address. A route goes to
the Service, not to one pod, so it survives the restart. The scripts then make no port-forward.

```bash
export BASE_URL=https://<your-router-address>
```

### Step 2: get the two images

The benchmark needs two images. The scripts point to our public images. Thus you set only the
tag:

```bash
export LOADAWARE_TAG=acf43d1
export BENCH_TAG=acf43d1
```

The tag `acf43d1` is the git short SHA that CI built the images from. Our reported measurements
use these images.

| Image | Dockerfile | Function | Our public image |
|---|---|---|---|
| Router | `Dockerfile` | The router with the `loadaware` policy | `quay.io/rhl193000/lmstack-router-loadaware` |
| Driver | `Dockerfile.bench` | Sends the requests from a pod | `quay.io/rhl193000/bench-driver` |

#### To build your own images (optional)

Use the root of the repository as the build context. Both Dockerfiles copy files from it.

```bash
export REG=docker.io/<your-username>
export TAG=$(git rev-parse --short HEAD)

podman build -t $REG/lmstack-router-loadaware:$TAG -f Dockerfile .
podman build -t $REG/bench-driver:$TAG -f Dockerfile.bench .

podman login docker.io
podman push $REG/lmstack-router-loadaware:$TAG
podman push $REG/bench-driver:$TAG
```

Make both repositories public. The cluster pulls them with no credentials. For a private
repository, add an image pull secret to the namespace yourself. The scripts do not make one.

Then give the names and the tag to the sweep in step 5.

Do not use `latest` as the tag. Each cell writes the image and its digest into `run.json`. A
floating tag makes the measurement impossible to audit later.

### Step 3: make the workload

```bash
python3 benchmarks/freeze_workloads.py
```

The command makes the workload files again from `benchmarks/workloads/manifest.json`. It stops with an
error if a checksum is different. The workload files are not in the repository. The manifest is
in the repository.

### Step 4: find the offered rate

```bash
benchmarks/rate_pilot.sh
```

The script sends requests at different rates. Read the achieved rate and the latency. The knee
is the rate where the achieved rate becomes less than the offered rate. On our cluster the knee
is between 14 and 16 requests for each second.

### Step 5: run the sweep

Run one cell with two seeds first. It takes approximately 5 minutes. The two tags come from
the exports in step 2.

```bash
SEEDS="1 2" benchmarks/run_cell.sh loadaware-b0.5 16 results/smoke
```

This runs the full sequence of one cell, the 12 operations listed below. A wrong tag, a
missing Prometheus or a bad storage class fails here, in 5 minutes, and not two hours into
the sweep. Delete `results/smoke` when it passes. It is not a measurement: two seeds cannot
give a result.

Then run the sweep:

```bash
benchmarks/run_sweep.sh 16
```

If you built your own images in step 2, give their names also:

```bash
ROUTER_REPO=<your-registry>/lmstack-router-loadaware \
BENCH_REPO=<your-registry>/bench-driver \
LOADAWARE_TAG=<image-sha> BENCH_TAG=<image-sha> benchmarks/run_sweep.sh 16
```

The sweep runs seven cells: `kvaware`, then beta 0.5, 1.0, 2.0, 0.25 and 0, then `roundrobin`.
One cell takes approximately 20 minutes. A full sweep takes approximately 2.3 hours.

For each cell, `run_cell.sh` does these operations:

1. Deploys the cell with Helm.
2. Sets the beta value with `kubectl set env`.
3. Applies the patch for the router Service ports.
4. Compares the router image with the label of the cell.
5. Restarts the servers. Each cell starts with an empty cache.
6. Waits for the two servers to register.
7. Opens the port-forward to the router. This is after the restart, so the restart cannot kill it.
8. Runs the registry probe.
9. Sends the warm-up requests.
10. Applies the Kubernetes Job. The Job sends the 20 seeds.
11. Collects the Prometheus data, the DCGM data and the manifest.
12. Checks the validity rules.

### Step 6: make the results

The sweep leaves one directory for each cell. Everything in this step runs on the laptop. The
cluster is not necessary. Three commands make the results:

```bash
python3 benchmarks/analyze.py table results/<sweep>/*       # the results table
python3 benchmarks/plot_results.py results/<sweep>/* --cand loadaware-b0.5 \
  --comparator results/<sweep>/*-roundrobin --out docs/figures-<sweep>
python3 benchmarks/utilization.py report results/<sweep>/*
```

The table gives one row for each cell: the median over the seeds of the error rate, the TTFT,
the inter-token latency, the end-to-end latency, the throughput and the load imbalance. It
looks like this:

```
cell            seeds  error_rate  ttft_p50_s  ttft_p95_s  ttft_p99_s  itl_p95_s  e2e_p95_s  req_per_s  tok_per_s  imbalance
kvaware            20       0.43%       0.173       0.324       0.415      0.151      7.077      14.41        922       2.39
loadaware-b0.5     20       0.34%       0.152       0.296       0.368      0.143      5.958      14.39        921       1.25
roundrobin         20       0.08%       1.144      11.051      13.309      0.912     28.381      10.41        666       1.50
```

The table is descriptive. It has no p-values: the test was registered for named pairs, and a
p-value for every pair of cells would make the threshold meaningless. For the test, read "The
statistical test" below.

`plot_results.py` makes the 12 figures. Write them to a directory for that sweep. Do not write
them to `docs/figures/`. That directory holds the figures for the reported sweep.
`utilization.py` gives the GPU, the CPU and the memory numbers.

#### The statistical test

`analyze.py compare` runs the pre-registered test on one pair of cells: the candidate first,
then the baseline. The candidate is the cell under test. The baseline is always the `kvaware`
cell. The command prints the per-seed pairs, the p-value and the size of the change.

```bash
python3 benchmarks/analyze.py compare results/<sweep>/*-loadaware-b0.5 results/<sweep>/*-kvaware
```

The `roundrobin` cell is a comparator for the figures. It is not one of the tested pairs.

The command tests one metric at a time. Select it with `--metric`:

| `--metric` value | Meaning |
|---|---|
| `ttft_p95` | The TTFT p95 of each seed. The default, and one of the two primary measurements. |
| `imbalance` | The load imbalance of each seed. The other primary measurement. |
| `ttft_slo_miss` | The fraction of the sent requests with no first token before the objective. Set the objective with `--slo`, in seconds. An errored request counts as a late request. |
| `ttft_`, `e2e_`, `itl_` + `mean`, `p50`, `p90`, `p95`, `p99` | The latency statistics, for example `e2e_p99`. |
| `throughput_req_s`, `throughput_tok_s` | The requests and the tokens for each second. |
| `error_rate` | The error rate of each seed. |

The metric `ttft_slo_miss` is 1 minus the goodput. The figure `fig12-goodput` draws the whole
curve from 50 ms to 400 ms, so one objective is not a threshold to select.

#### For the reported sweep

`export_summary.py` collects the per-seed numbers into one small CSV. It holds every number
that the report and the figures use. The repository commits this file for the reported sweep.

```bash
python3 benchmarks/export_summary.py results/<sweep>/* --out results/<sweep>/summary-per-seed.csv
```

If a new sweep becomes the reported sweep, change the cell names in `scripts/reproduce.sh` in
the same commit. The names are `HEADLINE`, `BASELINE`, `ABLATION`, `BETA1`, `BETA2` and
`COMPARATOR`. If you do not change them, the script compares the old sweep and passes, and it
does not check the new figures.

## The measurement runs inside the cluster

A Kubernetes Job sends the requests. The Job runs on a node in the cluster.

- The target is the router Service. The Job does not use a route and does not use TLS.
- The pod makes the workload again from the manifest. The pod does not receive a copy.
- The results go out through the pod log. Each seed is one compressed frame with a checksum.
  `collect_job.py` writes no data if one frame is incorrect.
- The measurement window comes from the clock of the pod, not from the laptop.
- The Job has `backoffLimit: 0`. A cell that fails must stop. It must not send the seeds again.

This is important for the latency data. When a laptop sent the requests, 45% to 59% of the
recorded latency was network time. That value is more than the difference between the policies.
Inside the cluster, the same term is approximately 26%.

The driver CSV files are the only source of per-request latency. The router gives average values
only. Load imbalance comes from Prometheus and is not affected by the network.

## The metrics

| Metric | Source | Scope |
|---|---|---|
| TTFT, end-to-end latency, inter-token latency | `driver-seed<N>.csv` | Each request |
| Throughput, requests and tokens for each second | `driver-seed<N>.csv` | Each seed |
| Errors | `driver-seed<N>.csv` | Each seed |
| Load imbalance | `vllm:num_requests_running` | Each server |
| Cache hit rate | `vllm:prefix_cache_hits_total` and `_queries_total` | Each server |
| Queue depth, preemptions | `vllm:num_requests_waiting`, `vllm:num_preemptions_total` | Each server |
| KV cache use | `vllm:kv_cache_usage_perc`, `lmcache:local_cache_usage` | Each server |
| GPU use, GPU power, GPU memory copy | `dcgm.csv` from the DCGM exporter | Each GPU |
| Router CPU, router memory | `router_cpu_usage_percent`, `process_resident_memory_bytes` | The router |

The driver CSV files are our own measurement. `load_driver.py` writes them. It runs in the
driver image, in a pod in the cluster, and it writes one row for each request that it sends.
One file holds one seed. Prometheus and the DCGM exporter are collectors of the cluster. This
project does not change them. They give the fleet metrics, because the driver sees only its own
requests.

The percentiles come from the CSV rows. They do not come from a Prometheus histogram. Thus the
p95 and the p99 values are exact.

The load imbalance is the mean in-flight count of the busiest server divided by the mean
in-flight count of the most idle server. Each seed gives one value, over the send window of
that seed. A value of 1.0 is even.

Two values are not available. vLLM has no `process_*` collector. Therefore the host CPU and the
host memory of the servers cannot be measured. The report gives them as missing. It does not
give a different value in their place. The GPU use is the utilization number for the servers.

Each `run.json` file records `utilization_coverage`. This is the fraction of the measurement
window that each metric covers. A value below 0.95 gives a warning. It does not stop the cell,
because the driver CSV files are the primary measurement.

## The figures

`plot_results.py` makes 12 figures.

| Figure | Contents | In the report |
|---|---|---|
| `fig1-ttft-p95-vs-beta` | The TTFT p95 for each value of beta | |
| `fig2-ttft-ecdf` | The distribution of the TTFT for each policy | |
| `fig3-hit-rate` | The LMCache lookup hit rate | |
| `fig4-paired-seeds` | The paired seeds. This is the data of the statistical test. | |
| `fig5-percentiles` | The TTFT percentiles for each policy | |
| `fig6-load-balance` | The busiest server against the most idle server | yes |
| `fig7-beta-tradeoff` | The latency against the cache hit rate | yes |
| `fig8-itl-percentiles` | The inter-token latency percentiles | |
| `fig9-throughput` | The tokens and the requests for each second | |
| `fig10-utilization` | The GPU use, the GPU memory, the CPU and the host memory | yes |
| `fig11-inflight-vs-time` | The requests in flight on each server against time | |
| `fig12-goodput` | The goodput against the latency objective, from 50 ms to 400 ms | yes |

The figures are in `docs/figures/` for generation 2, in `docs/figures-gen3/` for generation 3
and in `docs/figures-wan/` for generation 1.

## Why we selected these parameters

### The dataset: 128 shared prefixes, Zipf s = 0.9

The data is synthetic. `workload_gen.py` makes it from a fixed list of English words. It is not
a corpus of real text.

Each prompt has two parts:

| Part | Tokens | Function |
|---|---|---|
| A shared prefix, selected from a pool of 128 | 1544 | This part can come from the cache |
| A unique suffix | 34 | This part is different in each request, so no request is a repetition |

Real text is not necessary here. The router does not read the prompt. It compares token
prefixes. A prompt of real text and a prompt of filler words of the same length give the same
work to the cache and to the GPU. Synthetic data also gives the exact control that the
comparison needs: 97.8% of each prompt can come from the cache, and the length is the same in
every request. Therefore the prompt length cannot cause a difference between the policies.

This shape is the shape of a real load. A shared prefix is the system prompt or the retrieved
document that many requests have in common. The unique suffix is the question of one user.

Each request selects its prefix with a Zipf distribution (s = 0.9). The most frequent prefix
receives 14.8% of the requests. The first three prefixes receive 28.0%. This gives cache reuse,
but not extreme reuse. With extreme reuse, all requests go to one server, and the comparison has
no value.

The dataset is frozen. `benchmarks/workloads/manifest.json` holds a checksum for each seed file. Every cell
makes the files again from the manifest and stops if a checksum is different. Thus every cell in
every sweep replays exactly the same requests.

### The rate: 16 requests for each second

The rate comes from the pilot in step 4. The knee is between 14 and 16.

The rate is important. If the rate is too low, the servers have no queue. Then a load-aware
router has no queue to prevent. If the rate is too high, both servers become full. Then no
server is better than the other server. The window for an improvement closes at both ends.

### The output length: 64 tokens

A pilot measured 64, 128 and 256 tokens. At 128 tokens the fleet becomes full and gives only 65%
of the offered rate. At 256 tokens the error rate is 11.4%, which is more than the limit of 10%.
At 64 tokens the achieved rate follows the offered rate.

### The memory: 45% of the GPU

The value `gpuMemoryUtilization` is 0.45. This gives a KV pool of approximately 99000 tokens.
The measured use of the pool is a maximum of 0.70. Thus the cache never becomes full and never
removes a prefix. Therefore the eviction policy cannot influence the results.

### The seeds: 20 seeds, 500 requests for each seed

A seed is a number that selects the workload. The generator is deterministic. The same seed
always gives the same 500 requests, in the same order, at the same times. Seed 7 on the
`kvaware` cell and seed 7 on the `loadaware` cell send exactly the same work. Therefore the two
cells can be compared directly, pair by pair.

One seed is one observation, not 500. The 500 requests inside a seed share one queue, so they
are not independent of each other. The statistics use 20 observations.

20 observations is a small number, so why not fewer? Because a small number of pairs cannot show
a difference at all. With 5 pairs, the best possible p-value is 0.031, which is already more than
our threshold of 0.025. Such a test cannot pass, whatever the data shows.

Our result also has two seeds that go the wrong way. With 10 pairs and two seeds in the wrong
direction, the p-value stays at 0.216. With 20 pairs the same pattern gives 0.0060.

Seeds are cheap. 500 requests at 16 requests for each second is approximately 30 seconds of
cluster time. More seeds is therefore the cheap way to make the test able to answer.

## Why you can trust the results

### The statistics were selected before the measurement

The test and the threshold are on issue
[#31](https://github.com/BenEpstein/caching-in-llms/issues/31). Both were agreed before the
first cell. The test is a one-sided exact Wilcoxon signed-rank test on the 20 paired seeds. The
threshold is 0.025. This is the Bonferroni correction for two primary measurements. The effect
size is the median relative reduction, with a bootstrap confidence interval of 95%.

The TTFT result was not significant. The report gives this null result. The project did not
replace the measurement with a different measurement.

### Each cell must pass the validity rules

1. Errors are counted, but they are not part of the latency statistics. An error rate that is
   different between the policies makes the comparison invalid. One seed with more than 10%
   errors makes the run invalid.
2. A run with the wrong image, the wrong configuration or the development overlay is discarded.
   The file `run.json` records the image and its digest.
3. A run without a successful registry probe and warm-up is not a measurement.
4. The workload checksum must be correct before each cell.
5. The KV pool size must agree with the configured value.
6. Preemptions are recorded and reported. They do not make a run invalid.

### The data is in the repository

Each run directory has the CSV file for each seed, the Prometheus data, the GPU data and
`run.json`. The file `run.json` records the policy, the beta value, the rate, the images with
their digests, the git commit, and the checksum of each seed file.

The command `./scripts/reproduce.sh` calculates each reported number again from these files. It
stops with an error if a number is different. CI runs this command for each change.

### Two sweeps give the same result

Generation 3 repeats generation 2 with the same images in a different time window. The load
imbalance result is 48.1% in generation 2 and 49.4% in generation 3.

## What a run directory contains

```
results/<sweep>/<timestamp>-<cell>/
  driver-seed1.csv ... driver-seed20.csv   one row for each request
  prom/*.json                              one file for each Prometheus metric
  dcgm.csv                                 the GPU data
  run.json                                 the provenance of the cell
  window.env                               the measurement window from the pod clock
```

One row from `driver-seed1.csv`:

```
index,prefix_id,send_ts,ttft_s,e2e_s,prompt_tokens,completion_tokens,status,error,itls_ms
0,106,1785960825.985649,0.0906933001242578,1.6519070861395448,1578,64,ok,,20.21;19.11;18.65;...
```

The column `ttft_s` is the time to the first token. The column `itls_ms` has the time between
each pair of tokens. The columns `status` and `error` show a request that failed.

The workload replay files and the raw Job log are not in the repository. You can make the replay
files again from the manifest. The Job log contains the same data as the CSV files.

The file `results/README.md` gives the index of the three sweeps.

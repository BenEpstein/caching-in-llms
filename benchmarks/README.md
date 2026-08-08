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
| `analyze.py` | Makes the summaries and the statistics. |
| `export_summary.py` | Makes `summary-per-seed.csv` for a sweep. |
| `plot_results.py` | Makes the 12 figures. |
| `utilization.py` | Makes the utilization report. |
| `load_gate.py` | Shows if the offered rate causes load on the servers. |
| `cold_start.sh` | Restarts the servers before a cell. |
| `collectors/prom_dump.py` | Gets the Prometheus data for the measurement window. |
| `collectors/dcgm_poll.py` | Gets the GPU data from the DCGM exporter. |

## How to run the benchmark

### Step 1: prepare the cluster

You need a Kubernetes or OpenShift cluster with two GPUs and the NVIDIA GPU Operator. You also
need `oc`, `helm` version 3, and a login with permission to make namespaces.

```bash
oc new-project cache-llm
helm repo add vllm https://vllm-project.github.io/production-stack
helm install stack vllm/vllm-stack -n cache-llm --version 0.1.11 \
  -f deploy/values-baseline-kvaware.yaml
oc get pods -n cache-llm -w
```

Use chart version 0.1.11. Later versions have a different schema.

To get access from outside the cluster, make the routes:

```bash
oc create route edge llm --service=stack-router-service --port=router-sport -n cache-llm
oc create route edge grafana --service=stack-grafana --port=http-web -n cache-llm
```

The certificate is self-signed. Use `curl -k`.

### Step 2: build the router image

CI builds the image for each change to `patches/`. It sends the image to Quay with the commit
SHA as the tag. Do not build the image on a laptop. The measurement must use the image that CI
built.

### Step 3: make the workload

```bash
python3 benchmarks/freeze_workloads.py
```

The command makes the workload files again from `workloads/manifest.json`. It stops with an
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

```bash
LOADAWARE_TAG=<image-sha> BENCH_TAG=<image-sha> benchmarks/run_sweep.sh 16
```

The variable `LOADAWARE_TAG` is the router image. The variable `BENCH_TAG` is the driver image.
Both are necessary.

The sweep runs seven cells: `kvaware`, then beta 0.5, 1.0, 2.0, 0.25 and 0, then `roundrobin`.
One cell takes approximately 20 minutes. A full sweep takes approximately 2.3 hours.

For each cell, `run_cell.sh` does these operations:

1. Deploys the cell with Helm.
2. Sets the beta value with `oc set env`.
3. Applies the patch for the router Service ports.
4. Compares the router image with the label of the cell.
5. Restarts the servers. Each cell starts with an empty cache.
6. Waits for the two servers to register.
7. Runs the registry probe.
8. Sends the warm-up requests.
9. Applies the Kubernetes Job. The Job sends the 20 seeds.
10. Collects the Prometheus data, the DCGM data and the manifest.
11. Checks the validity rules.

### Step 6: make the results

```bash
python3 benchmarks/export_summary.py results/<sweep>/* --out results/<sweep>/summary-per-seed.csv
python3 benchmarks/analyze.py compare results/<sweep>/<candidate> results/<sweep>/<baseline>
python3 benchmarks/plot_results.py results/<sweep>/* --cand loadaware-b0.5 \
  --comparator results/<sweep>/<roundrobin> --out docs/figures-<sweep>
python3 benchmarks/utilization.py report results/<sweep>/*
```

Write the figures to a directory for that sweep. Do not write them to `docs/figures/`. That
directory holds the figures for the reported sweep.

If a new sweep becomes the reported sweep, change the cell names in `scripts/reproduce.sh` in
the same commit. The names are `HEADLINE`, `BASELINE`, `ABLATION`, `BETA1`, `BETA2` and
`COMPARATOR`. If you do not change them, the script compares the old sweep and passes, and it
does not check the new figures.

## Known cluster problems

These problems are silent. The code contains the corrections.

| Problem | Correction |
|---|---|
| The Helm chart does not expose the LMCache controller ports 9001 and 9002. Registration stops and each lookup fails. | `run_cell.sh` applies a patch to the Service before each cell. |
| The router image and the server image can have different LMCache versions. The messages then fail. | Both images use the same LMCache version. The `Dockerfile` sets the version. |
| The router has no writable cache directory. The tokenizer fails for each request. The router then accepts approximately 4 requests for each second. | `run_cell.sh` sets `HF_HOME=/tmp/hf` on all cells. |
| After a router restart, the KV registry loses data for approximately 40 seconds. Both policies then look the same. | Each cell runs `deploy/dev/registry-probe.sh` before the measurement. |
| A rolling update does not complete, because the GPUs are full. | The values file uses the `Recreate` strategy. |

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

The percentiles come from the CSV rows. They do not come from a Prometheus histogram. Thus the
p95 and the p99 values are exact.

Two values are not available. vLLM has no `process_*` collector. Therefore the host CPU and the
host memory of the servers cannot be measured. The report gives them as missing. It does not
give a different value in their place. The GPU use is the utilization number for the servers.

Each `run.json` file records `utilization_coverage`. This is the fraction of the measurement
window that each metric covers. A value below 0.95 gives a warning. It does not stop the cell,
because the driver CSV files are the primary measurement.

## The figures

`plot_results.py` makes 12 figures.

| Figure | Contents |
|---|---|
| `fig1-ttft-p95-vs-beta` | The TTFT p95 for each value of beta |
| `fig2-ttft-ecdf` | The distribution of the TTFT for each policy |
| `fig3-hit-rate` | The LMCache lookup hit rate |
| `fig4-paired-seeds` | The paired seeds. This is the data of the statistical test. |
| `fig5-percentiles` | The TTFT percentiles for each policy |
| `fig6-load-balance` | The busiest server against the most idle server |
| `fig7-beta-tradeoff` | The latency against the cache hit rate |
| `fig8-itl-percentiles` | The inter-token latency percentiles |
| `fig9-throughput` | The tokens and the requests for each second |
| `fig10-utilization` | The GPU use, the GPU memory, the CPU and the host memory |
| `fig11-inflight-vs-time` | The requests in flight on each server against time |
| `fig12-goodput` | The goodput against the latency objective, from 50 ms to 400 ms |

## Why we selected these parameters

### The workload: 128 prefixes, Zipf s = 0.9

The workload has 128 shared prefixes. Each request selects a prefix with a Zipf distribution
(s = 0.9). The most frequent prefix receives 14.8% of the requests. The first three prefixes
receive 28.0%.

This gives cache reuse, but not extreme reuse. With extreme reuse, all requests go to one
server, and the comparison has no value.

Each prompt has 1578 tokens. The shared prefix has 1544 tokens. Thus 97.8% of each prompt can
come from the cache. The prompt length is the same for each request. Therefore the prompt length
cannot cause a difference between the policies.

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

One seed is one observation. The requests inside a seed are not independent, because they share
a queue. Therefore the statistics use 20 observations, not 10000.

The number 20 is not arbitrary. The exact Wilcoxon test has a minimum p-value that depends on
the number of pairs. With 3 pairs the minimum is 0.125. With 5 pairs it is 0.031. Both values
are more than the threshold of 0.025. Therefore these sizes cannot give a significant result.

Our result has two seeds in the opposite direction. With 10 pairs and two opposite seeds, the
p-value is 0.216 in the worst condition. With 20 pairs it is 0.0060. Therefore 20 seeds are
necessary for this result.

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

## The goodput metric

Goodput is the fraction of the sent requests that receive the first token before an objective.
The default objective is 150 ms. Change it with the option `--slo`.

```bash
python3 benchmarks/analyze.py compare --metric ttft_slo_miss --slo 0.15 <candidate> <baseline>
```

Goodput is a secondary measurement. The project did not select it before the measurement.
Therefore the report gives the full curve from 50 ms to 400 ms in `fig12-goodput`, and not one
value. An error counts as a late request.

## The second workload

The specification asks for two workload profiles. The second profile has prompts that are all
different. It measures the cost of the cache, not the benefit.

```bash
python3 benchmarks/freeze_workloads.py --profile novel
WORKLOAD_PROFILE=novel benchmarks/run_cell.sh <cell> <arm> <rate>
```

The profile is complete and has its own manifest. The project did not run a measurement with it.

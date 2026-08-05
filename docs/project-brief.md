# Project: Load-Aware Prefix Routing for vLLM Production Stack

> status: live · 2026-08-05 · design source of truth for the *shape* of the work; the policy formula,
> model and deploy figures were corrected against shipped code on 2026-08-05 (issue #29). Decisions
> made after 2026-08-01 live in tickets + CHANGELOG, not here.

> Handoff brief for Claude Code. This document is the source of truth for the project.
> Read it fully before writing code. Where it says "verify against current docs," do that —
> versions of the Production Stack, LMCache, and vLLM change often.

---

## 1. One-line summary

Extend the vLLM **Production Stack** router so it routes on **cache-hit benefit AND instance load**,
instead of cache-hit benefit alone. Show that this reduces tail latency under a skewed workload
without giving up most of the cache-hit-rate win.

This is a **university coursework** project. It does not need to be publishable research — it needs
to be a clean "reproduce baseline → add a feature → measure the improvement" story with graphs.

---

## 2. Context / who's running this

- Deploying on an **OpenShift** cluster with the **NVIDIA GPU Operator** already installed.
- **2 GPUs available** → 2 model replicas, one per GPU.
- Owner is comfortable with OpenShift/Kubernetes, GPU Operator, `oc`, SCCs, PVCs, and vLLM.
  Deployment is NOT the hard part; don't over-explain kubectl basics. Do surface OpenShift-specific
  gotchas (SCC/permissions, image pull, HF token secret, PVC sizing).

---

## 3. Background: how the system works today (so you know what you're changing)

Three layers, and they are **separate components** — this distinction is the whole project:

1. **vLLM engine** — a single serving process running one model on GPU(s). Vanilla vLLM has no
   concept of routing between replicas. It *does* have its own in-instance prefix caching, but that
   only reuses cache *within one instance*.

2. **Router** — a **separate pod** from a **separate project**, the **vLLM Production Stack**
   (`github.com/vllm-project/production-stack`, maintained by the LMCache team). It sits in front of
   the vLLM replicas and decides which replica each request goes to. It's CPU-only (no GPU), image
   `lmcache/lmstack-router`. **This is the pod we modify.**

3. **LMCache** — lives *inside* each vLLM instance. Stores KV blocks, offloads them GPU→CPU→disk,
   and shares them across instances. It exposes a control API (pin / **lookup** / cleanup / move /
   compress). The router's KV-aware mode uses LMCache's lookup info to know what each instance
   actually has cached.

### The two shipped affinity routers (this is the key contrast)

- **`prefixaware`** — router-side only. Hashes the prompt prefix and always sends the same prefix to
  the same instance, *even if that instance already evicted the cache*. Doesn't know real cache state.
- **`kvaware`** — routes to the instance with the **highest actual KV-cache hit rate**, using
  LMCache's knowledge of what's really cached (including offloaded-to-CPU/disk blocks). This is the
  one whose decision function we extend.

### The problem we solve

The `kvaware` router optimizes **only** for cache hits. It never looks at how busy each instance is.
Under a **skewed workload** (a few very popular prefixes — e.g. a shared system prompt), every
request with a hot prefix is sent to the *same* GPU because that's where the cache lives. Result:
one GPU builds a huge queue while the other sits idle. Great hit rate, terrible p95/p99 latency.
The router made a locally smart, globally dumb choice.

---

## 4. The feature we build

Modify the router's scoring so it weighs two things:

```
score(instance) =  cache_hit_benefit(instance, request)
                 -  β * relative_load(instance)
```

- `cache_hit_benefit` = the **fraction** of this request's prompt tokens the instance already has
  cached, `matched_tokens / prompt_tokens` (kvaware uses the raw count).
- `relative_load` = the instance's in-flight count as a signed fraction of the fleet mean,
  `(load − mean) / max(1, mean)`, where load is `in_prefill_requests + in_decoding_requests`.
  0.0 is "average", +1.0 is "twice the fleet average".
- `β` = the one tunable weight (env var `LOADAWARE_BETA`). Both terms are dimensionless, so β is a
  pure exchange rate that carries no unit from the deployment. Sweep it in experiments.
- **There is no α.** An argmax is invariant under positive scaling, so only the benefit-to-load
  ratio was ever a free parameter. See `patches/README.md`.

Behavior we want: when an instance gets too loaded, the router becomes willing to send some requests
to the *other* instance even at the cost of a cache miss — because avoiding a big queue beats one
cache hit.

**Implementation note:** first **locate the actual routing/scoring code** in the production-stack repo
(look under the router/`src` for the routing logic — routing strategies like `roundrobin`,
`session`, `prefixaware`, `kvaware` live together). Read how `kvaware` currently scores and where the
router already has per-instance state (it maintains an index of what each LMCache instance holds).
Add `loadaware` as a new routing logic option rather than mutating `kvaware`, so baselines stay intact.
Confirm what live load signal the router already has access to before inventing a new one.

---

## 5. Deployment plan (OpenShift)

Deploy the Production Stack via its Helm chart. Target topology:

```
                 ┌─────────────┐
   requests ───► │   Router    │  (CPU-only pod, lmcache/lmstack-router)
                 └──────┬──────┘
             ┌──────────┴──────────┐
             ▼                     ▼
      ┌─────────────┐       ┌─────────────┐
      │  vLLM #1    │       │  vLLM #2    │
      │ model+LMCache│      │ model+LMCache│
      │   GPU 1     │       │   GPU 2     │
      └─────────────┘       └─────────────┘
```

Sketch of the Helm `values.yaml` (verify field names against the current chart version):

```yaml
servingEngineSpec:
  modelSpec:
    - name: "qwen3b"
      repository: "lmcache/vllm-openai"
      tag: "latest"                       # pin a real version, don't ship :latest to a report
      modelURL: "Qwen/Qwen2.5-3B-Instruct"
      replicaCount: 2                     # one per GPU
      requestGPU: 1
      requestCPU: 8
      requestMemory: "16Gi"
      pvcStorage: "50Gi"                  # model weights
      vllmConfig:
        maxModelLen: 16384
        enablePrefixCaching: true
      lmcacheConfig:
        enabled: true
        cpuOffloadingBufferSize: "4"      # GB (node RAM is tight on gapu-2)
      hf_token: <use a secret, not inline>
routerSpec:
  repository: lmcache/lmstack-router
  tag: "latest"                           # this is the image we rebuild with our feature
  routingLogic: "kvaware"                 # baseline; ours will be "loadaware"
```

**Model sizing:** the shipped deployment runs `Qwen/Qwen2.5-3B-Instruct` (ungated, so no HF token)
on 2×A10 23 GB. An 8B model in FP16 needs ~16 GB and leaves too little headroom for the KV pool the
experiment depends on. Keep the model identical across all experiments so comparisons are fair.

**OpenShift specifics to handle:**
- SCC / non-root: the LMCache vLLM image may need a suitable SCC; prefer `oc adm policy` over making
  pods privileged. Do NOT run privileged.
- HF token as a `Secret`, referenced by the chart — never inline in `values.yaml` committed to git.
- PVC storage class must exist and support the requested size.
- Confirm the GPU Operator exposes `nvidia.com/gpu` and pods request `requestGPU: 1`.
- Router reaches engines via the Kubernetes Service / API-based discovery — make sure RBAC allows it.

Validate the baseline with two curls sharing a prefix and confirm (via router logs) the second lands
on the same instance as the first.

---

## 6. Experiment design

### Setups to compare (same model, same hardware, same workload)
1. `roundrobin` — dumb baseline, ignores cache.
2. `kvaware` — shipped smart baseline, great hit rate, pileup under skew.
3. `loadaware` — **our feature**.

(Optionally also `prefixaware` to show the evicted-cache weakness, if time allows.)

### Workload generator (build this — it's what makes the problem appear)
- **Zipfian prefix popularity:** a small set of very hot prefixes + a long tail of unique ones.
- Each prompt = long shared prefix (system prompt / RAG-style context, e.g. 1–4k tokens) +
  short unique suffix. Long shared prefixes are what make cache reuse valuable and make the
  overload visible.
- Tunable: concurrency level, Zipf skew parameter `s`, request rate, prefix pool size.
- Emit an OpenAI-compatible `/v1/completions` (or `/v1/chat/completions`) load against the router
  service. Consider `vllm bench`, `guidellm`, or a small async Python client (httpx + asyncio).

### Metrics to capture
- **TTFT** (time to first token) — mean and p95/p99.
- **End-to-end latency** — p50/p95/p99.
- **Throughput** (req/s and tokens/s) at fixed concurrency.
- **KV cache hit rate** (the stack exposes this).
- **Load balance** across the 2 replicas — e.g. request-count share and queue-depth variance over time.

The stack ships **Prometheus + Grafana**; scrape from there rather than hand-rolling metrics. Export
raw numbers to CSV so you can plot in matplotlib for the writeup.

### The graph that tells the story
Sweep the Zipf skew (and/or concurrency). Expect:
- `roundrobin`: low hit rate throughout.
- `kvaware`: best hit rate, but p99 latency blows up as skew increases (traffic jam on one GPU).
- `loadaware`: keeps most of the hit rate, p99 stays flat/controlled. **This crossover is the result.**

Also do a `β` sweep to show the tradeoff knob (β=0 at one extreme is pure-cache placement, large β
at the other approaches least-loaded).

---

## 7. Suggested phase order

1. **Deploy baseline** stack on OpenShift, 2 replicas + router, validate with curl. Confirm prefix
   affinity works.
2. **Wire up metrics** — confirm Prometheus/Grafana scrape TTFT, hit rate, per-instance load.
3. **Build the workload generator** (Zipfian, configurable).
4. **Run baselines** (`roundrobin`, `kvaware`), collect CSVs.
5. **Read the router source**, locate the scoring function and per-instance state.
6. **Implement `loadaware`** scoring as a new routing logic option.
7. **Rebuild only the router image**, push, redeploy just that pod (fast loop — model pods untouched).
8. **Run experiments**, sweep skew + `β`, collect data.
9. **Plot + write up** the comparison.

---

## 8. Scope / honesty notes

- The genuinely novel part is the **load-aware scoring** (steps 5–6) and the **experiment design**
  (step 6/8). Everything else is setup that should go quickly.
- "Load balancing / priority routing" is an acknowledged open item on the production-stack roadmap,
  so this is a real gap, not a solved problem — good for a coursework contribution.
- Keep the model, hardware, and prompts identical across all three setups or the comparison is invalid.
- Pin image versions; record exact versions of production-stack, LMCache, vLLM, and the model in the
  writeup for reproducibility.

---

## 9. Key resources

- Production Stack repo: https://github.com/vllm-project/production-stack
- KV-cache-aware routing docs: https://docs.vllm.ai/projects/production-stack (see "KV Cache Aware Routing")
- KV cache offloading (LMCache) docs: same docs site, "KV Cache Offloading"
- LMCache: https://github.com/LMCache/LMCache and https://docs.lmcache.ai
- LMCache paper (arXiv 2510.09665) for the control-API / lookup semantics

---

## 10. First actions for Claude Code

1. Clone `production-stack`, locate the router source and the routing-logic implementations
   (`roundrobin` / `session` / `prefixaware` / `kvaware`). Report back what load signal the router
   already has per instance.
2. Draft the minimal `values.yaml` for the 2-replica OpenShift deploy (with a Secret for the HF token),
   and flag any SCC/permissions steps needed for the LMCache vLLM image.
3. Propose the concrete `loadaware` scoring function based on what signals actually exist in the code,
   and where exactly it plugs in.

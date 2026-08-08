# Baseline Justification - vLLM Production Stack LoadAware Router

## The baseline

The baseline is the **vLLM Production Stack** with **LMCache**. vLLM is an open-source LLM
serving engine. The Production Stack deploys several vLLM servers behind one router on
Kubernetes. LMCache is the KV-cache layer of the stack: it stores the KV data of prompt
prefixes and reuses it across servers. The cache policy of this system has two parts. LMCache
decides what prefix data each server holds. The router decides which server serves each
request, and therefore which cached data a request can hit.

## Why this baseline

**Maturity and community support.** LMCache is the KV-cache backend that vLLM itself
integrates with. It ships an official Helm chart, a documented KV-aware routing tutorial,
router and engine images in matched version pairs, and Prometheus metrics on both tiers. Both
projects are under active development.

**Ease of modification.** Every routing strategy lives in one file,
`src/vllm_router/routers/routing_logic.py`: a `RoutingLogic` enum, one class per strategy with
a `route_request()` method, and a factory. Per-router unit tests show how to test a strategy
without a GPU. Every LMCache knob is set through environment variables, so configuration the
Helm chart does not expose is still reachable.

## Main features relevant to this project

- **Prefix-addressed KV cache.** LMCache stores KV data in chunks keyed by prompt prefix. A
  central controller tracks which server holds which chunk (`kv_controller.py`,
  `registry.find_kv`).
- **Multi-tier storage.** KV data can spill from GPU memory to CPU memory and disk.
- **A live load signal per server.** `EngineStats` scrapes `num_running_requests`,
  `num_queuing_requests`, and `gpu_cache_usage_perc` from each server. The router passes these
  values to every `route_request()` call.

## The default routing policies

The router ships four strategies: `roundrobin`, `session`, `prefixaware`, and `kvaware`. The
two that matter here:

- **`roundrobin`** sends requests to the servers in a fixed cycle. It balances load perfectly
  and ignores the cache completely. A request that has a cached prefix on server A can land on
  server B and recompute everything.
- **`kvaware`** asks the LMCache controller which server holds the longest cached prefix for
  each request, and routes the request there. It maximizes cache hits and ignores load
  completely. The load signal above reaches `KvawareRouter.route_request()` on every call, and
  the code uses none of it.

No shipped strategy combines the two signals. That is the policy gap this project fills: a
`loadaware` strategy that scores each server on cache benefit against live load.

## The gap in the cache layer

The controller's `lookup()` resolves each chunk through `registry.find_kv()`, which returns
only the **first** server that holds it. Upstream marks this in code: *"TODO: improve the
matching logic, return multi results."* The consequence: no router above the controller can
compare servers on cache benefit, because the match information is incomplete. This project
extends the lookup to per-server match info and adds the `loadaware` strategy on top.

## Alternatives considered

**GPTCache** is a semantic cache. It keys on full prompt-response pairs, so there is no
prefix-placement problem to route around. **vLLM's built-in prefix cache** works inside a
single server and has no cross-server view. Neither exposes the multi-server routing gap this
project targets, so neither supports the extension.

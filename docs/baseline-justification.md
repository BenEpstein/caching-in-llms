# Baseline Justification - vLLM Production Stack LoadAware Router

Ben Epstein and Eliad Bazak

## The idea

This project adds one routing policy, `loadaware`, to the vLLM Production Stack. The stock
router can maximize cache hits or balance load, but no shipped policy uses both signals.
`loadaware` scores each server on the cache benefit it offers a request against its live load,
and routes the request to the best score.

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
router and engine images in matched version pairs, and Prometheus metrics on both tiers.

**Ease of modification.** Every routing strategy lives in one file,
`src/vllm_router/routers/routing_logic.py`: a `RoutingLogic` enum, one class per strategy with
a `route_request()` method, and a factory. Per-router unit tests show how to test a strategy
without a GPU. Every LMCache knob is set through environment variables, so configuration the
Helm chart does not expose is still reachable.

**Measurable in a live deployment.** The stack installs with one Helm command and exposes
per-request metrics on both tiers through Prometheus. The `--routing-logic` flag swaps the
routing policy while the deployment, model, and workload stay identical. This supports a
controlled vanilla-against-extended comparison on a live cluster, which is how we will
evaluate the extension.

## Main features relevant to this project

- **Prefix-addressed KV cache.** LMCache stores KV data in chunks keyed by prompt prefix. A
  central controller tracks which server holds which chunk (`kv_controller.py`,
  `registry.find_kv`).
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

No shipped strategy combines the two signals. That is the policy gap `loadaware` fills.

## The gap in the cache layer

The controller's `lookup()` resolves each chunk through `registry.find_kv()`, which returns
only the **first** server that holds it. Upstream marks this in code: *"TODO: improve the
matching logic, return multi results."* The consequence: no router above the controller can
compare servers on cache benefit, because the match information is incomplete. This project
extends the lookup to per-server match info and adds the `loadaware` strategy on top.

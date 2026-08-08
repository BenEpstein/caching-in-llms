# §2 Baseline Justification — vLLM Production Stack + LMCache

> status: live · 2026-08-08 · the §2 deliverable, verified against `production-stack` @ `1e973a3`
> and `LMCache` @ `bf20f51`. Full file/line evidence: `docs/feasibility-verification.md`, deleted
> in #59, recoverable at `a137f5a`.

## Choice

**LMCache** as the caching library, deployed through the **vLLM Production Stack**: LMCache is the
KV-cache layer (storage, lookup, eviction, cross-instance movement) and production-stack supplies
the multi-instance router that decides *which* cache a request can hit.

## Against the two recommended criteria

**Maturity & community support.** LMCache is the KV-cache backend vLLM itself integrates with. It
ships an official Helm chart with a documented KV-aware tutorial, published router and engine
images in matched version pairs, and Prometheus metrics on both tiers. It is under active
development: we worked against live upstream TODOs and cite a concurrent upstream PR.

**Ease of modification.** Every routing strategy lives in one file,
`src/vllm_router/routers/routing_logic.py` — a `RoutingLogic` enum, one class per strategy
implementing `route_request()`, and an `initialize_routing_logic()` factory. Per-router unit tests
(`test_prefixaware_router.py`) show how to test one without a GPU, and every LMCache knob is
environment-variable driven, so configuration the chart does not expose is still reachable.

## Main features relevant to this project

- **Prefix-addressed KV cache** with chunk-level storage and a controller tracking which instance
  holds which chunk (`kv_controller.py`, `registry.find_kv`).
- **Multi-tier storage** — GPU, local CPU (`LocalCPUBackend`), and disk backends.
- **Cross-instance KV movement** — `MoveMsg(..., copy: bool)`; `copy=True` is replication, gated
  on `enable_p2p` plus a NIXL channel, which is why it stayed a stretch goal.
- **A live per-instance load signal that already exists and is already ignored.** `EngineStats`
  scrapes `num_running_requests`, `num_queuing_requests` and `gpu_cache_usage_perc` per instance
  and hands them to *every* `route_request()` call; `KvawareRouter` uses none of it. Our scoring
  function plugs in exactly there.

## Default eviction policy

**LRU.** `lmcache/v1/storage_backend/cache_policy/` defines `BaseCachePolicy`, with **LRU, LFU,
FIFO and MRU** registered in `POLICY_MAPPING` and selected by the `cache_policy` config key —
default `"LRU"`, overridable via `LMCACHE_CACHE_POLICY`. That cuts two ways: eviction is *already*
pluggable, so a new eviction policy would have been a one-file exercise against a solved
interface, and our experiments avoid eviction pressure by design (KV usage peaks near 0.70 and
never exhausts), so LRU-versus-anything is not a confound in our results.

## The gap we chose to close

The controller's `lookup()` (`kv_controller.py:388`) resolves each chunk through
`registry.find_kv()`, which returns the **first** instance holding it, in dictionary iteration
order. Upstream acknowledges this in code: *"TODO: improve the matching logic, return multi
results."* Two consequences define this project:

1. Per-instance cache-benefit information is incomplete, so no router above it can score
   instances against each other properly.
2. Cache-side replication alone cannot balance load — replicate a hot prefix onto both instances
   and lookup still names only one, so a stock KV-aware router keeps sending all hot traffic
   there. **Replication and routing have to be co-designed.**

An acknowledged gap in the cache layer, an unused load signal one layer up, and an additions-only
way to reach both: that combination, not the eviction policy, is why we chose this baseline. The
alternatives had no such gap to close — semantic caches (GPTCache and similar) key on
prompt-to-response pairs, so no distributed-placement problem arises, and vLLM's own in-engine
prefix cache is single-instance by construction: there is no placement decision inside one engine.

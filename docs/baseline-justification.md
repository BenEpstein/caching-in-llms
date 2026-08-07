# §2 Baseline Justification — vLLM Production Stack + LMCache

> status: live · 2026-08-07 · the §2 deliverable, and the only place the code claims below are
> pinned. Every file/line reference was verified against `vllm-project/production-stack` @
> `1e973a3` and `LMCache/LMCache` @ `bf20f51`; re-verify before citing against a newer upstream.

## Choice

**LMCache** as the caching library, deployed through the **vLLM Production Stack** — LMCache is
the KV-cache layer (storage, lookup, eviction, cross-instance movement) and production-stack
supplies the multi-instance router that decides *which* cache a request can hit.

## Against the two recommended criteria

**Maturity & community support.** LMCache is the KV-cache backend vLLM itself integrates with,
it ships an official Helm chart with a documented KV-aware tutorial
(`tutorials/assets/values-17-kv-aware.yaml`), published router and engine images with matched
version pairs, and Prometheus metrics on both tiers. It is under active development — we found
and worked against live upstream TODOs, and cite a concurrent upstream PR in our own findings.

**Ease of modification.** The extension points are isolated and already have a test pattern to
copy. All routing strategies live in one file, `src/vllm_router/routers/routing_logic.py`: a
`RoutingLogic` enum, one class per strategy implementing `RoutingInterface.route_request()`, and
an `initialize_routing_logic()` factory. Adding a strategy is an additions-only diff, and
per-router unit tests (`test_prefixaware_router.py`, `test_roundrobin_router.py`) show how to
test one without a GPU. On the cache side, every LMCache knob is environment-variable driven, so
config the chart doesn't expose can still be set per model.

## Main features relevant to this project

- **Prefix-addressed KV cache** with chunk-level storage and a controller that tracks which
  instance holds which chunk (`kv_controller.py`, `registry.find_kv`).
- **Multi-tier storage** — GPU, local CPU (`LocalCPUBackend`), and disk backends.
- **Cross-instance KV movement** — `MoveMsg(..., copy: bool)`; `copy=True` is replication.
  Gated on `enable_p2p` plus a NIXL transfer channel, which is why we scoped it as a stretch
  goal and did not ship it.
- **A live per-instance load signal that already exists and is already ignored.** `EngineStats`
  (`stats/engine_stats.py:30`) scrapes `num_running_requests`, `num_queuing_requests`, and
  `gpu_cache_usage_perc` per instance and hands them to *every* `route_request()` call — and
  `KvawareRouter` uses none of it. Our scoring function plugs in exactly there.

## Default eviction policy

**LRU.** `lmcache/v1/storage_backend/cache_policy/` defines `BaseCachePolicy`
(`init_mutable_mapping`, `update_on_hit`, `update_on_put`, `update_on_force_evict`,
`get_evict_candidates`), with **LRU, LFU, FIFO and MRU** registered in `POLICY_MAPPING` and
selected by the `cache_policy` config key — default `"LRU"`, overridable via
`LMCACHE_CACHE_POLICY`.

This matters to our choice in two ways. First, eviction is *already* pluggable, so a new
eviction policy would have been a one-file exercise against a solved interface — the
interesting unsolved problem sits one level up. Second, our experiments deliberately avoid
eviction pressure (measured KV usage peaks around 0.70 and never exhausts), so LRU-versus-
anything is not a confound in our results.

## The gap we chose to close

The controller's `lookup()` (`kv_controller.py:388`) resolves each chunk through
`registry.find_kv()`, which returns the **first** instance holding it, in dictionary iteration
order. Upstream acknowledges this in code: *"TODO: improve the matching logic, return multi
results."* Two consequences follow, and together they define this project:

1. Per-instance cache-benefit information is incomplete, so no router above it can score
   instances against each other properly.
2. Cache-side replication alone cannot balance load — replicate a hot prefix onto both
   instances and lookup still names only one, so a stock KV-aware router keeps sending all hot
   traffic there. **Replication and routing have to be co-designed.**

So the baseline offers a real, acknowledged gap in the cache layer, an unused load signal one
layer up, and a clean additions-only way to fix both. That combination — not the eviction
policy — is why we chose it.

## Alternatives considered

General-purpose semantic caches (GPTCache and similar) were rejected: they cache
prompt→response pairs, so the interesting distributed-placement problem does not arise, and
the improvement would have been a similarity-threshold tuning exercise. Modifying vLLM's own
in-engine prefix cache was rejected as single-instance by construction — there is no placement
decision to make inside one engine.

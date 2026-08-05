# Feasibility Verification - Code-Level Findings

> status: live · 2026-08-05 · raw material for the §2 baseline-justification deliverable; re-verify
> line refs against current upstream before citing. The scoring formula was corrected to the shipped
> policy on 2026-08-05 (issue #29); the upstream findings themselves are unchanged since 2026-07-04.

> Verified 2026-07-04 against `vllm-project/production-stack` @ `1e973a3` (2026-06-26)
> and `LMCache/LMCache` @ `bf20f51` (2026-07-04). All file/line references are to those commits.
>
> Purpose: ground the project design in what the code actually supports, before touching the cluster.
> This doubles as raw material for the §2 "baseline justification" deliverable.

## Summary

| Claim | Verdict | Risk |
|---|---|---|
| Router can be extended with a `loadaware` strategy | ✅ Verified | Low |
| Live per-instance load signal already exists in the router | ✅ Verified | Low |
| LMCache lookup gives per-instance cache-hit info | ⚠️ Partially — first-match only, upstream TODO to fix | Medium (and an opportunity) |
| Cross-instance KV replication (`move` with `copy=True`) | ✅ Implemented, needs P2P/NIXL | Medium-High |
| LMCache eviction policy is pluggable (fallback plan) | ✅ Verified | Low |

## 1. Router extension point (production-stack)

All routing strategies live in `src/vllm_router/routers/routing_logic.py`:
a `RoutingLogic` enum (`roundrobin`, `session`, `kvaware`, `prefixaware`, …), one class per
strategy implementing `RoutingInterface.route_request()`, and an `initialize_routing_logic()`
factory. Adding `loadaware` is an isolated addition; existing per-router unit tests
(`src/tests/test_prefixaware_router.py`, `test_roundrobin_router.py`, …) give the test pattern.

## 2. Load signal — already scraped, already delivered, already ignored

`EngineStats` (`src/vllm_router/stats/engine_stats.py:30`) is scraped from each engine's
Prometheus `/metrics` by a background thread and carries per instance:

- `num_running_requests` (`vllm:num_requests_running`)
- `num_queuing_requests` (`vllm:num_requests_waiting`)
- `gpu_cache_usage_perc` (`vllm:gpu_cache_usage_perc`)
- GPU prefix-cache hit counters

Every `route_request()` receives `engine_stats: Dict[url, EngineStats]`.
**`KvawareRouter` ignores it entirely** — it also ignores match-length ordering: it takes
`list(layout_info.keys())[0]`, i.e. the first instance LMCache happened to return
(`routing_logic.py:383-387`). The scoring function
`score = matched_tokens/prompt_tokens − β·relative_load` plugs in exactly there.

## 3. LMCache lookup semantics — the key discovery

The controller's `lookup()` (`lmcache/v1/cache_controller/controllers/kv_controller.py:388`)
walks token chunks and calls `registry.find_kv(key)`
(`cache_controller/utils.py:580`), which returns the **first instance** that holds the chunk,
in instance-dict iteration order. Upstream TODOs in the code acknowledge this:

> "It simply returns the `instance_id` with longest prefix."
> "TODO: improve the matching logic, return multi results"

Two consequences:

1. **Per-instance cache-benefit info is incomplete** — when several instances hold the same
   prefix, only one is reported. A proper `loadaware` score needs the lookup extended to
   return match info for *all* instances (or per-instance queries using the existing
   `exclude_instance_id` parameter). This is cache-layer work that fixes an acknowledged
   upstream TODO → strong PR candidate.
2. **Cache-side replication alone cannot balance load** — even with a hot prefix replicated
   on both instances, lookup deterministically reports one, so a stock kvaware router keeps
   sending all hot traffic to it. Replication and routing must be co-designed.

## 4. Cross-instance KV move/copy — exists, guarded by P2P

Full chain verified:

- `MoveMsg(old_position=(instance, location), new_position=(…), tokens, copy: bool)`
  (`cache_controller/message.py:614`) — `copy=True` keeps the source ⇒ replication.
- Executor fans out to source workers, push-based transfer to destination peer URL
  (`cache_controller/executor.py:281`).
- Worker handler → `cache_engine.move()` (`cache_engine.py:1247`) → `P2PBackend`
  `async_batched_submit_put_task` with `target_peer_init_url`.
- Destination limited to `LocalCPUBackend` ("Only support moving to cpu for now").
- Requires `enable_p2p=True`, which asserts controller enabled + `p2p_host` +
  `p2p_init_ports` + `p2p_lookup_ports` + `transfer_channel` (`config.py:732-741`).
- The only real transfer channel is **NIXL** (`transfer_channel/__init__.py:41` —
  `assert channel_type in ["nixl", "mock_memory"]`; mock is test-only).

Verdict: feasible as a stretch goal; gate on NIXL initializing correctly inside the
`lmcache/vllm-openai` image on the cluster before committing to it.

## 5. Pluggable eviction policy (fallback / complement)

`lmcache/v1/storage_backend/cache_policy/` defines `BaseCachePolicy`
(`init_mutable_mapping`, `update_on_hit`, `update_on_put`, `update_on_force_evict`,
`get_evict_candidates`) with LRU / LFU / FIFO / MRU registered in `POLICY_MAPPING` and
selected by the `cache_policy` config key (default `"LRU"`, env `LMCACHE_CACHE_POLICY`).
A new (e.g. cost-aware) policy = one file + one mapping entry + unit tests, runnable
offline without GPUs.

## 6. Helm / deployment surface

- Official kvaware tutorial values: `tutorials/assets/values-17-kv-aware.yaml`
  (2 replicas, LMCache controller ports, `p2pHost`/`p2pInitPorts`).
- Helm template maps `lmcacheConfig.*` → `LMCACHE_*` env vars
  (`helm/templates/deployment-vllm-multi.yaml:356-393`). Knobs the chart doesn't expose
  (`enable_p2p`, `transfer_channel`, `p2p_lookup_ports`, `cache_policy`) can be set via the
  per-model `env:` list since all LMCache config is env-var driven.
- Router needs `lmcache` importable (kvaware imports the controller manager) — true in the
  `lmcache/lmstack-router` image.

## 7. Cluster facts (gapu-2, OpenShift)

- 2× worker nodes, **1× NVIDIA A10 (23 GB) each**; both GPUs free.
- Default StorageClass `ocs-external-storagecluster-ceph-rbd` (Ceph RBD).
- Logged in as cluster admin; NVIDIA GPU Operator exposes `nvidia.com/gpu`.
- A10 sizing: an 8B model in FP16 (~16 GB) fits but leaves little KV headroom;
  a 3–7B model leaves much more room for cache effects.

## Resulting project shape

1. **LMCache (cache layer):** extend controller lookup to return per-instance match info
   — fixes upstream TODO, PR candidate.
2. **Router:** new `loadaware` strategy scoring cache benefit vs. live load
   (β tunable via the `LOADAWARE_BETA` env var; there is no α).
3. **Stretch:** hot-prefix replication via `MoveMsg(copy=True)` (gated on NIXL check).
4. **Fallback:** cost-aware `BaseCachePolicy` implementation.

# Upstream Findings - Production-Stack / LMCache Control Plane

> status: live · 2026-08-01 · input to the upstream-PRs ticket (#10); findings dated 2026-07-04, re-verify against current upstream before filing

> Material collected during baseline deployment (2026-07-04) for two later uses:
> 1. the **final report** (Experimental Setup / Discussion sections — these findings show
>    baseline fragility and motivate parts of our design), and
> 2. **upstream issues/PRs** (the rubric's grade-100 path).
>
> Versions examined: production-stack @ `1e973a3` (chart 0.1.11, router images
> `lmstack-router:latest` = lmcache 0.3.11 and `0.1.9.dev9-g37bafbcf5.d20260107` =
> lmcache 0.3.9post2), LMCache @ `bf20f51`, engine images `vllm-openai:v0.3.9post2`
> and `v0.5.1rc2`. Cluster: OpenShift `gapu-2`, 2× NVIDIA A10.

---

## Finding 1 — One-shot worker registration: router restart silently disables kvaware

### The control-plane flow

```
Engine pod (vLLM + LMCache worker)          Router pod (LMCache controller)
────────────────────────────────           ────────────────────────────────
1. engine starts, LMCache init
2. worker thread starts
3. sends RegisterMsg ──────ZMQ──────────►  registration_controller stores
   (instance_id=pod name, ip, port)          {instance_id → socket/URL}
                                             in an IN-MEMORY dict
4. serves requests; every KV
   store/evict ────────────ZMQ push─────►  kv_controller updates in-memory
   (KVAdmitMsg / KVEvictMsg)                 index: "chunk hash X → instance"

                  per request: router's kvaware lookup reads that index
                  → "who has this prefix?" → route there
```

> **CORRECTED 2026-08-01.** The original version of this finding claimed registration is
> one-shot and never recovers. That is **wrong when the worker heartbeat is enabled** —
> see "Correction" below. The finding survives in a narrower, stronger form: the *default*
> configuration leaves the heartbeat off, and that is the actual upstream defect.

Two design properties (verified in `lmcache/v1/cache_controller/worker.py` and
`controllers/registration_controller.py`, lmcache 0.3.9post2):

- **The initial `RegisterMsg` is a one-shot boot action.** Sent exactly once at
  engine-process boot; there is no retry loop on the registration path itself.
- **The controller's registry is process memory.** No persistence; the registry *is* the
  router process.

### Correction — the heartbeat *does* re-register (verified live 2026-08-01)

`registration_controller.py:176-192` handles `HeartbeatMsg`: if the worker key is absent
from `worker_info_mapping` it logs *"has not been registered, re-register the worker"* and
calls `await self.register(msg)`. The worker side (`worker.py:201-224`) only emits
heartbeats when `lmcache_worker_heartbeat_time` is set and `> 0`.

**Measured on `gapu-2`:** with `servingEngineSpec…workerHeartbeatTime: "30"` set (our
`values-baseline-kvaware.yaml:58`), a `rollout restart` of the router alone — engines
untouched — had both workers re-registered within ~30 s, and end-to-end serving resumed
with no engine restart.

**Consequence for the dev loop: a router-only restart is ~60 s, not a 3-4 min engine
reload.** This is what makes iterating on `loadaware` in-cluster practical.

### The real upstream defect (narrower, still worth filing)

`workerHeartbeatTime` is **not** a chart default — we set it explicitly. An out-of-the-box
`vllm-stack` deployment therefore runs with the heartbeat off, where the one-shot behaviour
above *does* apply and the silent-failure chain below is exactly right. So the defensible
upstream framing is:

> With `kvaware` routing enabled, the chart should default `workerHeartbeatTime` to a
> non-zero value (or refuse to start kvaware without it), because otherwise any router
> restart silently and permanently degrades kvaware to QPS routing.

One-line chart default + a docs note; much easier to merge than a protocol change.

### What happens on router restart

```
Engine pod (still healthy)                  NEW router process
────────────────────────────────           ────────────────────────────────
   worker keeps pushing KV events ──ZMQ──► registry: {} (fresh process)
                                           "unknown instance" → event DROPPED
                                           (internal False, no error reply)
   worker notices NOTHING:
   - ZMQ auto-reconnects silently
   - pushes are fire-and-forget (no acks)

                  per request: lookup → empty index → never a match
                  → SILENT fallback to QPS routing
```

**Every layer fails silently**: ZMQ buffers/reconnects by design, pushes carry no acks, the
controller quietly ignores unknown instances, and the router treats "no match" as a normal
cache miss. Zero error lines anywhere. The only symptom is behavioral — prefix affinity
disappears — diagnosable via the router's `/metrics`
(`lmcache:cache_controller_registered_workers_count`, newer routers only) or by absent
`found by kvaware router` log lines.

### Operational consequence + workaround

**Only when the heartbeat is disabled.** In that configuration every router restart
requires restarting all engine pods so their init-time registration re-runs:
`oc rollout restart deployment/stack-llm-deployment-vllm -n cache-llm` (~3–4 min for 2×
model reload). **With `workerHeartbeatTime` set — our configuration — this is not needed;
the router restarts alone and workers re-register within one heartbeat interval.**

### Report/PR angle

- Report: baseline fragility; motivates measuring router availability as part of the story.
  The honest version of the story is the *default-configuration* hazard, not a missing
  mechanism — the mechanism exists and works.
- Upstream PR candidate (revised): make the chart default `workerHeartbeatTime` non-zero
  when `routingLogic: kvaware`, so the silent-degradation path is unreachable by default.
  **Do not** file "workers never re-register" — that claim is false and would be rejected
  on sight.

---

## Finding 2 — Helm chart: router Service omits controller reply/heartbeat ports (upstream bug, PR planned)

Routers ≥ ~0.1.10 split the controller across three sockets: 9000 (pull — KV events),
9001 (reply — **registration round-trip**), 9002 (heartbeat). The chart's
`service-router.yaml` exposes only 9000 while the same chart's values wire the engines to
`{release}-router-service:9001` — so registration hangs forever inside ZMQ, silently
(same failure surface as Finding 1: no errors anywhere).

Workaround (helm's 3-way merge preserves it across upgrades):

```bash
oc patch svc stack-router-service -n cache-llm --type=json -p '[
 {"op":"add","path":"/spec/ports/-","value":{"name":"lmcache-reply","port":9001,"targetPort":9001,"protocol":"TCP"}},
 {"op":"add","path":"/spec/ports/-","value":{"name":"lmcache-heartbeat","port":9002,"targetPort":9002,"protocol":"TCP"}}]'
```

**Planned PR:** add the two ports to `service-router.yaml`, parameterized on the existing
`routerSpec.lmcacheControllerReplyPort` / `lmcacheControllerHeartbeatPort` values (only
rendered when set — backward compatible).

---

## Finding 3 — No current official image pairing is protocol-compatible (incl. the official tutorial)

The controller (router) and workers (engines) exchange msgspec-tagged ZMQ structs; schema
drift across lmcache versions breaks decoding **silently** (messages counted on the socket,
zero handled).

Measured matrix (all official images):

| Router image | lmcache | Engine image | lmcache | Registration |
|---|---|---|---|---|
| `lmstack-router:latest` (2026-06-25) | 0.3.11 | `vllm-openai:v0.3.9post2` | 0.3.9post2 | ✗ (msg to wrong socket/schema) |
| `lmstack-router:latest` | 0.3.11 | `vllm-openai:v0.5.1rc2` | 0.5.1rc2 | ✗ (arrives on 9001, fails decode) |
| `lmstack-router:0.1.9.dev9-g37bafbcf5.d20260107` | 0.3.9post2 | `vllm-openai:v0.3.9post2` | 0.3.9post2 | ✅ **validated** |

Why no current pairing works: production-stack's `pyproject.toml` pinned
`lmcache==0.3.9post2` until 2026-01-14 (`5694032b` bumped to 0.3.11), but engine image
releases jumped 0.3.9post2 → 0.5.x — **no official engine image ever carried 0.3.11**.
The official kvaware tutorial (`values-17-kv-aware.yaml`: engine `v0.3.9post2` + router
`latest`) is therefore broken as shipped today.

### Report/PR angle

- Report: reproducibility section — exact image pins are load-bearing, and we validate the
  pairing empirically (`Registered instance-worker` in logs; affinity test).
- Upstream issue: document the incompatibility + ask for (a) a compatibility matrix or
  (b) a protocol-version handshake that logs a LOUD error on mismatch instead of silence.
  The `Dockerfile.kvaware` pattern (router built FROM the engine image, sharing its
  lmcache) is upstream's own alignment mechanism — but its base is stale
  (`2025-05-27-v1`) and the published router images don't use it.

---

## Finding 4 — Silent-failure design pattern (Discussion-section material)

Common thread of findings 1–3: LMCache's control plane degrades **silently by design** at
every layer (ZMQ reconnect semantics, fire-and-forget events, "no match = cache miss").
The router always *works* in the load-balancer sense while the entire KV-awareness layer is
dead. For a production system this is arguably the wrong default: the feature the operator
paid for (cache-aware routing) can be off for weeks without any signal.

Concrete diagnostics we relied on (worth standardizing in our benchmark harness):

1. `lmcache:cache_controller_registered_workers_count` == number of engines (newer routers).
2. Router logs contain `Registered instance-worker` per engine after every (re)start.
3. Router logs contain `found by kvaware router` under a shared-prefix probe
   (2 requests, same ≥1k-token prefix, 5s apart → same instance + latency drop).

Our baseline validation (2026-07-04): probe showed instance affinity and 0.83s → 0.39s
latency on the prefix hit.

---

## Finding 5 - Our load term is a request count; the state of the art counts unique KV blocks (Discussion-section material)

> Verified 2026-08-04 against `ai-dynamo/dynamo` @ `main`:
> `lib/kv-router/src/scheduling/selector.rs` (the cost function),
> `sequences/prompt_registry.rs` + `sequences/multi_worker.rs` (load accounting),
> `scheduling/config.rs` (defaults). Newer than the 2026-07-04 findings above.

NVIDIA's Dynamo KV-router is the closest production system to `loadaware`, and it has the same
skeleton: a cache credit subtracted from a load cost, minimized over workers. `selector.rs:262`:

```
logit(w) = prefill_load_scale · max(0, raw_prefill_blocks − overlap_credit_blocks)
         + potential_decode_blocks(w)
         + decode_active_request_weight · active_requests(w)
```

Three differences that matter to us, in descending order:

1. **Load is unique KV blocks, not a request count.** `potential_decode_blocks = active_blocks +
   new_blocks`, where `active_blocks` is documented as the worker's "unique active decode load in
   blocks" (`prompt_registry.rs:54`) and `new_blocks = query_len − overlap_depth` is only the
   candidate's blocks *not already resident*. Two in-flight requests sharing a prefix pin one copy,
   not two. `decode_active_request_weight` - the term closest to ours - defaults to **0.0**.
   Prompt blocks are counted; output blocks are not (`router_track_output_blocks: false`).
2. **Cache credit is tiered by location**: device 1.0, host 0.75, disk 0.25. We receive `location`
   in `layout_info` and discard it.
3. **Ties are broken randomly** (reservoir sampling, `selector.rs:464`), with an optional
   `router_temperature` for softmax sampling. We break ties lexicographically, which is a
   systematic bias toward one engine whenever benefit ties - the likely mechanism behind
   `loadaware-b0`'s imbalance of 3.1–5.2.

Units also differ: their terms are all blocks, so `prefill_load_scale = overlap_score_credit = 1.0`
are meaningful defaults; our score adds a fraction in [0,1] to a request count, so β is an exchange
rate that has to be swept. Converting β to their axis needs the blocks-per-in-flight-request factor,
which prefix dedup makes workload- and rate-dependent (0.69 at 10.5 req/s, 0.45 at 7.5), so the two
parameterizations are **not** related by a constant. Under that approximation their default sits near
β ≈ 0.45–0.7, past the optimum our sweep found (`fig7-beta-tradeoff.png`).

### Drop-in Discussion paragraph (§6)

> Our load term, `in_prefill + in_decoding`, is a count of in-flight requests, and it is the
> coarsest part of the policy. NVIDIA's Dynamo router, the closest production system to our design,
> uses the same overall shape - a cache credit subtracted from a load cost, minimized over workers - > but denominates both terms in KV blocks and counts only *unique* blocks: two in-flight requests
> sharing a prefix pin one copy of that prefix's KV, not two. Our formulation cannot express this.
> It charges full price for a request that adds almost no memory to an engine already holding its
> prefix, pushing traffic away from the cheapest available placement and working against the
> locality the policy exists to exploit. Reconstructing the in-flight set from our driver logs, the
> workload carried 30.3 concurrent requests against 20.9 distinct prefixes at 10.5 req/s (18.1
> against 8.2 at 7.5), so between 31% and 55% of what we counted as load was memory that already
> existed. The refinement does not bind at our operating point: KV utilization peaked at 33%, so no
> block was ever evicted and locality was never scarce - round-robin reaches the same 0.95 lookup
> hit rate as both cache-aware arms - and `num_requests_waiting` was 0 throughout, so no queue
> formed that a better load estimate could have shortened. We therefore expect deduplicated-block
> accounting to matter only under cache scarcity, where a count-based load term begins to fight the
> cache it is meant to exploit. That is the natural next experiment rather than a defect in the
> present result.

Evidence for the numbers: dedup factor from `results/2026*/driver-seed*.csv` (in-flight set
reconstructed from `send_ts` + `e2e_s`, distinct `prefix_id` counted); KV utilization and
`num_requests_waiting` from `results/2026*/prom/`; hit rates from
`results/2026*/prom/lmcache_lookup_hit_rate.json`.

---

## Affinity probe (reusable snippet)

```python
# 3 requests sharing a ~1200-token prefix through the router; expect:
# request 1 → fallback (cold), requests 2-3 → "found by kvaware router", same instance
import json, urllib.request, random, time
random.seed(7)
words = "system context document retrieval answer question cache policy latency".split()
prefix = "[AFFINITY-PROBE] " + " ".join(random.choices(words, k=1200))
for i, suffix in enumerate(["Q one?", "Q two?", "Q three?"]):
    body = json.dumps({"model": MODEL, "prompt": f"{prefix} {suffix}",
                       "max_tokens": 8, "temperature": 0}).encode()
    t0 = time.time()
    urllib.request.urlopen(urllib.request.Request(
        f"{ROUTER}/v1/completions", body, {"Content-Type": "application/json"})).read()
    print(f"{suffix!r}: {time.time()-t0:.2f}s")
    time.sleep(5 if i == 0 else 2)
# then: oc logs deploy/stack-deployment-router | grep -E "Routing request|found by kvaware"
```

# Upstream Findings — Production-Stack / LMCache Control Plane

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

Two design properties (verified in `lmcache/v1/cache_controller/worker.py` and
`controllers/registration_controller.py`):

- **Registration is a one-shot `__init__` action.** `RegisterMsg` is sent exactly once at
  engine-process boot. No retry loop, no periodic re-announcement. Newer versions add a
  heartbeat, but it does not recreate a lost registration (measured: after a router
  restart, `registered_workers_count` stays 0 while heartbeats flow).
- **The controller's registry is process memory.** No persistence; the registry *is* the
  router process.

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

Every router restart (crash, probe kill, redeploy of a modified router — i.e., **our whole
loadaware dev loop**) requires restarting all engine pods so their init-time registration
re-runs: `oc rollout restart deployment/stack-llm-deployment-vllm -n cache-llm`
(~3–4 min for 2× model reload).

### Report/PR angle

- Report: baseline fragility; motivates measuring router availability as part of the story.
- Upstream PR candidate: worker re-registration — the heartbeat response already carries a
  command channel (`HeartbeatRetMsg.commands`); a controller that answers an unknown
  worker's heartbeat with a "re-register" command closes the loop with no new protocol.

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

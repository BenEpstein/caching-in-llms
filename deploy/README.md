# Deployment (OpenShift, cluster `gapu-2`)

Target topology: 1 CPU router pod → 2 vLLM+LMCache replicas, one per A10 GPU.

## Prereqs

- `oc` logged in with rights to create namespaces/SCC bindings
- `helm` v3
- A HuggingFace token **if** the model is gated (Llama family is)

## Steps

```bash
# 1. Namespace
oc new-project cache-llm

# 2. HF token secret (only for gated models)
oc create secret generic hf-token-secret \
  --from-literal=HF_TOKEN=<your token> -n cache-llm

# 3. Install the stack (pin the chart version you actually used!)
helm repo add vllm https://vllm-project.github.io/production-stack
helm install stack vllm/vllm-stack -n cache-llm \
  -f values-baseline-kvaware.yaml \
  --set "servingEngineSpec.modelSpec[0].modelURL=<MODEL>" \
  --set "servingEngineSpec.modelSpec[0].hf_token.secretName=hf-token-secret" \
  --set "servingEngineSpec.modelSpec[0].hf_token.secretKey=HF_TOKEN"

# 4. Watch
oc get pods -n cache-llm -w
```

## Validate prefix affinity (baseline sanity check)

```bash
oc port-forward svc/stack-router-service 8000:80 -n cache-llm &

# two requests sharing a long prefix — second must land on the same pod (router logs)
curl -s localhost:8000/v1/completions -H 'Content-Type: application/json' -d '{
  "model": "<MODEL>", "prompt": "<LONG SHARED PREFIX> question one", "max_tokens": 10}'
curl -s localhost:8000/v1/completions -H 'Content-Type: application/json' -d '{
  "model": "<MODEL>", "prompt": "<LONG SHARED PREFIX> question two", "max_tokens": 10}'

oc logs deploy/stack-router -n cache-llm | grep -i "routing"
```

## Operational gotchas (learned the hard way)

0. **UPSTREAM CHART BUG — router Service missing controller ports.** The chart's
   `service-router.yaml` exposes only 9000 (pull), but LMCache workers register on the
   **reply port 9001** and heartbeat on 9002. Result: registration hangs silently,
   `registered_workers_count` stays 0, all KV-admit events are rejected, every lookup
   misses, and kvaware degrades to QPS routing with **no error anywhere**. Diagnose via
   the router *logs*: count `Registered instance-worker` lines (the pinned 0.1.9-era
   router exposes **no** `lmcache:cache_controller_registered_workers_count` gauge on
   `/metrics` - verified live 2026-08-01).
   Fix (re-apply after every `helm upgrade` — helm reverts it):

   ```bash
   oc patch svc stack-router-service -n cache-llm --type=json -p '[
    {"op":"add","path":"/spec/ports/-","value":{"name":"lmcache-reply","port":9001,"targetPort":9001,"protocol":"TCP"}},
    {"op":"add","path":"/spec/ports/-","value":{"name":"lmcache-heartbeat","port":9002,"targetPort":9002,"protocol":"TCP"}}]'
   ```

   Then restart the engines (gotcha #1). → Good candidate for an upstream PR.
   (In practice helm's 3-way merge preserves the added ports across upgrades;
   re-check with `oc get svc stack-router-service -o yaml` after chart-version bumps.)

0b. **UPSTREAM PITFALL — lmcache version skew router↔engine.** The controller (router)
   and workers (engines) speak msgspec-tagged ZMQ structs; schema drift between lmcache
   versions makes registration fail **silently** (message stuck on the socket, zero
   errors logged). We hit it with `lmstack-router:latest` (lmcache 0.3.11) vs
   `vllm-openai:v0.3.9post2` (lmcache 0.3.9.post2). Rule: pin engine + router images
   so both carry the same lmcache minor version. Check with:
   `oc exec <pod> -- /opt/venv/bin/python3 -c "from importlib.metadata import version; print(version('lmcache'))"`

1. **Router restart re-registers the workers — but ONLY because `workerHeartbeatTime` is set,
   and the KV registry stays blind for ~40 s afterwards (issue #13).**
   *(Corrected twice on 2026-08-01. Re-registration self-heals. `kv_pool` does not come back
   with it: it is in-memory, admission is one-shot per chunk, and admits are lost until both
   workers re-register — so anything first stored in that window is invisible to the
   Controller for the life of the engine process. No engine restart needed; gate measurements
   on `deploy/dev/registry-probe.sh`.)*
   The controller re-registers unknown workers when it receives their heartbeat
   (`registration_controller.py:176-192`), and the worker only sends heartbeats when
   `lmcache_worker_heartbeat_time > 0`. Our `values-baseline-kvaware.yaml:58` sets
   `workerHeartbeatTime: "30"`, so a router-only restart recovers in ~30 s with engines
   untouched — **verified live**. Keep that value set; if it is ever removed, the old
   failure mode returns: the registry comes back empty, every KV lookup misses, and
   kvaware silently degrades to QPS routing.
   Symptom of the broken state: router logs show `Routing request ... with session id None`
   alternating, and no `found by kvaware router` lines. Recovery in that case:
   `oc rollout restart deployment/stack-llm-deployment-vllm -n cache-llm`.
2. **RollingUpdate deadlocks on full GPUs** — values set `strategy: Recreate`.
3. **Arbitrary UID:** `HOME=/` is not writable; values set `HOME=/tmp` or flashinfer
   crashes the engine core at import.
4. **Router startup probe:** kvaware init takes ~20s+; chart default kills it. Values
   relax `failureThreshold`.
5. **Shared model PVC across nodes needs RWX** (CephFS), not the default ceph-rbd RWO.

## OpenShift notes

- The `lmcache/vllm-openai` image runs as non-root; if pods fail with SCC errors, grant
  `anyuid` is NOT the first resort — check the actual UID requirement first:
  `oc adm policy who-can use scc/anyuid`, prefer `nonroot-v2`.
- PVCs use the default StorageClass (`ocs-external-storagecluster-ceph-rbd`).
- Router discovers engines via the K8s API — the chart's RBAC handles this; verify with
  `oc auth can-i list pods --as=system:serviceaccount:cache-llm:stack-router-service-account`.

## Access (no port-forward needed, VPN required)

```bash
oc create route edge llm --service=stack-router-service --port=router-sport -n cache-llm
oc create route edge grafana --service=stack-grafana --port=http-web -n cache-llm
```

- Model API: https://llm-cache-llm.apps.gapu-2.customers.k8s.co.il/v1 (OpenAI-compatible, no key)
- Grafana:   https://grafana-cache-llm.apps.gapu-2.customers.k8s.co.il (admin / cache-llm)

Self-signed cert → `curl -k` / disable TLS verify in clients. Anyone on the cluster
VPN can reach both.

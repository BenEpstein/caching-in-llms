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

1. **Router restart ⇒ engine restart.** LMCache workers register with the router's
   controller once at engine startup and never re-register. If the router pod restarts
   (crash, redeploy of our loadaware image, probe kill), its worker registry comes back
   empty, every KV lookup misses, and kvaware silently degrades to QPS routing —
   requests alternate between instances. Fix:
   `oc rollout restart deployment/stack-llm-deployment-vllm -n cache-llm`
   after every router restart. Symptom to check: router logs show
   `Routing request ... with session id None` alternating, and no
   `found by kvaware router` lines.
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

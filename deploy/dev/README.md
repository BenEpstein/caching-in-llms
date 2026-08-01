# Dev loop — iterating on router / LMCache code in-cluster

Validated end-to-end on `gapu-2` 2026-08-01.

## Why this exists

The contribution (`loadaware` placement + the multi-instance lookup) lives entirely in the
**router pod**: both `vllm_router` and the LMCache `cache_controller` are installed there as
plain Python under `/opt/venv/lib/python3.12/site-packages/`. Two facts make a fast loop
possible without building a single image:

1. **A ConfigMap mounted with `subPath` replaces one installed file**, even though
   `site-packages` is root-owned and the pod runs as an arbitrary UID — the kubelet performs
   the mount, so pod write permissions are irrelevant.
2. **A router-only restart re-registers the workers.** They re-register from their heartbeat
   within ~30 s (see `../README.md` gotcha #1), so the model is never reloaded.

Result: **edit → live on cluster in ~60 s** — but see the KV-registry caveat below before
you trust a routing observation.

## ⚠️ Re-registration self-heals; the KV registry does not

Observed 2026-08-01 (issue #13). The Controller's `kv_pool` is in-memory, so a router
restart empties it — and the engines never re-announce what they already hold. Across three
router pods the engines stored new chunks four times while the Controller stayed at
`pool_size=0` and received zero admits; only an **engine restart** brought admissions back.

So after every `apply-router-patch.sh` **and** every `revert-router-patch.sh`:

```bash
oc rollout restart deploy/stack-llm-deployment-vllm -n cache-llm
oc rollout status  deploy/stack-llm-deployment-vllm -n cache-llm --timeout=600s
```

then re-warm before you read anything into `layout_info`. In practice the loop is ~7 min,
not ~60 s, whenever the observation depends on cache-hit info. A `layout_info={}` on a prefix
you know is cached means the registry is empty, not that the lookup is broken.

**Forcing a two-holder prefix** (needed to see multi-instance lookup do anything): fire two
*concurrent* cold requests with the same >2000-token prefix. With `kv_pool` empty for it,
`kvaware` falls back to QPS routing and splits them across both engines, so both cache it;
a third request then observes both holders.

## Usage

```bash
# 1. pull the stock file out of the image (once per file you intend to change)
oc exec -n cache-llm deploy/stack-deployment-router -- \
  cat /opt/venv/lib/python3.12/site-packages/vllm_router/routers/routing_logic.py \
  > work/routing_logic.py

# 2. edit work/routing_logic.py locally

# 3. push it and wait for re-registration
./apply-router-patch.sh work/routing_logic.py

# 4. observe
oc logs -f deploy/stack-deployment-router -n cache-llm | grep -iE "kvaware|loadaware|layout_info"
```

Multiple files at once are fine: `./apply-router-patch.sh work/routing_logic.py work/kv_controller.py`

`apply-router-patch.sh` only accepts files listed in its `PATCH_TARGETS` map — the mount
path has to be the file's real location inside the image, so add new targets there first.

## ⚠️ Revert before measuring

```bash
./revert-router-patch.sh
```

The overlay is invisible in `helm list` and survives router restarts. **Any baseline number
measured while a patch is mounted is invalid.** Revert first, confirm the router is stock,
then measure.

## Limits — this is a dev loop, not the deliverable

- Not reproducible for a third party: it mutates a live Deployment out-of-band from Helm.
- ConfigMaps cap at ~1 MB total.
- `helm upgrade` may drop the patch (the volume is an out-of-band addition), which is
  harmless — reapply.

The **§6 deliverable** is a real image: `FROM lmcache/lmstack-router:0.1.9.dev9-g37bafbcf5.d20260107`
plus `COPY` of the changed files, pushed to a registry the cluster can pull, and referenced
via `routerSpec.repository`/`tag`. That still needs a container runtime and a registry —
neither is set up yet (no docker/podman locally; no exposed internal registry on `gapu-2`).
Track it as its own task; it is not needed to start writing code.

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
2. **A router-only restart self-heals.** Workers re-register from their heartbeat within
   ~30 s (see `../README.md` gotcha #1), so engines are never restarted and the model is
   never reloaded.

Result: **edit → live on cluster in ~60 s.**

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

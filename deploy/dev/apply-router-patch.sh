#!/usr/bin/env bash
# Overlay locally-modified router/LMCache Python files onto the running router pod.
#
# The router image is pure Python, so a ConfigMap mounted with subPath can replace
# individual installed files without rebuilding an image or touching the engines.
# Engines re-register via heartbeat (~30s), so this is a ~60s dev loop.
#
# Usage:  ./apply-router-patch.sh <file> [<file> ...]
# Revert: ./revert-router-patch.sh
#
# Only files whose basename matches an entry in PATCH_TARGETS can be applied — the
# mount path must be the file's real location inside the image.
set -euo pipefail

NS="${NS:-cache-llm}"
DEPLOY=stack-deployment-router
CONTAINER=router-container
CM=router-patch
SP=/opt/venv/lib/python3.12/site-packages

# basename -> absolute path inside the router image
declare -A PATCH_TARGETS=(
  [routing_logic.py]="$SP/vllm_router/routers/routing_logic.py"
  [kv_controller.py]="$SP/lmcache/v1/cache_controller/controllers/kv_controller.py"
  [registration_controller.py]="$SP/lmcache/v1/cache_controller/controllers/registration_controller.py"
)

[ $# -gt 0 ] || { echo "usage: $0 <file> [<file> ...]" >&2; exit 1; }

cm_args=(); mounts=()
for f in "$@"; do
  b=$(basename "$f")
  [ -f "$f" ] || { echo "no such file: $f" >&2; exit 1; }
  [ -n "${PATCH_TARGETS[$b]:-}" ] || {
    echo "unknown target '$b' — add it to PATCH_TARGETS first" >&2; exit 1; }
  cm_args+=(--from-file="$b=$f")
  mounts+=("{\"name\":\"$CM\",\"mountPath\":\"${PATCH_TARGETS[$b]}\",\"subPath\":\"$b\",\"readOnly\":true}")
done

echo "==> ConfigMap/$CM"
oc create configmap "$CM" -n "$NS" "${cm_args[@]}" --dry-run=client -o yaml | oc apply -f -

echo "==> mounting over: $*"
oc patch deploy "$DEPLOY" -n "$NS" --type=strategic -p "{
  \"spec\":{\"template\":{\"spec\":{
    \"volumes\":[{\"name\":\"$CM\",\"configMap\":{\"name\":\"$CM\"}}],
    \"containers\":[{\"name\":\"$CONTAINER\",\"volumeMounts\":[$(IFS=,; echo "${mounts[*]}")]}]
  }}}}"

# ConfigMap content changes alone do not restart the pod; force it.
oc rollout restart "deploy/$DEPLOY" -n "$NS"
oc rollout status "deploy/$DEPLOY" -n "$NS" --timeout=180s

echo "==> waiting for both workers to re-register (heartbeat, ~30s)"
until [ "$(oc logs "deploy/$DEPLOY" -n "$NS" 2>/dev/null \
          | grep -c 'Registered instance-worker')" -ge 2 ]; do sleep 5; done
oc logs "deploy/$DEPLOY" -n "$NS" | grep 'Registered instance-worker' | tail -2

echo "==> patched router is live"

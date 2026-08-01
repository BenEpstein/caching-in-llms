#!/usr/bin/env bash
# Remove the router code overlay and return the router to the stock pinned image.
# ALWAYS run this before a baseline measurement run.
set -euo pipefail

NS="${NS:-cache-llm}"
DEPLOY=stack-deployment-router
CM=router-patch

echo "==> removing volumeMount + volume"
oc patch deploy "$DEPLOY" -n "$NS" --type=json -p '[
  {"op":"remove","path":"/spec/template/spec/containers/0/volumeMounts"},
  {"op":"remove","path":"/spec/template/spec/volumes"}
]' 2>/dev/null || echo "   (already clean)"

oc delete configmap "$CM" -n "$NS" --ignore-not-found
oc rollout status "deploy/$DEPLOY" -n "$NS" --timeout=180s

echo "==> confirming stock code is back (expect 0 matches)"
oc exec -n "$NS" "deploy/$DEPLOY" -- \
  grep -c PATCHED /opt/venv/lib/python3.12/site-packages/vllm_router/routers/routing_logic.py \
  2>/dev/null || echo "0 — stock"

echo "==> waiting for worker re-registration"
until [ "$(oc logs "deploy/$DEPLOY" -n "$NS" 2>/dev/null \
          | grep -c 'Registered instance-worker')" -ge 2 ]; do sleep 5; done
echo "==> baseline router restored"

#!/usr/bin/env bash
# Per-cell choreography for the benchmark sweep (methodology, issue #3):
#
#   helm upgrade → service-port patch → ENGINE restart (#13: cold, identical
#   initial state) → wait for worker registration → registry probe (#13 gate)
#   → warm-up passes gated on non-empty layout_info → replay the 6 frozen
#   seeds back-to-back at the fixed rate → Prometheus dump + DCGM CSV +
#   run.json manifest → validity check.
#
# Usage:
#   ./run_cell.sh <cell> <rate> [results-root]
#
#   cell ∈ kvaware | roundrobin | loadaware-b<beta>     (α fixed at 1.0)
#          e.g. loadaware-b0 loadaware-b0.1 loadaware-b0.5 loadaware-b1.0
#   rate = fixed open-loop Poisson req/s (from rate_pilot.sh, ~75% of the knee)
#
# Environment overrides (defaults match deploy/README.md on gapu-2):
#   NS, RELEASE, CHART, BASE_URL, MODEL, ROUTER_DEPLOY, ENGINE_DEPLOY,
#   LOADAWARE_TAG (REQUIRED for loadaware cells: git short SHA of the CI image)
set -euo pipefail

CELL="${1:?usage: run_cell.sh <cell> <rate> [results-root]}"
RATE="${2:?usage: run_cell.sh <cell> <rate> [results-root]}"
RESULTS_ROOT="${3:-results}"

BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$BENCH_DIR/.." && pwd)"

NS="${NS:-cache-llm}"
RELEASE="${RELEASE:-stack}"
CHART="${CHART:-vllm/vllm-stack}"
BASE_URL="${BASE_URL:-https://llm-cache-llm.apps.gapu-2.customers.k8s.co.il}"
MODEL="${MODEL:-Qwen/Qwen2.5-3B-Instruct}"
ROUTER_DEPLOY="${ROUTER_DEPLOY:-stack-deployment-router}"
ENGINE_DEPLOY="${ENGINE_DEPLOY:-stack-llm-deployment-vllm}"
PROM_PORT="${PROM_PORT:-19090}"
DCGM_PORT="${DCGM_PORT:-19400}"

# ---- cell → helm args -------------------------------------------------------
HELM_ARGS=(-f "$REPO_ROOT/deploy/values-baseline-kvaware.yaml")
ALPHA="" BETA="" ARM=""
case "$CELL" in
  kvaware)
    ARM=kvaware ;;
  roundrobin)
    ARM=roundrobin
    HELM_ARGS+=(--set routerSpec.routingLogic=roundrobin) ;;
  loadaware-b*)
    ARM=loadaware
    ALPHA="1.0"
    BETA="${CELL#loadaware-b}"
    : "${LOADAWARE_TAG:?loadaware cells need LOADAWARE_TAG=<git short SHA of the CI-built image>}"
    HELM_ARGS+=(
      -f "$REPO_ROOT/deploy/values-loadaware-image.yaml"
      --set routerSpec.tag="$LOADAWARE_TAG"
      --set "routerSpec.env[0].name=LOADAWARE_ALPHA"
      --set-string "routerSpec.env[0].value=$ALPHA"
      --set "routerSpec.env[1].name=LOADAWARE_BETA"
      --set-string "routerSpec.env[1].value=$BETA"
    ) ;;
  *)
    echo "unknown cell: $CELL" >&2; exit 2 ;;
esac

OUT="$RESULTS_ROOT/$(date +%Y%m%d-%H%M%S)-$CELL"
mkdir -p "$OUT"
echo "==> cell $CELL (arm=$ARM alpha=${ALPHA:-n/a} beta=${BETA:-n/a}) rate=$RATE → $OUT"

PIDS=()
cleanup() { for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done; }
trap cleanup EXIT

# ---- 0. frozen workloads verified ------------------------------------------
python3 "$BENCH_DIR/freeze_workloads.py"

# ---- 1. deploy the arm ------------------------------------------------------
helm upgrade --install "$RELEASE" "$CHART" -n "$NS" "${HELM_ARGS[@]}"

# ---- 2. router Service controller ports (deploy/README.md gotcha #0) --------
if ! oc get svc "$RELEASE-router-service" -n "$NS" -o jsonpath='{.spec.ports[*].name}' \
    | grep -q lmcache-reply; then
  oc patch svc "$RELEASE-router-service" -n "$NS" --type=json -p '[
    {"op":"add","path":"/spec/ports/-","value":{"name":"lmcache-reply","port":9001,"targetPort":9001,"protocol":"TCP"}},
    {"op":"add","path":"/spec/ports/-","value":{"name":"lmcache-heartbeat","port":9002,"targetPort":9002,"protocol":"TCP"}}]'
fi

# ---- 3. cold start: restart engines so every cell begins with empty caches --
oc rollout status "deploy/$ROUTER_DEPLOY" -n "$NS" --timeout=10m
oc rollout restart "deploy/$ENGINE_DEPLOY" -n "$NS"
oc rollout status "deploy/$ENGINE_DEPLOY" -n "$NS" --timeout=30m

# ---- 4. wait until both workers are registered ------------------------------
echo "==> waiting for 2 registered workers"
for _ in $(seq 60); do
  count=$(curl -ks -m 10 "$BASE_URL/metrics" \
    | awk '/^lmcache:cache_controller_registered_workers_count/ {print $2}' | head -1)
  [ "${count%%.*}" = "2" ] && break
  sleep 5
done
[ "${count%%.*}" = "2" ] || { echo "workers never registered (count=$count)" >&2; exit 1; }

# ---- 5. registry probe (#13) - skip on roundrobin (routing ignores the registry)
if [ "$ARM" != "roundrobin" ]; then
  "$REPO_ROOT/deploy/dev/registry-probe.sh" "$(date +%s)"
fi

# ---- 6. warm-up over the prefix pool, gated on non-empty layout_info --------
WARMUP_START=$(date +%s)
python3 "$BENCH_DIR/warmup.py" --base-url "$BASE_URL" --model "$MODEL" --insecure
if [ "$ARM" != "roundrobin" ]; then
  since=$(( $(date +%s) - WARMUP_START + 5 ))
  hits=$(oc logs "deploy/$ROUTER_DEPLOY" -n "$NS" --since="${since}s" \
    | grep -c "found by .* router" || true)
  if [ "$hits" -eq 0 ]; then
    echo "warm-up gate FAILED: no 'found by … router' lines - layout_info empty, do not measure" >&2
    exit 1
  fi
  echo "==> warm-up gate ok ($hits cache-path routings)"
fi

# ---- 7. collectors ----------------------------------------------------------
oc port-forward -n "$NS" svc/stack-prometheus "$PROM_PORT:9090" >/dev/null 2>&1 &
PIDS+=($!)
oc port-forward -n nvidia-gpu-operator svc/nvidia-dcgm-exporter "$DCGM_PORT:9400" >/dev/null 2>&1 &
PIDS+=($!)
sleep 3
python3 "$BENCH_DIR/collectors/dcgm_poll.py" \
  --url "http://localhost:$DCGM_PORT/metrics" --out "$OUT/dcgm.csv" &
PIDS+=($!)

# ---- 8. measured replay: 6 frozen seeds back-to-back ------------------------
CELL_START=$(date +%s)
for seed in 1 2 3 4 5 6; do
  echo "==> seed $seed / 6"
  python3 "$BENCH_DIR/load_driver.py" \
    --base-url "$BASE_URL" --model "$MODEL" --insecure \
    --workload "$BENCH_DIR/workloads/seed-$seed.jsonl" \
    --rate "$RATE" --seed "$seed" \
    --out "$OUT/driver-seed$seed.csv" \
    --summary-json "$OUT/summary-seed$seed.json"
done
CELL_END=$(date +%s)

# ---- 9. Prometheus dump over the measurement window -------------------------
python3 "$BENCH_DIR/collectors/prom_dump.py" \
  --prom-url "http://localhost:$PROM_PORT" \
  --start "$CELL_START" --end "$CELL_END" --out "$OUT/prom"

# ---- 10. run manifest -------------------------------------------------------
ROUTER_IMAGE=$(oc get deploy "$ROUTER_DEPLOY" -n "$NS" \
  -o jsonpath='{.spec.template.spec.containers[0].image}')
ROUTER_IMAGE_ID=$(oc get pods -n "$NS" -l "$(oc get deploy "$ROUTER_DEPLOY" -n "$NS" \
  -o jsonpath='{.spec.selector.matchLabels}' \
  | python3 -c 'import json,sys; print(",".join(f"{k}={v}" for k,v in json.load(sys.stdin).items()))')" \
  -o jsonpath='{.items[0].status.containerStatuses[0].imageID}' 2>/dev/null || echo unknown)
GIT_COMMIT=$(git -C "$REPO_ROOT" rev-parse HEAD)
export CELL ARM ALPHA BETA RATE CELL_START CELL_END ROUTER_IMAGE ROUTER_IMAGE_ID GIT_COMMIT OUT BENCH_DIR
python3 - <<'PY'
import glob, json, os
out = os.environ["OUT"]
seeds = {}
for path in sorted(glob.glob(os.path.join(out, "summary-seed*.json"))):
    seeds[os.path.basename(path)[len("summary-seed"):-len(".json")]] = json.load(open(path))
manifest = json.load(open(os.path.join(os.environ["BENCH_DIR"], "workloads", "manifest.json")))
run = {
    "cell": os.environ["CELL"],
    "arm": os.environ["ARM"],
    "alpha": os.environ["ALPHA"] or None,
    "beta": os.environ["BETA"] or None,
    "rate_req_s": float(os.environ["RATE"]),
    "window": {"start_ts": int(os.environ["CELL_START"]), "end_ts": int(os.environ["CELL_END"])},
    "router_image": os.environ["ROUTER_IMAGE"],
    "router_image_id": os.environ["ROUTER_IMAGE_ID"],
    "git_commit": os.environ["GIT_COMMIT"],
    "workload_manifest": manifest,
    "seed_windows": seeds,
}
with open(os.path.join(out, "run.json"), "w") as f:
    json.dump(run, f, indent=2)
print(f"wrote {out}/run.json")
PY

# ---- 11. validity gate ------------------------------------------------------
python3 "$BENCH_DIR/analyze.py" validate "$OUT"
echo "==> cell $CELL complete: $OUT"

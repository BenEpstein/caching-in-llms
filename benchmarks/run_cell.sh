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
#   cell ∈ kvaware | roundrobin | loadaware-b<beta>
#          e.g. loadaware-b0 loadaware-b0.25 loadaware-b1.0 loadaware-b2.0
#          beta is dimensionless (load is normalized against the fleet mean
#          inside the router), so the grid is fixed, not probe-calibrated.
#   rate = fixed open-loop Poisson req/s (from rate_pilot.sh, ~75% of the knee)
#
# Environment overrides (defaults match deploy/README.md on gapu-2):
#   NS, RELEASE, CHART, CHART_VERSION, BASE_URL, MODEL, ROUTER_DEPLOY,
#   ENGINE_DEPLOY, LOADAWARE_TAG (REQUIRED for loadaware cells: git short SHA
#   of the CI image)
#
# Verified live on gapu-2 2026-08-01: chart 0.1.11 ignores `routerSpec.env`
# (hardcoded env list), so α/β travel via `oc set env` after the upgrade; the
# router exposes NO registered-workers gauge in this build, so registration is
# gated on the router's "Registered instance-worker" log lines instead.
set -euo pipefail

CELL="${1:?usage: run_cell.sh <cell> <rate> [results-root]}"
RATE="${2:?usage: run_cell.sh <cell> <rate> [results-root]}"
RESULTS_ROOT="${3:-results}"

# Which frozen seeds this cell replays. The inferential cells run 20 (n=6 cannot
# survive a single reversal - the pilot proved it - and n=10 then returned
# p=0.0527); the beta-sweep cells run a 3-seed subset of the SAME frozen files,
# so there is still exactly one dataset.
SEEDS="${SEEDS:-1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20}"

BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$BENCH_DIR/.." && pwd)"

NS="${NS:-cache-llm}"
RELEASE="${RELEASE:-stack}"
CHART="${CHART:-vllm/vllm-stack}"
# Pin the chart: the cluster runs 0.1.11 and 0.1.12+ has schema drift - an
# unpinned upgrade would silently migrate the stack mid-experiment.
CHART_VERSION="${CHART_VERSION:-0.1.11}"
BASE_URL="${BASE_URL:-https://llm-cache-llm.apps.gapu-2.customers.k8s.co.il}"
MODEL="${MODEL:-Qwen/Qwen2.5-3B-Instruct}"
ROUTER_DEPLOY="${ROUTER_DEPLOY:-stack-deployment-router}"
ENGINE_DEPLOY="${ENGINE_DEPLOY:-stack-llm-deployment-vllm}"
PROM_PORT="${PROM_PORT:-19090}"
DCGM_PORT="${DCGM_PORT:-19400}"

# ---- cell → helm args -------------------------------------------------------
# USES_LOOKUP: does this arm route via the KV registry? Gates the registry
# probe and the layout_info warm-up check; roundrobin ignores the registry so
# neither signal exists there.
HELM_ARGS=(-f "$REPO_ROOT/deploy/values-baseline-kvaware.yaml")
BETA="" ARM="" USES_LOOKUP=1
case "$CELL" in
  kvaware)
    ARM=kvaware ;;
  roundrobin)
    ARM=roundrobin
    USES_LOOKUP=0
    HELM_ARGS+=(--set routerSpec.routingLogic=roundrobin) ;;
  loadaware-b*)
    ARM=loadaware
    BETA="${CELL#loadaware-b}"
    : "${LOADAWARE_TAG:?loadaware cells need LOADAWARE_TAG=<git short SHA of the CI-built image>}"
    HELM_ARGS+=(
      -f "$REPO_ROOT/deploy/values-loadaware-image.yaml"
      --set routerSpec.tag="$LOADAWARE_TAG"
    ) ;;
  *)
    echo "unknown cell: $CELL" >&2; exit 2 ;;
esac

OUT="$RESULTS_ROOT/$(date +%Y%m%d-%H%M%S)-$CELL"
mkdir -p "$OUT"
echo "==> cell $CELL (arm=$ARM beta=${BETA:-n/a}) rate=$RATE → $OUT"

PIDS=()
cleanup() { for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done; }
trap cleanup EXIT

# ---- 0. frozen workloads verified ------------------------------------------
python3 "$BENCH_DIR/freeze_workloads.py"

# ---- 1. deploy the arm ------------------------------------------------------
helm upgrade --install "$RELEASE" "$CHART" -n "$NS" --version "$CHART_VERSION" "${HELM_ARGS[@]}"

# validity rule 2: a mounted dev overlay would invalidate every number - strip it
if oc get deploy "$ROUTER_DEPLOY" -n "$NS" \
    -o jsonpath='{.spec.template.spec.volumes[*].name}' | grep -q router-patch; then
  echo "==> dev overlay mounted on the router - reverting before measuring"
  NS="$NS" "$REPO_ROOT/deploy/dev/revert-router-patch.sh"
fi

# α/β travel by env var; chart 0.1.11 has no routerSpec.env passthrough. Set
# them explicitly on loadaware cells and REMOVE them on baselines - the
# three-way merge preserves out-of-band env across upgrades, so a stale β
# would otherwise leak between cells.
# HF_HOME rides along on BOTH arms (#21). The router image has no writable HF
# cache (arbitrary uid under the restricted SCC, HF_HOME unset, no /.cache), so
# `AutoTokenizer.from_pretrained` fails on every request - and because the
# except path never assigns self.tokenizer, it is retried per request at
# ~245 ms a time, reaching huggingface.co before it fails. That alone is ~0.25 s
# of event-loop blocking per request, i.e. a ~4 req/s ceiling and the liveness
# SIGKILL. /tmp is the only writable path: chart 0.1.11 gives the router neither
# a `routerSpec.env` passthrough nor any volume hook, so this cannot live in
# values. Set on baselines too - the fix must be arm-neutral or it flatters us.
if [ "$ARM" = "loadaware" ]; then
  oc set env "deploy/$ROUTER_DEPLOY" -n "$NS" \
    HF_HOME=/tmp/hf "LOADAWARE_BETA=$BETA"
else
  oc set env "deploy/$ROUTER_DEPLOY" -n "$NS" \
    HF_HOME=/tmp/hf LOADAWARE_BETA-
fi

# ---- 2. router Service controller ports (deploy/README.md gotcha #0) --------
if ! oc get svc "$RELEASE-router-service" -n "$NS" -o jsonpath='{.spec.ports[*].name}' \
    | grep -q lmcache-reply; then
  oc patch svc "$RELEASE-router-service" -n "$NS" --type=json -p '[
    {"op":"add","path":"/spec/ports/-","value":{"name":"lmcache-reply","port":9001,"targetPort":9001,"protocol":"TCP"}},
    {"op":"add","path":"/spec/ports/-","value":{"name":"lmcache-heartbeat","port":9002,"targetPort":9002,"protocol":"TCP"}}]'
fi

# ---- 3. cold start: restart engines so every cell begins with empty caches --
oc rollout status "deploy/$ROUTER_DEPLOY" -n "$NS" --timeout=10m

# validity rule 2: wrong image = discard, never correct. Assert the deployed
# router matches the cell's label before anything is measured.
ROUTER_IMAGE=$(oc get deploy "$ROUTER_DEPLOY" -n "$NS" \
  -o jsonpath='{.spec.template.spec.containers[0].image}')
if [ "$ARM" = "loadaware" ]; then
  WANT="quay.io/rhl193000/lmstack-router-loadaware:$LOADAWARE_TAG"
  [ "$ROUTER_IMAGE" = "$WANT" ] || { echo "router image $ROUTER_IMAGE != $WANT" >&2; exit 1; }
else
  case "$ROUTER_IMAGE" in
    lmcache/lmstack-router:*) ;;
    *) echo "baseline cell but router image is $ROUTER_IMAGE" >&2; exit 1 ;;
  esac
fi

# Drain-then-restart, NOT `rollout restart`: the old engine pods would otherwise
# still be alive while the new router comes up, register, and leave dead
# instance_ids in kv_pool - which the stock kvaware router turns into
# KeyError -> HTTP 500 on any lookup that returns one. See cold_start.sh.
# ---- 4. cold, stale-free start (drain -> router -> engines -> registration) --
# cold_start.sh owns the registration wait and the stale-id assertion; on
# roundrobin there is no LMCache controller, so neither applies.
NS="$NS" ROUTER_DEPLOY="$ROUTER_DEPLOY" ENGINE_DEPLOY="$ENGINE_DEPLOY" \
  EXPECT_REGISTRATIONS="$USES_LOOKUP" "$BENCH_DIR/cold_start.sh"

# ---- 5. registry probe (#13) - only meaningful on lookup-routing arms -------
if [ "$USES_LOOKUP" = 1 ]; then
  "$REPO_ROOT/deploy/dev/registry-probe.sh" "$(date +%s)"
fi

# ---- 6. warm-up over the prefix pool, gated on non-empty layout_info --------
WARMUP_START=$(date +%s)
python3 "$BENCH_DIR/warmup.py" --base-url "$BASE_URL" --model "$MODEL" --insecure
if [ "$USES_LOOKUP" = 1 ]; then
  # window starts AT warm-up start: probe traffic just before it also logs
  # "found by" lines and must not satisfy this gate
  since=$(( $(date +%s) - WARMUP_START )); [ "$since" -lt 1 ] && since=1
  hits=$(oc logs "deploy/$ROUTER_DEPLOY" -n "$NS" --since="${since}s" \
    | grep -c "found by .* router" || true)
  if [ "$hits" -eq 0 ]; then
    echo "warm-up gate FAILED: no 'found by … router' lines - layout_info empty, do not measure" >&2
    exit 1
  fi
  echo "==> warm-up gate ok ($hits cache-path routings)"
fi

# ---- 7. collectors ----------------------------------------------------------
# NOTE: no Prometheus port-forward here. prom_dump uses query_range over a past
# window, so it only needs Prometheus reachable at DUMP time - holding a
# forward open across the whole cell just gives it ~10 min to die. It did
# (2026-08-03: `Connection refused` after the b0 seeds, which under `set -e`
# killed the cell AND the remaining three cells of the sweep). Established in
# step 9 instead, with retries. DCGM genuinely needs a live forward because it
# polls continuously.
# DCGM is a DaemonSet: forward each pod on its own port, or one node's GPU is lost
DCGM_URLS=()
port="$DCGM_PORT"
for pod in $(oc get pods -n nvidia-gpu-operator -l app=nvidia-dcgm-exporter \
    -o jsonpath='{.items[*].metadata.name}'); do
  oc port-forward -n nvidia-gpu-operator "pod/$pod" "$port:9400" >/dev/null 2>&1 &
  PIDS+=($!)
  DCGM_URLS+=(--url "http://localhost:$port/metrics")
  port=$((port + 1))
done
sleep 3
python3 "$BENCH_DIR/collectors/dcgm_poll.py" \
  "${DCGM_URLS[@]}" --out "$OUT/dcgm.csv" &
PIDS+=($!)

# ---- 8. measured replay: the cell's frozen seeds back-to-back ---------------
CELL_START=$(date +%s)
SEED_COUNT=$(echo "$SEEDS" | wc -w | tr -d ' ')
for seed in $SEEDS; do
  echo "==> seed $seed / $SEED_COUNT"
  python3 "$BENCH_DIR/load_driver.py" \
    --base-url "$BASE_URL" --model "$MODEL" --insecure \
    --workload "$BENCH_DIR/workloads/seed-$seed.jsonl" \
    --rate "$RATE" --seed "$seed" \
    --out "$OUT/driver-seed$seed.csv"
done
CELL_END=$(date +%s)

# ---- 9. Prometheus dump over the measurement window -------------------------
# Forward now, not at cell start: a short-lived forward is a reliable one. Retry
# because a single refused connection here would otherwise discard a cell whose
# measurements are already complete and on disk.
prom_dumped=0
for attempt in 1 2 3; do
  oc port-forward -n "$NS" svc/stack-prometheus "$PROM_PORT:9090" >/dev/null 2>&1 &
  pf_pid=$!
  PIDS+=("$pf_pid")
  sleep 5
  if python3 "$BENCH_DIR/collectors/prom_dump.py" \
      --prom-url "http://localhost:$PROM_PORT" \
      --start "$CELL_START" --end "$CELL_END" --out "$OUT/prom"; then
    prom_dumped=1
    break
  fi
  echo "==> prometheus dump attempt $attempt failed; retrying" >&2
  kill "$pf_pid" 2>/dev/null || true
  PROM_PORT=$((PROM_PORT + 1))   # a wedged local port would fail identically
done
[ "$prom_dumped" = 1 ] || echo "WARNING: no Prometheus dump for $OUT - driver CSVs are still valid, but the imbalance co-primary cannot be computed for this cell" >&2

# ---- 10. run manifest -------------------------------------------------------
# per-seed windows are derivable from each driver CSV's send_ts column
ROUTER_IMAGE_ID=$(oc get pods -n "$NS" -l "$(oc get deploy "$ROUTER_DEPLOY" -n "$NS" \
  -o jsonpath='{.spec.selector.matchLabels}' \
  | python3 -c 'import json,sys; print(",".join(f"{k}={v}" for k,v in json.load(sys.stdin).items()))')" \
  -o jsonpath='{.items[0].status.containerStatuses[0].imageID}' 2>/dev/null || echo unknown)
GIT_COMMIT=$(git -C "$REPO_ROOT" rev-parse HEAD)
export CELL ARM BETA RATE CELL_START CELL_END ROUTER_IMAGE ROUTER_IMAGE_ID GIT_COMMIT OUT BENCH_DIR
python3 - <<'PY'
import json, os
env = os.environ
manifest = json.load(open(os.path.join(env["BENCH_DIR"], "workloads", "manifest.json")))
run = {
    "cell": env["CELL"],
    "arm": env["ARM"],
    "beta": env["BETA"] or None,
    "rate_req_s": float(env["RATE"]),
    "window": {"start_ts": int(env["CELL_START"]), "end_ts": int(env["CELL_END"])},
    "router_image": env["ROUTER_IMAGE"],
    "router_image_id": env["ROUTER_IMAGE_ID"],
    "git_commit": env["GIT_COMMIT"],
    "workload_manifest": manifest,
}
with open(os.path.join(env["OUT"], "run.json"), "w") as f:
    json.dump(run, f, indent=2)
print(f"wrote {env['OUT']}/run.json")
PY

# ---- 11. validity gate ------------------------------------------------------
python3 "$BENCH_DIR/analyze.py" validate "$OUT"
echo "==> cell $CELL complete: $OUT"

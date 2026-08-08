#!/usr/bin/env bash
# Per-cell choreography for the benchmark sweep (methodology, issue #3).
# The step order and why each gate exists: benchmarks/README.md, "Step 5: run the
# sweep" - the numbered sections below follow it.
#
# Usage:
#   ./run_cell.sh <cell> <rate> [results-root]
#
#   cell ∈ kvaware | roundrobin | loadaware-b<beta>
#          e.g. loadaware-b0 loadaware-b0.5 loadaware-b1.0 loadaware-b2.0
#          beta is dimensionless (load is normalized against the fleet mean
#          inside the router), so the grid is fixed, not probe-calibrated.
#   rate = fixed open-loop Poisson req/s (from rate_pilot.sh, at or just under the knee)
#
# Environment overrides (defaults match our cluster; see the top-level README, "Our
# environment"):
#   NS, RELEASE, CHART, CHART_VERSION, BASE_URL, MODEL, ROUTER_DEPLOY, ROUTER_REPO,
#   ENGINE_DEPLOY, LOADAWARE_TAG (REQUIRED for loadaware cells: git short SHA
#   of the CI image)
set -euo pipefail

CELL="${1:?usage: run_cell.sh <cell> <rate> [results-root]}"
RATE="${2:?usage: run_cell.sh <cell> <rate> [results-root]}"
RESULTS_ROOT="${3:-results}"

# BENCH_TAG is checked here rather than in bench_job.sh so the cell fails in the first second
# instead of minutes into helm upgrade, cold start and warm-up.
: "${BENCH_TAG:?every cell needs BENCH_TAG=<git short SHA of the CI-built bench image>}"

# Which frozen seeds this cell replays. EVERY cell runs the full 20 (run_sweep.sh SEEDS_FULL),
# curve arms included - smaller n cannot survive a reversal (derivation: benchmarks/README.md,
# "The seeds: 20 seeds, 500 requests for each seed"). All cells replay the same frozen
# files, so there is exactly one dataset.
SEEDS="${SEEDS:-1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20}"

BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$BENCH_DIR/.." && pwd)"

NS="${NS:-cache-llm}"
RELEASE="${RELEASE:-stack}"
CHART="${CHART:-vllm/vllm-stack}"
# Pin the chart: the cluster runs 0.1.11 and 0.1.12+ has schema drift - an
# unpinned upgrade would silently migrate the stack mid-experiment.
CHART_VERSION="${CHART_VERSION:-0.1.11}"
# BASE_URL is the laptop-side endpoint, used by the warm-up and the registry probe - neither
# is measured. The default matches the `kubectl port-forward` in benchmarks/README.md,
# "Step 1: install the stack". Point it at an Ingress instead if you have one.
BASE_URL="${BASE_URL:-http://localhost:8000}"
# TARGET_URL is what the MEASURED replay hits, from inside the cluster (#27). Same target
# Prometheus scrapes; `kubectl get svc stack-router-service` shows router-sport 80 -> 8000.
TARGET_URL="${TARGET_URL:-http://stack-router-service.$NS.svc.cluster.local:80}"
MODEL="${MODEL:-Qwen/Qwen2.5-3B-Instruct}"
ROUTER_DEPLOY="${ROUTER_DEPLOY:-stack-deployment-router}"
ENGINE_DEPLOY="${ENGINE_DEPLOY:-stack-llm-deployment-vllm}"
# The single source of truth for the router image repo: it is BOTH --set to helm and
# asserted against the running pod below. Overriding routerSpec.repository in the values
# file alone would deploy one image and assert another. Set this to run from your own registry.
ROUTER_REPO="${ROUTER_REPO:-quay.io/rhl193000/lmstack-router-loadaware}"
PROM_PORT="${PROM_PORT:-19090}"
DCGM_PORT="${DCGM_PORT:-19400}"

# OSL - output sequence length, pinned per request with `ignore_eos` so it is
# exact rather than a cap. A runtime flag, not part of the frozen workload:
# changing it costs no dataset regeneration and the manifest stays valid.
#
# OSL is the main lever on per-request work: it sets decode time, hence in-flight
# concurrency at a fixed rate (L = lambda*W), hence KV pressure. At the swept
# value the mechanism by which imbalance hurts is dormant; raising it is how that
# mechanism is reached without touching the rate. See benchmarks/README.md, "The
# output length: 64 tokens".
MAX_TOKENS="${MAX_TOKENS:-64}"

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
      --set routerSpec.repository="$ROUTER_REPO"
      --set routerSpec.tag="$LOADAWARE_TAG"
    ) ;;
  *)
    echo "unknown cell: $CELL" >&2; exit 2 ;;
esac

OUT="$RESULTS_ROOT/$(date +%Y%m%d-%H%M%S)-$CELL"
mkdir -p "$OUT"
echo "==> cell $CELL (arm=$ARM beta=${BETA:-n/a}) rate=$RATE osl=$MAX_TOKENS → $OUT"

PIDS=()
# The DCGM supervisors (step 7) each trap TERM and kill their own port-forward
# before exiting, so a plain kill here cannot orphan a `kubectl port-forward` and
# leave a local port bound for the next cell.
cleanup() { for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done; }
trap cleanup EXIT

# ---- 0. frozen workloads verified ------------------------------------------
# `zipfian` is the shared-prefix placement experiment; `novel` is the no-reuse profile that
# measures what the cache COSTS when it can never hit (guidelines §3, ticket #25).
# Two profiles, two manifests: adding the second cannot perturb the first's checksums.
WORKLOAD_PROFILE="${WORKLOAD_PROFILE:-zipfian}"
case "$WORKLOAD_PROFILE" in
  zipfian|novel) ;;
  *) echo "unknown WORKLOAD_PROFILE=$WORKLOAD_PROFILE (want: zipfian|novel)" >&2; exit 2 ;;
esac
export WORKLOAD_PROFILE
python3 "$BENCH_DIR/freeze_workloads.py" --profile "$WORKLOAD_PROFILE"

# ---- 1. deploy the arm ------------------------------------------------------
helm upgrade --install "$RELEASE" "$CHART" -n "$NS" --version "$CHART_VERSION" "${HELM_ARGS[@]}"

# validity rule 2: a mounted dev overlay would invalidate every number - strip it
if kubectl get deploy "$ROUTER_DEPLOY" -n "$NS" \
    -o jsonpath='{.spec.template.spec.volumes[*].name}' | grep -q router-patch; then
  echo "==> dev overlay mounted on the router - reverting before measuring"
  NS="$NS" "$REPO_ROOT/deploy/dev/revert-router-patch.sh"
fi

# β travels by env var; chart 0.1.11 has no routerSpec.env passthrough. Set it
# explicitly on loadaware cells and REMOVE it on baselines - the three-way merge
# preserves out-of-band env across upgrades, so a stale β would otherwise leak
# between cells.
# HF_HOME=/tmp/hf must be set on BOTH arms (#21): the router has no writable cache
# dir, so the tokenizer blocks its event loop on every request and throughput
# collapses to ~4 req/s. The comparison is void if only one arm gets it. It cannot
# live in values: chart 0.1.11 has no routerSpec.env passthrough.
if [ "$ARM" = "loadaware" ]; then
  kubectl set env "deploy/$ROUTER_DEPLOY" -n "$NS" \
    HF_HOME=/tmp/hf "LOADAWARE_BETA=$BETA"
else
  kubectl set env "deploy/$ROUTER_DEPLOY" -n "$NS" \
    HF_HOME=/tmp/hf LOADAWARE_BETA-
fi

# ---- 2. router Service controller ports -------------------------------------
# Upstream chart bug (production-stack#1029): the Service omits the LMCache
# controller ports 9001/9002, so engines never register and every lookup fails
# silently. Patch it before each cell.
if ! kubectl get svc "$RELEASE-router-service" -n "$NS" -o jsonpath='{.spec.ports[*].name}' \
    | grep -q lmcache-reply; then
  kubectl patch svc "$RELEASE-router-service" -n "$NS" --type=json -p '[
    {"op":"add","path":"/spec/ports/-","value":{"name":"lmcache-reply","port":9001,"targetPort":9001,"protocol":"TCP"}},
    {"op":"add","path":"/spec/ports/-","value":{"name":"lmcache-heartbeat","port":9002,"targetPort":9002,"protocol":"TCP"}}]'
fi

# ---- 3. router settled, image asserted (the cold start itself is step 4) ----
kubectl rollout status "deploy/$ROUTER_DEPLOY" -n "$NS" --timeout=10m

# validity rule 2: wrong image = discard, never correct. Assert the deployed
# router matches the cell's label before anything is measured.
ROUTER_IMAGE=$(kubectl get deploy "$ROUTER_DEPLOY" -n "$NS" \
  -o jsonpath='{.spec.template.spec.containers[0].image}')
if [ "$ARM" = "loadaware" ]; then
  WANT="$ROUTER_REPO:$LOADAWARE_TAG"
  [ "$ROUTER_IMAGE" = "$WANT" ] || { echo "router image $ROUTER_IMAGE != $WANT" >&2; exit 1; }
else
  case "$ROUTER_IMAGE" in
    lmcache/lmstack-router:*) ;;
    *) echo "baseline cell but router image is $ROUTER_IMAGE" >&2; exit 1 ;;
  esac
fi

# ---- 4. cold, stale-free start (drain -> router -> engines -> registration, #13)
# Drain-then-restart, NOT `rollout restart` - a still-live old engine registers
# instance_ids that outlive it and the stock kvaware router 500s on them.
# cold_start.sh owns that ordering, the registration wait and the stale-id
# assertion (and its header owns the why); on roundrobin there is no LMCache
# controller, so none of it applies.
NS="$NS" ROUTER_DEPLOY="$ROUTER_DEPLOY" ENGINE_DEPLOY="$ENGINE_DEPLOY" \
  EXPECT_REGISTRATIONS="$USES_LOOKUP" "$BENCH_DIR/cold_start.sh"

# ---- 5. registry probe (#13) - only meaningful on lookup-routing arms -------
if [ "$USES_LOOKUP" = 1 ]; then
  NS="$NS" BASE_URL="$BASE_URL" MODEL="$MODEL" \
    "$REPO_ROOT/deploy/dev/registry-probe.sh" "$(date +%s)"
fi

# ---- 6. warm-up over the prefix pool, gated on non-empty layout_info --------
WARMUP_START=$(date +%s)
python3 "$BENCH_DIR/warmup.py" --base-url "$BASE_URL" --model "$MODEL" --insecure
if [ "$USES_LOOKUP" = 1 ]; then
  # window starts AT warm-up start: probe traffic just before it also logs
  # "found by" lines and must not satisfy this gate
  since=$(( $(date +%s) - WARMUP_START )); [ "$since" -lt 1 ] && since=1
  hits=$(kubectl logs "deploy/$ROUTER_DEPLOY" -n "$NS" --since="${since}s" \
    | grep -c "found by .* router" || true)
  if [ "$hits" -eq 0 ]; then
    echo "warm-up gate FAILED: no 'found by … router' lines - layout_info empty, do not measure" >&2
    exit 1
  fi
  echo "==> warm-up gate ok ($hits cache-path routings)"
fi

# ---- 7. collectors ----------------------------------------------------------
# NOTE: no Prometheus port-forward here. prom_dump uses query_range over a past
# window, so it only needs Prometheus reachable at DUMP time; a forward held
# open across the whole cell has the whole cell to die in, and under `set -e` it
# takes the cell and the rest of the sweep with it. Established in step 9, with
# retries. DCGM genuinely needs a live forward because it polls continuously.
# DCGM is a DaemonSet: forward each pod on its own port, or one node's GPU is lost
#
# Each forward runs under a supervisor that restarts it (#35): a bare
# `kubectl port-forward` dies for good the moment the VPN drops, and dcgm_poll keeps
# polling a dead local port, truncating the tail with nobody noticing. The
# supervisor cannot make this WAN-immune (nothing laptop-side can); it only
# recovers once the link returns. The residual risk is covered by the coverage
# gate in step 11.
DCGM_URLS=()
port="$DCGM_PORT"
for pod in $(kubectl get pods -n nvidia-gpu-operator -l app=nvidia-dcgm-exporter \
    -o jsonpath='{.items[*].metadata.name}'); do
  ( trap 'kill "${pf:-}" 2>/dev/null; exit 0' TERM INT
    while :; do
      kubectl port-forward -n nvidia-gpu-operator "pod/$pod" "$port:9400" >/dev/null 2>&1 &
      pf=$!
      # `|| true` is load-bearing: this script runs under `set -e`, so a
      # non-zero wait - which is precisely what a dropped forward produces -
      # would kill the supervisor on the first failure it exists to survive.
      wait "$pf" || true
      sleep 2   # the link is down; do not spin
    done ) &
  PIDS+=($!)
  DCGM_URLS+=(--url "http://localhost:$port/metrics")
  port=$((port + 1))
done
sleep 3
python3 "$BENCH_DIR/collectors/dcgm_poll.py" \
  "${DCGM_URLS[@]}" --out "$OUT/dcgm.csv" &
PIDS+=($!)

# ---- 8. measured replay: the cell's frozen seeds back-to-back ---------------
# The replay runs INSIDE the cluster (#27): a Job runs the SAME load_driver.py against the
# router's ClusterIP - no route, no TLS, no WAN - so the metric is unchanged and only the
# instrument moved. Everything else in this script still runs on the laptop. Why the WAN
# instrument was unusable, and what that means for pre-#27 results:
# benchmarks/README.md, "The measurement runs inside the cluster".
NS="$NS" MODEL="$MODEL" RATE="$RATE" MAX_TOKENS="$MAX_TOKENS" SEEDS="$SEEDS" \
  CELL="$CELL" OUT="$OUT" TARGET_URL="$TARGET_URL" "$BENCH_DIR/bench_job.sh"

# The window comes from the POD's clock, written by collect_job.py. It cannot come from
# `date +%s` here: image pull plus dataset verification sit between the warm-up above and
# the first request, and a laptop-clock window would drag warm-up traffic into the
# Prometheus dump and contaminate the imbalance co-primary.
# shellcheck source=/dev/null
source "$OUT/window.env"   # CELL_START, CELL_END, DRIVER_NODE, BENCH_IMAGE

# ---- 9. Prometheus dump over the measurement window -------------------------
# Forward now, not at cell start: a short-lived forward is a reliable one. Retry
# because a single refused connection here would otherwise discard a cell whose
# measurements are already complete and on disk.
prom_dumped=0
for attempt in 1 2 3; do
  kubectl port-forward -n "$NS" svc/stack-prometheus "$PROM_PORT:9090" >/dev/null 2>&1 &
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
ROUTER_IMAGE_ID=$(kubectl get pods -n "$NS" -l "$(kubectl get deploy "$ROUTER_DEPLOY" -n "$NS" \
  -o jsonpath='{.spec.selector.matchLabels}' \
  | python3 -c 'import json,sys; print(",".join(f"{k}={v}" for k,v in json.load(sys.stdin).items()))')" \
  -o jsonpath='{.items[0].status.containerStatuses[0].imageID}' 2>/dev/null || echo unknown)
GIT_COMMIT=$(git -C "$REPO_ROOT" rev-parse HEAD)
# SWEEP_ID names the batch this cell belongs to; `analyze.py compare` refuses to pair cells
# carrying different ones. Why that guard needs a name of its own: benchmarks/README.md,
# "Overview".
#
# The results-root basename is the fallback, but NEVER the shared default root: `results` as an
# id would stamp every sweep ever run identically and let the guard pass the cross-window pairs
# it exists to stop. A bare cell run is its own batch, so it gets its own stamped id.
if [ -z "${SWEEP_ID:-}" ]; then
  SWEEP_ID="$(basename "$RESULTS_ROOT")"
  [ "$SWEEP_ID" = "results" ] && SWEEP_ID="cell-$(date +%Y%m%d-%H%M%S)"
fi
export CELL ARM BETA RATE MAX_TOKENS CELL_START CELL_END ROUTER_IMAGE ROUTER_IMAGE_ID GIT_COMMIT OUT BENCH_DIR
export DRIVER_NODE BENCH_IMAGE TARGET_URL SWEEP_ID
python3 - <<'PY'
import json, os
env = os.environ
profile = env.get("WORKLOAD_PROFILE", "zipfian")
sub = "workloads" if profile == "zipfian" else os.path.join("workloads", profile)
manifest = json.load(open(os.path.join(env["BENCH_DIR"], sub, "manifest.json")))
run = {
    "cell": env["CELL"],
    "sweep_id": env["SWEEP_ID"],
    "arm": env["ARM"],
    "beta": env["BETA"] or None,
    "rate_req_s": float(env["RATE"]),
    "osl_tokens": int(env["MAX_TOKENS"]),
    "window": {"start_ts": int(env["CELL_START"]), "end_ts": int(env["CELL_END"])},
    "router_image": env["ROUTER_IMAGE"],
    "router_image_id": env["ROUTER_IMAGE_ID"],
    # Where the measurement was taken from (#27). "in-cluster" is what makes the recorded
    # TTFT comparable to the pre-registered metric rather than to the WAN-contaminated runs.
    "driver": {
        "location": "in-cluster",
        "node": env["DRIVER_NODE"],
        "image": env["BENCH_IMAGE"],
        "target": env["TARGET_URL"],
    },
    "git_commit": env["GIT_COMMIT"],
    "workload_profile": env.get("WORKLOAD_PROFILE", "zipfian"),
    "workload_manifest": manifest,
}
with open(os.path.join(env["OUT"], "run.json"), "w") as f:
    json.dump(run, f, indent=2)
print(f"wrote {env['OUT']}/run.json")
PY

# ---- 11. utilization coverage gate ------------------------------------------
# Every utilization series checked against [CELL_START, CELL_END] and the covered
# fraction recorded in run.json (#35). WARN-ONLY, and deliberately so: the driver
# CSVs are the primary measurement and a cell with good latency data must not be
# discarded over utilization sampling. What it buys is that a truncated series is
# recorded rather than silent.
python3 "$BENCH_DIR/utilization.py" coverage "$OUT" --update-run-json || \
  echo "WARNING: utilization coverage step failed for $OUT (non-fatal)" >&2

# ---- 12. validity gate ------------------------------------------------------
python3 "$BENCH_DIR/analyze.py" validate "$OUT"
echo "==> cell $CELL complete: $OUT"

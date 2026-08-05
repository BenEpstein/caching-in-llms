#!/usr/bin/env bash
# Laptop side of the in-cluster measured replay (#27): emit the Job, apply it, wait it out,
# pull the results back out of the pod log.
#
# Called by run_cell.sh step 8 with NS / MODEL / RATE / MAX_TOKENS / SEEDS / CELL / OUT in
# the environment. Everything else about the cell still runs on the laptop.
#
# Requires BENCH_TAG - the git short SHA of the CI-built bench image, exactly like
# LOADAWARE_TAG for the router image.
set -euo pipefail

: "${OUT:?}" "${CELL:?}" "${MODEL:?}" "${RATE:?}" "${MAX_TOKENS:?}" "${SEEDS:?}"
: "${BENCH_TAG:?needs BENCH_TAG=<git short SHA of the CI-built bench image>}"

NS="${NS:-cache-llm}"
BENCH_REPO="${BENCH_REPO:-quay.io/rhl193000/bench-driver}"
BENCH_IMAGE="$BENCH_REPO:$BENCH_TAG"
# The in-cluster service, not the edge route: same target Prometheus already scrapes, and
# `oc get svc stack-router-service` confirms router-sport 80 -> 8000. No TLS, no route,
# no WAN - which is the entire point of this file.
TARGET_URL="${TARGET_URL:-http://stack-router-service.$NS.svc.cluster.local:80}"
BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Job names are DNS-1123 labels; beta cells carry dots (loadaware-b0.5).
CELL_SLUG=$(echo "$CELL" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9-' '-' | sed 's/-*$//')
JOB="bench-$CELL_SLUG-$(date +%s)"

echo "==> $JOB: image $BENCH_IMAGE, target $TARGET_URL, seeds [$SEEDS]"

# ---- the Job ----------------------------------------------------------------
#
# NO podAntiAffinity, deliberately. gapu-2 has exactly two schedulable nodes (all three
# control planes carry NoSchedule) and an engine runs on EACH of them, so an anti-affinity
# against the engines can never be satisfied - required would leave the pod Pending forever
# and preferred is theatre. Worse, worker0 sits at 93% CPU / 99% memory requested, so the
# driver lands on worker1 essentially deterministically, next to the router and the second
# engine. The control for that is not a scheduling rule, it is the engine-side TTFT
# cross-check in the definition of done: if in-cluster engine-side TTFT matches the existing
# WAN cells, the driver pod is not perturbing the engines. The node is recorded either way.
#
# No CPU limit, also deliberately: CFS throttling on the driver would inflate client-side
# TTFT exactly the way the WAN did, which is the artifact this whole change removes. The
# 1-CPU request maps to cpu.shares, so under contention the driver gets a proportional share
# rather than starving the router. (Verified 2026-08-05: no LimitRange in the namespace, so
# nothing injects a default limit behind our back.)
oc apply -n "$NS" -f - <<YAML
apiVersion: batch/v1
kind: Job
metadata:
  name: $JOB
  namespace: $NS
  labels:
    app: bench-driver
    cell: "$CELL_SLUG"
spec:
  # A crashed cell must fail loudly. A silent retry would replay seeds into the same
  # measurement window and quietly corrupt the cell.
  backoffLimit: 0
  # Deliberately NO ttlSecondsAfterFinished: the pod and its logs are the results channel
  # and must survive until collected.
  template:
    metadata:
      labels:
        app: bench-driver
    spec:
      restartPolicy: Never
      containers:
        - name: driver
          image: $BENCH_IMAGE
          imagePullPolicy: IfNotPresent
          command: ["/bin/bash", "/app/in_pod.sh"]
          env:
            - {name: TARGET_URL, value: "$TARGET_URL"}
            - {name: MODEL, value: "$MODEL"}
            - {name: RATE, value: "$RATE"}
            - {name: MAX_TOKENS, value: "$MAX_TOKENS"}
            - {name: SEEDS, value: "$SEEDS"}
            - {name: PYTHONUNBUFFERED, value: "1"}
            - {name: PYTHONDONTWRITEBYTECODE, value: "1"}
            # HOME defaults to / under the restricted SCC's arbitrary uid, which is not
            # writable. Same failure class as #21.
            - {name: HOME, value: "/tmp"}
            - name: NODE_NAME
              valueFrom:
                fieldRef:
                  fieldPath: spec.nodeName
          resources:
            requests:
              cpu: "1"
              memory: 512Mi
              ephemeral-storage: 512Mi
            limits:
              memory: 2Gi
              ephemeral-storage: 1Gi
          volumeMounts:
            - {name: tmp, mountPath: /tmp}
      volumes:
        # 126 MB of regenerated JSONL (measured) plus ~5 MB of CSV. Sized explicitly so the
        # pod is not an eviction candidate on a node already under pressure.
        - name: tmp
          emptyDir:
            sizeLimit: 1Gi
YAML

# ---- progress ---------------------------------------------------------------
# Wait for the pod to EXIST and to have left Pending before following it. Two failure modes
# were hit on 2026-08-05, both silent because this tail is deliberately non-fatal:
#
#   1. `oc logs -f job/<name>` straight after `oc apply` - the Job exists but its pod does
#      not yet, so kubectl's selector matches nothing and it exits non-zero.
#   2. `oc logs -f pod/<name>` while that pod is still ContainerCreating, which happens
#      whenever the node has not cached the tag - "BadRequest: container is waiting to
#      start". Fixing (1) walked straight into (2): the disconnect test only passed because
#      the image happened to be cached from the previous cell.
#
# `--pod-running-timeout` fixes neither - it governs waiting when a SELECTOR resolves pods,
# not a pod named directly, so it was doing nothing here. Poll the phase instead.
# Succeeded/Failed are fine to follow: `oc logs -f` on a finished pod prints its whole log
# and exits, which is what a short cell should show anyway.
POD="" PHASE=""
for _ in $(seq 300); do   # up to 10 min - covers a cold image pull on a busy node
  POD=$(oc get pods -n "$NS" -l "job-name=$JOB" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  if [ -n "$POD" ]; then
    PHASE=$(oc get pod "$POD" -n "$NS" -o jsonpath='{.status.phase}' 2>/dev/null || true)
    [ -n "$PHASE" ] && [ "$PHASE" != "Pending" ] && break
  fi
  sleep 2
done

# Progress ONLY, and non-fatal by design: `oc logs -f` dies with the VPN, and the data is
# collected below by a plain `oc logs` that re-reads the whole log from the start and is
# therefore idempotent. Reconnecting is "run the command again", not "resume a stream".
if [ -n "$POD" ] && [ -n "$PHASE" ] && [ "$PHASE" != "Pending" ]; then
  echo "==> following pod/$POD (phase $PHASE)"
  oc logs -f "pod/$POD" -n "$NS" || true
else
  echo "==> no running pod for $JOB after 10m; skipping the progress tail" >&2
  echo "    (collection is unaffected - it re-reads the whole log at the end)" >&2
fi

# ---- wait -------------------------------------------------------------------
# The tail above may have exited early (VPN, pod restart, anything), so completion is waited
# on properly. Each `oc wait` is capped at 60 s so a dropped connection costs one iteration
# instead of the cell.
while true; do
  if oc wait --for=condition=complete "job/$JOB" -n "$NS" --timeout=60s >/dev/null 2>&1; then
    break
  fi
  if [ "$(oc get "job/$JOB" -n "$NS" \
      -o jsonpath='{.status.conditions[?(@.type=="Failed")].status}' 2>/dev/null)" = "True" ]; then
    echo "==> $JOB FAILED - last 50 log lines:" >&2
    oc logs "job/$JOB" -n "$NS" 2>/dev/null | tail -50 >&2
    exit 1
  fi
  echo "==> waiting for $JOB …"
done

# ---- collect ----------------------------------------------------------------
oc logs "job/$JOB" -n "$NS" >"$OUT/job.log"
python3 "$BENCH_DIR/collect_job.py" \
  --log "$OUT/job.log" --out "$OUT" --seeds "$SEEDS" --bench-image "$BENCH_IMAGE"

echo "==> $JOB collected → $OUT   (job kept for inspection: oc delete job/$JOB -n $NS)"

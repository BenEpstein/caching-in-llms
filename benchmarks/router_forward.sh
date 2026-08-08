#!/usr/bin/env bash
# Laptop -> router reachability for the two UNMEASURED steps: the registry probe and the
# warm-up. Sourced, not executed - it exports BASE_URL into the caller's shell.
#
# Why this file exists rather than a port-forward the operator starts by hand:
# `kubectl port-forward svc/...` binds to ONE pod behind the Service. Every cell restarts the
# router in its cold start (cold_start.sh, #13), that pod dies, and the forward dies with it -
# "lost connection to pod ... ID does not exist". A forward opened before the cell is therefore
# guaranteed to be dead by the time the probe runs. It must be opened AFTER the cold start,
# which is the last router restart in a cell, and that is what start_router_forward does.
#
# An operator with an Ingress or an OpenShift route does not need any of this: those front the
# Service, not a pod, so they survive the restart. Setting BASE_URL to one skips the forward.

# start_router_forward <namespace> <release> [local-port]
#
# No-op when BASE_URL is already set to something that is not the local default, so an
# operator-supplied Ingress always wins. Otherwise opens a forward, waits for the router to
# answer on it, exports BASE_URL, and appends the pid to PIDS for the caller's trap to reap.
start_router_forward() {
  local ns="$1" release="$2" port="${3:-8000}"

  case "${BASE_URL:-}" in
    ""|http://localhost:*|http://127.0.0.1:*) ;;
    *) echo "==> router endpoint: $BASE_URL (operator-supplied, no port-forward)"; return 0 ;;
  esac

  kubectl port-forward -n "$ns" "svc/$release-router-service" "$port:80" >/dev/null 2>&1 &
  local pf=$!
  PIDS+=("$pf")

  # Readiness, not a fixed sleep: the forward is up when the router answers through it. 30 s is
  # far past the observed sub-second bind and still fails the cell rather than hanging it.
  local i
  for i in $(seq 30); do
    if curl -fsS --max-time 2 -o /dev/null "http://localhost:$port/v1/models" 2>/dev/null; then
      BASE_URL="http://localhost:$port"
      export BASE_URL
      echo "==> router endpoint: $BASE_URL (port-forward pid $pf)"
      return 0
    fi
    kill -0 "$pf" 2>/dev/null || { echo "port-forward to $release-router-service died on startup" >&2; return 1; }
    sleep 1
  done

  echo "port-forward to $release-router-service never served /v1/models" >&2
  return 1
}

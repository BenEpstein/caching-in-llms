#!/usr/bin/env bash
# Bring the stack to a cold, STALE-FREE state before measuring.
#
# Two invariants have to hold at once, and the obvious orderings satisfy only one:
#
#   (a) #13: the router must be up BEFORE the engines start admitting, or their
#       admits are lost and the registry is empty for the life of the engine
#       process - both arms then silently degrade to QPS routing.
#   (b) The router must NEVER see a registration from an engine that will die
#       before measurement. `kv_pool` keys chunks by instance_id, and the stock
#       kvaware router raises KeyError -> HTTP 500 when a lookup returns an
#       instance whose id has no live URL.
#
# `helm upgrade` then `rollout restart engines` satisfies (a) but violates (b):
# the OLD engine pods are still running while the new router comes up, they
# register, and their ids outlive them.
#
# This is NOT patched in the router: the baseline must stay unmodified or the
# comparison is void. It is fixed by choreography - drain the engines to zero
# FIRST, restart the router into an empty cluster, then bring engines back.
set -euo pipefail

NS="${NS:-cache-llm}"
ROUTER_DEPLOY="${ROUTER_DEPLOY:-stack-deployment-router}"
ENGINE_DEPLOY="${ENGINE_DEPLOY:-stack-llm-deployment-vllm}"
REPLICAS="${REPLICAS:-2}"

echo "==> draining engines to 0 (no live engine may register with the new router)"
oc scale "deploy/$ENGINE_DEPLOY" -n "$NS" --replicas=0
for _ in $(seq 120); do
  n=$(oc get pods -n "$NS" -l model=llm --no-headers 2>/dev/null | wc -l | tr -d ' ')
  [ "$n" = 0 ] && break
  sleep 5
done
[ "$(oc get pods -n "$NS" -l model=llm --no-headers 2>/dev/null | wc -l | tr -d ' ')" = 0 ] \
  || { echo "engines did not drain" >&2; exit 1; }

echo "==> restarting the router into an empty cluster (kv_pool starts clean)"
oc rollout restart "deploy/$ROUTER_DEPLOY" -n "$NS"
oc rollout status "deploy/$ROUTER_DEPLOY" -n "$NS" --timeout=10m
ROUTER_UP_TS=$(date +%s)

echo "==> bringing engines back to $REPLICAS"
oc scale "deploy/$ENGINE_DEPLOY" -n "$NS" --replicas="$REPLICAS"
oc rollout status "deploy/$ENGINE_DEPLOY" -n "$NS" --timeout=30m

# A `--routing-logic roundrobin` router never instantiates the LMCache controller,
# so no worker ever registers and there is no stale id to guard against either.
# Same USES_LOOKUP reason the registry probe and warm-up gate are skipped on that arm.
if [ "${EXPECT_REGISTRATIONS:-1}" != 1 ]; then
  echo "==> cold start complete (arm does not use the registry; no registrations expected)"
  exit 0
fi

echo "==> waiting for exactly $REPLICAS worker registrations"
for _ in $(seq 60); do
  since=$(( $(date +%s) - ROUTER_UP_TS )); [ "$since" -lt 1 ] && since=1
  registered=$(oc logs "deploy/$ROUTER_DEPLOY" -n "$NS" --since="${since}s" 2>/dev/null \
    | grep -c "Registered instance-worker" || true)
  [ "$registered" -ge "$REPLICAS" ] && break
  sleep 5
done
[ "${registered:-0}" -ge "$REPLICAS" ] \
  || { echo "workers never registered (saw ${registered:-0})" >&2; exit 1; }

# (b) as an assertion: a stale id here means the drain raced, and the run would
# carry KeyError 500s.
live=$(oc get pods -n "$NS" -l model=llm -o jsonpath='{.items[*].metadata.name}')
stale=0
for id in $(oc logs "deploy/$ROUTER_DEPLOY" -n "$NS" 2>/dev/null \
    | grep -o "Registered instance-worker ('[^']*'" | grep -o "'[^']*'" | tr -d "'" | sort -u); do
  case " $live " in *" $id "*) ;; *) echo "    STALE registration: $id" >&2; stale=1 ;; esac
done
[ "$stale" = 0 ] || { echo "router holds registrations for dead engines - do not measure" >&2; exit 1; }
echo "==> cold start clean: $registered registrations, all live"

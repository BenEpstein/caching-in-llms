# The cache-OFF arm (novel-prompt / cache-overhead experiment)

> status: live · 2026-08-04 · **not yet validated on gapu-2** — the first run must confirm the
> engines come up with LMCache disabled and that the router falls back cleanly.

## Why this arm exists

The guidelines (§3) ask for a second workload profile — *novel long prompts, unlikely to be
cached* — **to measure cache overhead**. Benefit and cost cannot be separated on the Zipfian
workload, because there the cache is mostly hitting. Pair `WORKLOAD_PROFILE=novel`, where
nothing is ever reused, with LMCache disabled, and the cache-on/cache-off difference is what
lookup, admission and storage charge on a pure-miss path.

## Deliberately NOT a values file

`servingEngineSpec.modelSpec` is a **list**. Helm merges maps but *replaces* lists, so a second
`-f` file containing a one-element `modelSpec` silently discards the model name, the GPU memory
settings, the controller ports — everything in the base values. It would deploy, and it would
deploy the wrong thing. Use `--set` against the list index instead, which merges into the
existing element:

```bash
helm upgrade --install "$RELEASE" "$CHART" -n "$NS" --version 0.1.11 \
  -f deploy/values-baseline-kvaware.yaml \
  --set servingEngineSpec.modelSpec[0].lmcacheConfig.enabled=false \
  --set routerSpec.routingLogic=roundrobin
```

## Both arms must use `roundrobin`

With LMCache disabled there is no registry for `kvaware` or `loadaware` to consult, so a
cache-aware arm would not be comparing like with like. On a no-reuse workload every placement is
equivalent anyway — which is exactly what makes this a clean cost measurement rather than a
confounded one.

## Running it

```bash
# cache ON  (baseline for this comparison)
WORKLOAD_PROFILE=novel benchmarks/run_cell.sh novel-cache-on  roundrobin <rate>

# cache OFF (add the two --set flags above to the helm invocation)
WORKLOAD_PROFILE=novel benchmarks/run_cell.sh novel-cache-off roundrobin <rate>

python3 benchmarks/analyze.py compare results/<...novel-cache-on> results/<...novel-cache-off>
```

`analyze.py compare` will refuse the pair unless both runs recorded the same rate and the same
workload manifest, so a profile mix-up fails loudly rather than producing a plausible number.

## Pre-registered expectation

A small positive overhead (single-digit % on TTFT). A **large** overhead would be a finding
worth reporting on its own. A **negative** one means the arms are not actually comparable and
the run is void — stated here before the run so it cannot be rationalized afterwards.

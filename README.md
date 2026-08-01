# Load-Aware Prefix Routing for vLLM Production Stack

BGU final project: extend the [vLLM Production Stack](https://github.com/vllm-project/production-stack)
router with a `loadaware` routing strategy that scores KV-cache-hit benefit against live instance
load, using per-instance prefix-match info from [LMCache](https://github.com/LMCache/LMCache)'s
controller. Goal: a measurable, statistically significant latency/hit-rate improvement over the
unmodified baseline, evaluated on a 2×A10 OpenShift cluster.

## Repo layout

| Path | What it is |
|---|---|
| `docs/project-brief.md` | Project design and source of truth for the extension |
| `docs/feasibility-verification.md` | Code-level feasibility evidence (file/line refs) |
| `docs/handoff-core-implementation.md` | Implementation brief for the two core changes |
| `benchmarks/` | Workload generator + async load driver + unit tests |
| `deploy/` | OpenShift/Helm configs for the 2×A10 cluster, dev loop in `deploy/dev/` |
| `CONTEXT.md` | Ubiquitous language - project terms and their exact meanings |
| `CHANGELOG.md` | Shared session memory; newest entry on top |

## Setup

Python ≥ 3.10, no GPU needed for the test suite:

```bash
pip install -r requirements.txt
```

## Tests

```bash
pytest benchmarks/ -q
```

CI (`.github/workflows/ci.yml`) runs the same suite on every push.

## Benchmarks

The harness is under construction (workload generator and load driver are in place; the
methodology and evaluation runs are tracked on the
[wayfinder map](https://github.com/BenEpstein/caching-in-llms/issues/1)). Cluster deployment
instructions live in `deploy/README.md`.

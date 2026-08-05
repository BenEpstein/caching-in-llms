# `patches/` - our modified copies of router-image Python files

> status: live · 2026-08-05 · describes the shipped overlay; the policy description and the
> `kv_aware_threshold` note were verified against `routing_logic.py` on 2026-08-05 (issue #29)

The contribution lives entirely in the **router pod**: both `vllm_router` and the LMCache
`cache_controller` are installed there as plain Python. This directory holds our modified
copies of those files, **mirroring their path inside the image** under
`/opt/venv/lib/python3.12/site-packages/`:

```
patches/lmcache/v1/cache_controller/controllers/kv_controller.py
   →   /opt/venv/lib/python3.12/site-packages/lmcache/v1/cache_controller/controllers/kv_controller.py
```

The mirror is deliberate: the same tree is what the §6 deliverable image `COPY`s, so the
dev loop and the reproducible image apply *identical* bytes.

## Rules

- **Each file starts as a verbatim copy** pulled out of the pinned router image
  (`lmstack-router:0.1.9.dev9-g37bafbcf5.d20260107`, lmcache 0.3.9post2), so `git diff`
  against the stock file is the real diff. Pull a fresh copy with:
  ```bash
  oc exec -n cache-llm deploy/stack-deployment-router -- \
    cat /opt/venv/lib/python3.12/site-packages/<path> > patches/<path>
  ```
- **Every change is marked** with a `LOADAWARE PATCH` comment naming what it changes and why.
- **Never edit these in `deploy/dev/work/`** — that directory is gitignored scratch for stock
  copies. Point `apply-router-patch.sh` at the tracked file here:
  ```bash
  deploy/dev/apply-router-patch.sh patches/lmcache/v1/cache_controller/controllers/kv_controller.py
  ```
- **Apply every patched file in one invocation.** `apply-router-patch.sh` rebuilds the
  ConfigMap from exactly the files it is given, so passing one file drops the others:
  ```bash
  deploy/dev/apply-router-patch.sh \
    patches/lmcache/v1/cache_controller/controllers/kv_controller.py \
    patches/vllm_router/routers/routing_logic.py \
    patches/vllm_router/parsers/parser.py
  ```
  `loadaware` needs *both* router files (the parser to accept the flag, `routing_logic.py`
  to implement it) and, to have anything to weigh, the `kv_controller.py` patch too.
- **Tests import these files directly** (`tests/conftest.py` stubs the `lmcache` import
  surface and loads the file by path), so the bytes under test are the bytes that get mounted.
  Run them with `pytest tests/` — no cluster, no GPU, no lmcache install.

## Current contents

| File | Change | Ticket |
|---|---|---|
| `lmcache/v1/cache_controller/controllers/kv_controller.py` | Multi-instance lookup: `lookup()` reports per-instance matched-token counts for every holder, not just `kv_pool[key][0]` | [#4](https://github.com/BenEpstein/caching-in-llms/issues/4) |
| `vllm_router/routers/routing_logic.py` | `loadaware` placement policy: `LOADAWARE` enum + factory branch + a `LoadAwareRouter` that routes by `cache_hit_benefit − β·relative_load` over every endpoint. Additions only — `KvawareRouter` is untouched | [#5](https://github.com/BenEpstein/caching-in-llms/issues/5) |
| `vllm_router/parsers/parser.py` | One-line widening of `--routing-logic`'s hard-coded `choices` list to accept `loadaware`. Without it argparse rejects the flag and the router exits before the factory runs | [#5](https://github.com/BenEpstein/caching-in-llms/issues/5) |

## Tunable parameters (`loadaware`)

The score is

```
score(i) = matched_tokens(i)/prompt_tokens  −  β · relative_load(i)
relative_load(i) = (load(i) − mean_load) / max(1, mean_load)
load(i) = in_prefill(i) + in_decoding(i)
```

argmax over all endpoints, ties broken by lexicographic URL. **Both terms are
dimensionless** — a fraction of this prompt, and a fraction of this fleet's mean load — so β
carries no unit from the deployment and the same value is the same policy at any request
rate, prompt length, GPU or engine count.

| Parameter | Env var | Default | Meaning |
|---|---|---|---|
| β | `LOADAWARE_BETA` | `1.0` | Weight on the load penalty. Reads as "an endpoint sitting **100% above fleet-average load** forfeits one full cache hit". β=0 is cache-only placement (the ablation arm); larger β diverts sooner |

**There is no α.** An argmax is invariant under positive scaling, so `α·benefit − β·load` and
`benefit − (β/α)·load` are the same policy: only the ratio was ever a free parameter. It was
1.0 in every run this project recorded, and removing it changes no placement decision.

Set them without a restart-and-reinstall:

```bash
oc set env deploy/stack-deployment-router -n cache-llm LOADAWARE_BETA=0.5
```

Environment rather than a CLI flag on purpose: `--kv-aware-threshold` and friends are
registered in `vllm_router/parsers/parser.py` and consumed in `app.py`, so a flag would make
this a **three**-file patch to mount and keep in sync. `initialize_routing_logic` still
forwards the `loadaware_beta` kwarg when present, so adding the flag later
needs no change to `routing_logic.py`.

`kv_aware_threshold` is accepted for interface compatibility but **not applied** by
`loadaware`: the argmax already lets a small match lose to load, and keeping the band would
route every sub-threshold prompt by QPS in *both* arms. `kvaware` keeps it.

## ⚠️ Baseline measurements must be taken with the patch reverted

`kvaware` is **not guaranteed** behaviourally invariant under the multi-instance lookup, even
though `routing_logic.py` is untouched. The *instance* it picks - `list(layout_info.keys())[0]` - is unchanged, but that instance's `matched_tokens` can grow, since an instance is now credited
on every chunk it holds rather than only on chunks where it happens to be `[0]`. kvaware bands
`matched_tokens` against `kv_aware_threshold` to choose the cache path over the QPS fallback, so
in principle a larger count can flip that branch. Run `deploy/dev/revert-router-patch.sh` and
confirm the router is stock before measuring the baseline arm.

**On this workload the band is vacuous, and the reasoning is worth stating precisely because it
is easy to get backwards.** The test is

```python
matched_tokens < max(len(token_ids) - self.threshold, 0)   # routing_logic.py:396
```

With ISL **1578** and `kv_aware_threshold` defaulting to **2000**, `max(1578 - 2000, 0) == 0`, so
it reduces to `matched_tokens < 0` and **can never fire**. kvaware takes the cache path for every
request that has any holder at all, whatever `matched_tokens` reads.

Two consequences:

- The branch cannot flip on this workload, so kvaware *is* invariant here in practice. Reverting
  for the baseline arm stays the rule anyway: it costs one script and removes the assumption.
- **The claim that prompts must exceed 2000 tokens or kvaware never takes the cache path is
  false, and it is not the reason the workload uses long shared prefixes.** The real reason is
  that a long shared prefix is what creates a hot instance for the policy to route around. That
  inverted claim appeared in three handoff docs, all removed 2026-08-05 (issue #29).

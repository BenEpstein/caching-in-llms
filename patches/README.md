# `patches/` — our modified copies of router-image Python files

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
- **Tests import these files directly** (`tests/conftest.py` stubs the `lmcache` import
  surface and loads the file by path), so the bytes under test are the bytes that get mounted.
  Run them with `pytest tests/` — no cluster, no GPU, no lmcache install.

## Current contents

| File | Change | Ticket |
|---|---|---|
| `lmcache/v1/cache_controller/controllers/kv_controller.py` | Multi-instance lookup: `lookup()` reports per-instance matched-token counts for every holder, not just `kv_pool[key][0]` | [#4](https://github.com/BenEpstein/caching-in-llms/issues/4) |
| `vllm_router/routers/routing_logic.py` | `loadaware` placement policy: `LOADAWARE` enum + factory branch + a `LoadAwareRouter` that routes by `α·cache_hit_benefit − β·load_penalty` over every endpoint. Additions only — `KvawareRouter` is untouched | [#5](https://github.com/BenEpstein/caching-in-llms/issues/5) |

## Tunable parameters (`loadaware`)

The score is `α · (matched_tokens / prompt_tokens) − β · (in_prefill + in_decoding)`, argmax
over all endpoints, ties broken by lexicographic URL.

| Parameter | Env var | Default | Meaning |
|---|---|---|---|
| α | `LOADAWARE_ALPHA` | `1.0` | Weight on cache-hit benefit, the **fraction** of the prompt already cached on that instance (0–1) |
| β | `LOADAWARE_BETA` | `0.1` | Weight on load penalty, the instance's in-flight requests. `1/β` reads as "how many in-flight requests cancel a full cache hit" — at the default, 10 |

Set them without a restart-and-reinstall:

```bash
oc set env deploy/stack-deployment-router -n cache-llm LOADAWARE_ALPHA=1.0 LOADAWARE_BETA=0.25
```

Environment rather than a CLI flag on purpose: `--kv-aware-threshold` and friends are
registered in `vllm_router/parsers/parser.py` and consumed in `app.py`, so a flag would make
this a **three**-file patch to mount and keep in sync. `initialize_routing_logic` still
forwards `loadaware_alpha` / `loadaware_beta` kwargs when present, so adding the flag later
needs no change to `routing_logic.py`.

`kv_aware_threshold` is accepted for interface compatibility but **not applied** by
`loadaware`: the argmax already lets a small match lose to load, and keeping the band would
route every sub-threshold prompt by QPS in *both* arms. `kvaware` keeps it.

## ⚠️ Baseline measurements must be taken with the patch reverted

`kvaware` **is not behaviourally invariant** under the multi-instance lookup, even though
`routing_logic.py` is untouched. The *instance* it picks — `list(layout_info.keys())[0]` — is
unchanged, but that instance's `matched_tokens` can grow, since an instance is now credited on
every chunk it holds rather than only on chunks where it happens to be `[0]`. kvaware bands
`matched_tokens` against `kv_aware_threshold` (`routing_logic.py:354-369`) to choose the cache
path over the QPS fallback, so a larger count can flip that branch. Run
`deploy/dev/revert-router-patch.sh` and confirm the router is stock before measuring the
baseline arm.

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

## ⚠️ Baseline measurements must be taken with the patch reverted

`kvaware` picks `list(layout_info.keys())[0]`, i.e. it depends on insertion order. The
multi-instance lookup changes that order whenever a chunk has more than one holder, so
`kvaware` **is not behaviourally invariant** under this patch even though
`routing_logic.py` is untouched. Run `deploy/dev/revert-router-patch.sh` and confirm the
router is stock before measuring the baseline arm.

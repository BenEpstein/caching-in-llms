# Handoff: cache sizing for the evaluation workload

> status: live · 2026-08-03 · summarises the #7 sweep and the one open question it raised;
> numbers below are from `results/20260803-*` and verified on gapu-2, not estimates.

## The question for this session

**How do we size the LMCache backend and prefix pool so the evaluation can actually pay
loadaware for what it does?** Ben's instinct was to drop CPU offload and run HBM-only. That
specific move is blocked (see "Constraint"), but the underlying knob is right.

## What we ran

Full 6-cell sweep on gapu-2, [issue #7](https://github.com/BenEpstein/caching-in-llms/issues/7):

```
LOADAWARE_TAG=c68ccfc benchmarks/run_sweep.sh 7.5
```

6 cells (`loadaware-b0 / b0.1 / b0.5 / b1.0`, `kvaware`, `roundrobin`) × 6 seeds × 500
requests at 7.5 req/s. All validated: errors ≤ 0.6%, every registry probe 4/4, every warm-up
gate ≥ 20 cache-path routings. Figures in `docs/figures/`, full resolution comment on #7.

**Headline (b0.1 vs kvaware, pre-registered):** 5/6 seeds improve, median −17.0% TTFT p95,
exact one-sided Wilcoxon **p=0.219, not significant**. Seed 2 reverses and passes every
validity rule, so it stays. At n=6 one reversal caps p at 0.219 regardless of effect size.

## What led to the suggestion

Two findings, both from data already in hand.

**1. The working set fits inside every engine, so there is no placement decision to get
right.** From the engine's own `config.json` (36 layers, 2 KV heads, head_dim 128, bf16):

| | |
|---|---|
| KV per token | 36 KiB |
| one 2048-token prefix | 75.5 MB |
| whole 20-prefix pool | **1.51 GB** |
| per-engine LMCache budget | **4 GB** (`cpuOffloadingBufferSize: "4"`) |

After warm-up both engines hold every prefix. Whichever instance the router picks already has
the KV. Corroborated by `fig3`: engine-local hit rate saturates at ~0.95 on **every** arm
including roundrobin, which is only possible if the cache is everywhere. (Note: that metric is
scraped from the engines, `job=vllm-engines`, so it measures each engine's own cache, not
whether the router chose the holder. It cannot be the §5 headline.)

**2. The policy mechanically works, it just doesn't get paid.** Per-engine mean in-flight
requests over the window (`vllm:num_requests_running`, `fig6`):

| Arm | busiest | idlest | imbalance |
|---|---|---|---|
| `kvaware` | 12.52 | 3.36 | **3.7x** |
| `loadaware-b0` (β=0 ablation) | 12.5 | 3.5 | 3.6x |
| `loadaware-b0.1` (shipped) | 8.22 | 8.03 | **1.0x** |
| `roundrobin` | 10.21 | 6.92 | 1.5x |

kvaware concentrates 3.7:1; shipped loadaware balances to 1.0x and beats roundrobin at it
(it balances work in flight, not request count). β=0 reproducing kvaware's 3.6x is the clean
ablation that the β term is what does it.

So the effect is large and mechanical, but at 7.5 req/s with the cache everywhere, sitting
12-deep instead of 8-deep costs almost nothing in TTFT. A big mechanical win shows up as a
17% latency wobble that n=6 cannot certify.

## Constraint: CPU offload cannot simply be removed

The LMCache CPU buffer is the only thing making the router's registry non-empty:

`KVAdmitMsg(instance_id, worker_id, key, location)` is emitted when an engine stores a chunk in
a **backend** → `KVController.admit()` files it into `kv_pool`
([kv_controller.py:66](../patches/lmcache/v1/cache_controller/controllers/kv_controller.py#L66))
→ our patched `lookup()` reads `kv_pool` for per-instance matched tokens → `LoadAwareRouter`
scores on that.

vLLM's native HBM prefix cache (`enablePrefixCaching: true`) is internal to the engine and
emits **no** admit messages. Engine env confirms CPU is the only backend: `LMCACHE_LOCAL_CPU=True`,
`LMCACHE_MAX_LOCAL_CPU_SIZE=4`, no `LOCAL_DISK`, no remote.

HBM-only ⇒ `kv_pool` empty ⇒ `lookup()` returns 0 matches for every instance ⇒ the α term is
identically 0 ⇒ both arms collapse to load-only routing. That is issue #13, permanently.

## Proposal to react to

**Shrink the buffer rather than delete it.** At ~0.5 GB per engine a 1.51 GB pool is 3x
capacity, forcing engines to hold disjoint prefix sets so a routing mistake costs a real
recompute and the cache term stops being free. One line in
`deploy/values-baseline-kvaware.yaml`. Raising the prefix pool past 20 is the same lever from
the other side but needs the frozen workload regenerated and re-frozen.

Two companions, for context only, not this session's question: raise the rate to the knee
(~10 req/s) so queue depth converts into TTFT, and promote load imbalance to a reported §5
metric alongside TTFT.

## Open sub-questions

- What buffer size? 0.5 GB is a first guess, not a derived number. Worth checking what LMCache
  0.3.9post2 does at a chunk granularity when the budget is small (thrash vs clean eviction).
- Does this LMCache version expose a **GPU-resident tracked** backend? If it does, Ben's
  HBM-only instinct becomes viable as stated and is strictly cleaner. Unverified.
- Does shrinking the buffer change the arms asymmetrically? It must not: any sizing change
  applies to both arms or the comparison is invalid.

## Cost / state

Re-running the sweep is ~3 h. Nothing has been changed or launched. #7 stays open; a
methodology amendment must be recorded on
[#3](https://github.com/BenEpstein/caching-in-llms/issues/3) before new data is looked at.
Current 6 cells become a pilot, not the headline. ~6 days to the 2026-08-10 deadline.

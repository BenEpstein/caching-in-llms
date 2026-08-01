---
branch: claude/wayfinder-3-e533a3
date: 2026-08-01 (workload exploration session)
status: decisions locked - feed back into the main #3 grilling session
---

# Handoff: workload exploration → decisions for the #3 grilling

This session answered the paused workload question from
`claude-wayfinder-3-e533a3.md` and, in the process, locked the sweep design.
Bring these decisions back to the main grilling; they replace grilling
questions 1-3 of the "What's Left" list there.

## Decisions locked (Ben, this session)

### D1 - Workload: synthetic Zipfian prefix pool, ONE frozen dataset

`benchmarks/workload_gen.py` as-is. Config frozen: s=1.2, 20 prefixes,
2048+32 ISL, OSL 64, seed 42. Generated once, committed as JSONL, replayed
identically for every arm. No second dataset type, no Mooncake replay, no
"future work" framing.

**The story (for §5/§6), evidence-backed:**
- The *structure* (pool of shared prefixes + unique suffix) is the industry
  standard - all three of these do exactly it:
  - vLLM `benchmarks/benchmark_prefix_caching.py` (synthetic repetition, even
    in its ShareGPT mode: prompts replicated `--repeat-count` times)
  - LMCache's own LMBenchmark `synthetic-multi-round-qa`
    (`--shared-system-prompt` / `--user-history-prompt`) - the baseline
    library's own convention
  - NVIDIA AIPerf "Prefix Prompt" (`--num-prefix-prompts`,
    `--prefix-prompt-length`)
- The *Zipf popularity layer* is our deliberate addition. AIPerf samples its
  prefix pool **uniformly** - and uniform is the wrong null model: every
  instance ends up warm for every prefix, nothing is hot, the router pathology
  is hidden. Production traces (Mooncake, FAST'25: JSONL with `hash_ids`,
  512-token blocks) show skewed prefix popularity. We cite the trace as
  external validity, we don't claim precedent for Zipf.
- Parameter rationale: 2048 prefix > `kv_aware_threshold`=2000 (kvaware must
  take the cache path) and = exactly 8 LMCache chunks of 256; 20 prefixes give
  head+tail but the whole pool fits in cache, so hit-rate deltas are
  attributable to *routing*, not eviction; OSL=64 because decode doesn't
  affect routing.
- Zipf head shares at the frozen s=1.2, measured from the generator:
  top-1 prefix = 35% of requests, top-3 = 57%.

### D2 - Design: ONE merged sweep, 6 cells, no separate tuning phase

| Cell | Arm | Notes |
|---|---|---|
| 1-4 | loadaware, β ∈ {0, 0.1, 0.5, 1.0} | α pinned 1.0; β=0 doubles as the cache-only ablation |
| 5 | kvaware | **patch reverted** (Change 1 flips its threshold branch - PR #15 correction) |
| 6 | roundrobin | context baseline |

- **3 seeds × 500 requests per cell** (1500 samples/arm). Seeds replay within
  one deployment window - cheap. Cells are expensive.
- **Every cell = full choreography**: config change → engine restart (~7 min,
  the #13 tax) → warm-up (~2-3 passes over the 20 prefixes, gated on
  non-empty `layout_info` / registry probe) → measured window.
  Cold-start-per-cell is a *feature*: identical starting state per cell, no
  cross-cell cache contamination. Rejected making β hot-reloadable (new code
  days before deadline + reintroduces the contamination confound).
- **~2-2.5h total**, one scripted unattended batch. ~50 min of that is
  irreducible restart tax.
- **Step 0 - rate pilot** (now explicit, since there's no tuning phase to fold
  it into): ~30-60 min, ramp one arm until TTFT p95 elbows, freeze ~75% of the
  knee as the fixed request rate for all 6 cells.

### D3 - Headline pinned a priori to shipped default β=0.1

The claim is "the extension *as shipped* (β=0.1) beats kvaware", NOT
"the best β beats kvaware". Picking β* from the same runs used for the
significance claim is post-hoc selection - indefensible under grilling. The
sweep is robustness evidence (the win isn't a knife-edge), and if another β is
dramatically better that's reported as a sensitivity finding, not the
headline.

**Centerpiece figure for §5**: TTFT p95 vs β with CI bars, kvaware and
roundrobin as horizontal bands; companion panel hit-rate + load-CV vs β
showing the mechanism trade (β↑ → load balance improves, hit rate decays).

### D4 - Confirmed: no adaptive/auto-tuned β exists

Checked against map #1 and issue #5: adaptive β was cut in the deadline scope
decision. Implementation is static `LOADAWARE_ALPHA`/`LOADAWARE_BETA` env vars
(defaults 1.0/0.1). The β sweep IS the tuning story.

## Open items - NOT decided here (other grilling session may have answered)

Standing recommendations only, carry them in:

1. **Overlay vs built image for measured runs** (#16 → #3): recommend built
   image (reproducibility 30%; reader-rebuildable artifact). Eliad
   independently recommends the same in his #3 comment.
2. **Stats test**: paired-by-seed comparison of TTFT p95, bootstrap CI on the
   difference, Mann-Whitney U on per-request TTFT as backup; report the CI.
3. **Metrics plumbing**: latency/TTFT client-side from the load driver
   (per-request JSONL); hit rate from LMCache logs cross-checked vs
   Prometheus; GPU util from Prometheus/DCGM. Harness detail (#6).

## Sources for the report bibliography

- vLLM prefix-caching benchmark: github.com/vllm-project/vllm/blob/main/benchmarks/benchmark_prefix_caching.py
- LMBenchmark (LMCache): github.com/LMCache/LMBenchmark
- production-stack benchmark tutorial: github.com/vllm-project/production-stack/blob/main/tutorials/08-benchmark-multi-round-qa-multi-gpu.md
- AIPerf CLI reference (Prefix Prompt, uniform sampling): docs.nvidia.com/aiperf/reference/command-line-options
- AIPerf Mooncake trace replay: docs.nvidia.com/aiperf/benchmark-modes/trace-replay-with-mooncake-traces
- Mooncake (trace + paper): github.com/kvcache-ai/Mooncake ; dl.acm.org/doi/pdf/10.1145/3773772

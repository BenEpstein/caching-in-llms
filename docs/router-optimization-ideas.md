# Router Optimization Ideas — Fresh Survey of production-stack `main`

> Surveyed 2026-08-01 against `vllm-project/production-stack` @ `3314ee6` (current `main`),
> live issue tracker, and roadmap #855. Purpose: find candidates that could **add to or
> replace** the decided second optimization (adaptive β, `docs/decisions/second-optimization.md`).
> Ranked against the locked decision criteria from `docs/handoff-second-optimization.md`
> (rubric fit → one story → deployment risk → ablation ladder → upstream mergeability).

## What changed upstream since the 2026-07-05 decision (answer: almost nothing)

- Only **18 commits** landed on `main` since our feasibility pin `1e973a3` (2026-06-26);
  none touch routing logic except two bugfixes (#990 prefix-trie seeding, #998 header strip).
- **#876 (router-side queuing RFC) and #905 (queuing groundwork PR) are both still open**,
  unmerged, with the "LMCache locality vs FIFO fairness" policy question still explicitly
  deferred to a later phase. The gap our loadaware policy answers remains acknowledged and
  unclaimed. → The 2026-07-05 decision's evidence base is still current; nothing upstream
  forces a re-decision.
- Roadmap #855 items still open and relevant to us: **P1 router-side queuing**,
  **P2 predictive routing for future workloads**, **P2 priority-based routing**,
  **P2 router performance improvements**.

## New candidates found this survey

### F — Zero-overhead kvaware fast path (router lookup-cost optimization) ★ strongest new find

**Code fact:** `KvawareRouter.route_request()` is `async` but does **blocking work in the
event loop on every request**: full-prompt `AutoTokenizer.encode()` on the router CPU
(`routing_logic.py:364`, 1–4k tokens per request under our workload) and, on tokenizer
failure, a **synchronous** `requests.post()` to the engine's `/tokenize`
(`routing_logic.py:373`) — this stalls the entire router event loop, serializing all
in-flight routing decisions. Our loadaware router inherits this path.

**The optimization (any subset):**
1. Prefix-cached tokenization — LRU keyed on a hash of the prompt's head; under Zipf, hot
   prefixes tokenize once instead of per-request. (The router already ships a `HashTrie`
   for prefixaware; same spirit.)
2. Offload cold tokenization to a thread pool (`asyncio.to_thread`) so the loop never blocks.
3. Replace the sync `requests.post` fallback with the router's existing aiohttp client.

**Why it's strong here:** the rubric's §3 benchmark spec *already requires* a
"novel long prompts → measure overhead" workload profile — this candidate is the natural
thing that profile measures. Router-image-only, zero deployment risk, deterministic unit
tests (tokenize-count assertions, no GPU). Fits the ladder as an orthogonal rung that
benefits *every* strategy: "our policy adds scoring work; here's why the router still gets
*faster*." Upstream: blocking-the-event-loop is bugfix-flavored — high merge odds, and it's
in roadmap #855's "P2 router performance improvements" lane.

**Honest caveat:** with 2 instances and moderate QPS the absolute win may be small router-side
milliseconds; the effect grows with QPS and prompt length. Frame as overhead-elimination +
scalability microbenchmark, not a headline p99 claim.

**Cost:** ~1 day code + ½ day microbenchmark. **Criteria fit:** rubric ✓✓, one-story ✓,
risk none, ladder ✓ (orthogonal rung), upstream ✓✓.

### G — Work-left load signal (predictive load penalty)

**Code fact:** the router already tracks, per instance, `in_prefill_requests`,
`in_decoding_requests`, `avg_decoding_length`, `avg_itl` (`stats/request_stats.py:35`),
and each request carries `max_tokens` at admission time. A count-based `load_penalty`
treats a request about to finish the same as one that just started; expected-work is the
better signal: `load = w_p·in_prefill + Σ expected_remaining_decode` (from avg decode
length, optionally capped by the request's own `max_tokens`).

**Why interesting:** this is roadmap #855's **P2 "predictive routing for future workloads"**
— acknowledged, unclaimed. Pure router, deterministic tests. Crucially it is *not a sibling
mechanism*: it upgrades the `load` term inside the existing equation, so it can ship as the
load signal *within* adaptive β rather than competing with it. One extra ablation line
(count-based vs work-left load) on the same grid.

**Cost:** ~1–2 days. **Criteria fit:** rubric ✓, one-story ✓✓ (same equation), risk none,
ladder ✓, upstream ✓ (roadmap P2).

### H — Tier-aware cache benefit (bandwidth-calibrated discount)

**Code fact:** the controller lookup's `layout_info` values are `(location, matched_tokens)`
tuples — kvaware reads `[1]` and ignores `[0]` (`routing_logic.py:387`); LMCache's control
messages model location explicitly (`MoveMsg(old_position=(instance, location), …)`). A hit
on disk-offloaded KV costs an onload before it saves prefill; a GPU-resident hit is free.
So: `cache_benefit = Σ tier_weight(location)·matched_tokens`, tier weights from measured
onload bandwidth. This is the Dynamo kv-router "bandwidth-calibrated tier discount" idea our
deep-research named-but-rejected — the flip condition is closer now because the
multi-instance lookup extension already surfaces `layout_info` per instance.

**Why cautious:** on our 2×A10 setup with a 20 GB CPU buffer and no disk tier, GPU-vs-CPU
onload deltas may be small → weak effect size. **Verify first** (½ day): confirm at our
pinned lmcache `0.3.9post2` that lookup actually reports distinguishable locations, and
measure CPU-onload vs recompute latency for a 2k-token prefix. If the delta is <15% of
prefill time, drop this.

**Cost:** ~1 day code after the spike. **Criteria fit:** rubric ✓, one-story ✓ (extra term,
same equation), risk low, ladder ✓, upstream ✓ (uses data kvaware already gets and drops).

### I — Cache-aware admission/queuing policy (the #876 deferred question)

Router-side queue whose *dequeue order* uses our score (cache locality × wait time) — i.e.,
answer "LMCache locality vs FIFO fairness" directly. Highest upstream relevance of anything
on the table (P1 roadmap, active RFC), but **wrong for us as an implementation**: the infra
(#905) is unmerged and explicitly excludes the admission controller; building our own queue
means timing-dependent tests inside the correctness-40% story. **Right move:** keep it as
report Discussion + an upstream RFC comment citing our loadaware results as evidence for
their deferred policy decision — grade-100 leverage at zero implementation risk.

### Micro-PR for the upstream portfolio (not a second optimization)

`PrefixAwareRouter` picks `random.choice` among matched endpoints and ignores both stats
dicts (`routing_logic.py:518`) — a two-line change picks the least-loaded matched endpoint.
Tiny, obviously correct, mergeable; adds one more item to the upstream-PR track.

## Recommendation

1. **Keep adaptive β as the decided second optimization** — upstream moved ~nothing in July;
   every input to the 2026-07-05 memo still holds, and #876/#905 staying open means the gap
   is still ours to claim.
2. **Fold G (work-left load) into the existing design** as the load-signal definition used
   by static-β and adaptive-β alike — it deepens the same equation and claims the P2
   predictive-routing lane without a second narrative.
3. **Adopt F (fast-path/tokenization) as the low-risk third rung / first upstream PR** — it
   is the only new candidate that *strengthens correctness + reproducibility* (deterministic
   tests, overhead profile the rubric already demands) rather than adding a gain claim.
4. **Run the ½-day H spike only if schedule allows** after core + static β land; drop it
   silently if the CPU-onload delta is small.
5. **Do not implement I**; spend it as Discussion + upstream RFC comment.

If one candidate must *replace* adaptive β (e.g., schedule pressure), F is the replacement:
strictly lower risk, still router-only, and its evaluation is already mandated by §3 — the
report arc becomes "right placement (core) + fast placement (F)" instead of
"right placement + self-tuning placement".

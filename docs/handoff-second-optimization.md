# Handoff: choose the second optimization (or decide none is needed)

> Written 2026-07-05 during a requirements-grounding session. This brief is self-contained:
> a fresh Claude Code session should be able to run the deep-dive from this file alone.
> Outcome expected from that session: a **decision memo** (see "Deliverable" at the bottom).

## Why this handoff exists

The project's **core is locked** (see below). The open question is the **second
optimization**: the candidate ideas on the table feel not-obviously-strong, and the owner
wants a dedicated deep-dive across the production-stack + LMCache ecosystem to find the
*best* opportunity — not just pick from the first menu we happened to enumerate.

## What is already locked (do not re-litigate)

1. **Core contribution:** add a `loadaware` routing strategy to the production-stack router
   scoring `α·cache_hit_benefit − β·load_penalty`, which requires extending LMCache's
   controller `lookup()` from first-match to **per-instance match info** (fixes an
   acknowledged upstream TODO). Evidence: `docs/feasibility-verification.md` §1–§3.
2. **Router-image-only bias:** the controller (and thus the lookup code) runs *inside the
   router pod*. Core ships without touching engine images. Engine restarts are slow and
   worker registration is one-shot (`deploy/README.md` gotcha #1), so any candidate that
   forces engine-image rebuilds carries a real iteration-speed tax — count it against them.
3. **Framing rule (assignment fit):** the story is *"KV-cache-aware request placement:
   extending a distributed KV cache's placement policy to weigh cache-hit benefit against
   queueing cost"* — never headline it "load balancing". The §1 scope clause "other things
   if they make sense" covers us; hit rate stays a first-class metric.
4. **Upstream-PR portfolio is a parallel track, not the second optimization.** The found
   bugs (router Service missing controller ports 9001/9002; one-shot worker registration;
   image-pairing matrix) are documented in `docs/upstream-findings.md` and should become
   issues/PRs regardless of what this session decides. Grade-100 clause: merged upstream
   contributions.

## The known menu (starting point, not the boundary)

| # | Candidate | Where it runs | Key risk | Notes from the grounding session |
|---|---|---|---|---|
| A | Hot-prefix replication, tunable threshold `k` | Controller (router image) + engine *config* | NIXL/P2P unverified on our image/A10s; young worker-side move code | Mechanism already exists end-to-end: `MoveMsg(copy=True)` → source worker pushes → dest **CPU backend only**. `k=1` (= "always mirror") is an ablation corner, not the design: tail pollution under Zipf + capacity halving. |
| B | Router-triggered pre-warm: when loadaware routes a hot prefix *away* from its cache holder, controller issues the copy for exactly that prefix | Same as A | Same NIXL risk | Most elegant A-variant; no threshold to justify; ablation ladder kvaware → +lookup → +load → +pre-warm. Was the session's tentative Plan A, gated on a 1-day NIXL spike. |
| C | Adaptive β: feedback loop replacing the static load weight (react to queue-depth imbalance / TTFT drift) | Router only | None (pure Python) | Zero deployment risk; deepens the core instead of bolting on a sibling; strong §5 "sensitivity" story. Was Plan B if the NIXL spike fails. |
| D | Cost-aware eviction policy in LMCache's pluggable `cache_policy` (GDSF-style; LRU/LFU/FIFO/MRU exist) | **Inside engines** | Engine-image rebuilds; forks the report into two stories | Matches §4's literal example. `docs/feasibility-verification.md` §5: one file + one mapping entry, unit-testable offline without GPUs. |
| E | No second implementation — spend the time on evaluation depth + upstream PRs | — | None | Legitimate: §5 requires ablation only "if you combined multiple ideas"; rubric = correctness 40 / repro 30 / gain 15 / clarity 15. |

## Decision criteria (rank candidates against these, in this order)

1. **Rubric fit:** does it strengthen the *convincing-the-grader* axes (correctness,
   reproducibility) rather than just adding a second gain claim worth ≤15%?
2. **One coherent story:** the report must stay a single narrative. A candidate that needs
   its own separate workload/eval design (e.g., D) pays a clarity tax.
3. **Deployment risk:** router-image-only ≫ engine-config-only ≫ engine-image-rebuild.
   Anything gated on NIXL/P2P needs the spike to pass first (defined below).
4. **Ablation ladder:** can it be presented as one more rung on the core's ladder, isolated
   with one graph?
5. **Upstream mergeability:** does it fix something the maintainers acknowledge (TODOs,
   roadmap items)? That's the 100-grade path.

## What the deep-dive session should actually do

1. **Survey the real surface:** clone `vllm-project/production-stack` (feasibility refs
   are @ `1e973a3`, 2026-06-26) and `LMCache/LMCache` (@ `bf20f51`, 2026-07-04). Read the
   roadmap/issues for acknowledged gaps (load/priority routing is one; find others —
   e.g., lookup-cost, registration robustness, eviction, offload admission).
2. **Mine our own research:** the deep-research verdict lives in
   `research/notes/final_report_kv-offload-vs-routing.md` (local, gitignored — ask Eliad
   if missing). Named-but-rejected alternatives with flip conditions: Dynamo `lib/kv-router`
   bandwidth-calibrated tier discount; κ-aware offload admission; vllm-project/router
   RFC #51. NotebookLM corpora: `d5a7565e` (272 sources), `e4f7c11c` (132 sources).
3. **Run the NIXL spike if A/B stay in contention** (timebox: 1 day): enable
   `enable_p2p=True` + NIXL channel via chart env vars on cluster `gapu-2` (namespace
   `cache-llm`), confirm P2P initializes in `lmcache/vllm-openai:v0.3.9post2` on the A10s,
   issue one `MoveMsg(copy=True)`, verify the chunk lands in the destination CPU backend
   and a subsequent request onloads it. Any failure ⇒ A/B are out.
4. **Quantify each finalist:** expected effect size on our 2×A10 / Qwen2.5-3B / Zipf
   setup (order-of-magnitude is fine), implementation cost in days, and which images it
   touches.

## Constraints the winner must respect

- Cluster: OpenShift `gapu-2`, 2× A10 23 GB, namespace `cache-llm`, VPN required.
- Pinned pairing (do not break): router `lmstack-router:0.1.9.dev9-g37bafbcf5.d20260107`
  + engines `vllm-openai:v0.3.9post2` — both lmcache 0.3.9post2. Version skew fails
  *silently* (`deploy/README.md` gotcha #0b).
- No gated/HF-token models; model stays Qwen/Qwen2.5-3B-Instruct across all experiments.
- Router restart ⇒ engines must be rollout-restarted (one-shot registration).
- On-cluster image builds blocked by the QuayIntegration webhook; builds happen locally.

## Deliverable of the deep-dive session

A short decision memo (`docs/decisions/second-optimization.md`): the chosen candidate (or
explicit "core-only, invest in evaluation"), the two runners-up with the condition that
would flip the decision, effect-size estimate, implementation cost, and which rung it adds
to the ablation ladder. Record the decision in `CHANGELOG.md` under **Decided**.

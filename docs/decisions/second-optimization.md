# Decision: Second Optimization = Adaptive β (feedback-controlled load weight)

> Decided 2026-07-05, deep-dive session per `docs/handoff-second-optimization.md`.
> Survey basis: `vllm-project/production-stack` @ `1e973a3`, `LMCache/LMCache` @ `bf20f51`
> (fresh clones), both projects' 2026 roadmaps/issue trackers, and one live read-only
> check on cluster `gapu-2`.

## The decision

**Candidate C wins: replace the core's static load weight β with a feedback loop**
(`BetaPolicy`), driven by the router's own **fresh, event-driven load signals** —
not by the scraped engine stats, which are 15–30 s stale by default.

The deep-dive strengthened C beyond how the handoff menu described it. Two code facts
found this session turn "adaptive β" from a sensitivity knob into a needed deepening
of the core:

1. **The scraped load signal is stale by design.** `EngineStats` arrives via a
   background scrape loop — CLI default 30 s (`parser.py:313`), helm chart default 15 s
   (`values.yaml:539`, exposed as `routerSpec.engineScrapeInterval`). Queues under burst
   build in sub-second time; a β acting on a 15–30 s-old queue depth herds requests at
   whatever instance *looked* idle last tick (classic stale-load-info herding). Upstream
   knows: the router-queuing groundwork PR (production-stack #905, post-dates our pin)
   adds an "admission-oriented **fast** engine-stats refresh" for exactly this reason.
2. **The router already holds a zero-lag load signal it never uses for routing.**
   `RequestStats` (`stats/request_stats.py:35`) tracks per-backend
   `in_prefill_requests` / `in_decoding_requests` and a TTFT moving average, updated
   event-driven on request arrival / first token / completion — no scrape lag. The
   loadaware score (and the β controller) should read these, optionally blended with
   scraped `num_queuing_requests`.

### Scope sketch (what "adaptive β" concretely means)

- `LoadAwareRouter` takes a pluggable `BetaPolicy`: `static` (default, = core) or
  `adaptive`.
- Signal: per-instance imbalance from router-side stats, e.g.
  `imbalance = (max−min in-flight) / (max+min)` and/or inter-instance TTFT drift.
- Update rule (multiplicative-weights style, clipped):
  `β ← clip(β · exp(η · (imbalance − target)), β_min, β_max)`, evaluated per tick or
  per N routed requests.
- Unit tests: synthetic stats streams (step imbalance, oscillation, cold start) assert
  convergence and no-oscillation — deterministic, offline, no GPU.

### Why C, against the handoff's criteria (in their priority order)

1. **Rubric fit (correctness 40 / repro 30):** pure Python in the router; deterministic
   unit tests; no new runtime dependency. B/A would put NIXL runtime behavior inside the
   reproducibility story; D needs engine-image rebuilds and its own eval design.
2. **One coherent story:** the report's arc stays a single equation —
   *"score = α·cache_benefit − β·load; but what is β? A fixed β is tuned to one
   operating point; close the loop."* No sibling mechanism, no second workload design.
3. **Deployment risk:** zero. Router-image-only; identical dev loop as the core.
4. **Ablation ladder:** rung 4 of one ladder — `kvaware → +multi-instance lookup →
   +load (static β) → +adaptive β` — isolated with one graph (p99 + hit rate over a
   Zipf-s × QPS grid, one line per rung).
5. **Upstream mergeability:** C's weakest axis (a feedback controller is
   research-flavored). Mitigation: the mergeability story already rides on the parallel
   PR track (multi-instance lookup fixes an acknowledged TODO — still open at `bf20f51`,
   `kv_controller.py:402`; service-ports fix; heartbeat re-registration). The staleness
   finding itself is citable upstream: #876/#905 show maintainers converging on the same
   gap, and #876 explicitly *defers* the "LMCache locality vs FIFO fairness" policy
   question that our loadaware scoring answers.

### Effect size (order of magnitude, 2×A10 / Qwen2.5-3B / Zipf)

The honest headline is **robustness**, not a bigger peak number: one adaptive config
holds p99 near the per-point-best static β across the whole Zipf-s × QPS grid, while any
single static β is off-tuned somewhere on it. At off-tuned points a static β either
herds (β too low → kvaware-like pileup, p99 blows up by seconds / 2–5×) or sheds cache
hits (β too high → roundrobin-like, hit rate drops by tens of pp). Expected result:
**2–5× p99 improvement at off-tuned operating points vs the single best static β, with
hit rate within ~5 pp of kvaware**. This also *reduces* eval burden: the §5 α/β
sensitivity sweep the report needs anyway doubles as the adaptive-β baseline grid.

### Implementation cost

~1–2 days code (BetaPolicy + unit tests; the fresh-signal plumbing is reading fields the
router already computes) + 1–2 days eval sweeps (mostly machine time). Images touched:
**router only**.

## Runners-up and flip conditions

**Runner-up 1 — B (router-triggered pre-warm via `MoveMsg(copy=True)`).**
New evidence this session *improved* B's odds: `nixl` 0.7.1 is importable inside the
running engine pods (checked live via `oc exec` — vLLM 0.11's dependency chain provides
it even though LMCache's `v0.3.9post2` Dockerfile never installs it), and the full
worker-side move path exists at the engine tag `v0.3.9post2` (verified at the tag, not
just at LMCache HEAD). B remains out because it loses on criteria 1–3 regardless of the
spike: replication correctness is timing-dependent and hard to test, NIXL enters the
repro story, benefit is bounded by (prefill recompute − CPU→GPU onload) on redirected
hot requests only — likely <10 % aggregate p95 on our setup. **Flip to B if all of:**
(a) core + static-β eval land with ≥3 spare days before report freeze; (b) the 1-day
NIXL spike (handoff §3) passes; (c) the static-β eval shows loadaware frequently
redirecting hot prefixes away from their cache holder (i.e., pre-warm would have real
traffic to help). The spike was deliberately **not** run this session: A/B were already
out on criteria grounds, and it would churn the validated baseline deployment.

**Runner-up 2 — E (core-only, invest in evaluation + upstream PRs).**
**Flip to E if** the core (multi-instance lookup + static-β loadaware) is not landed
and validated with most of the schedule still ahead; adaptive β then collapses
gracefully to the static α/β sensitivity sweep, which §5 requires anyway. This is the
built-in safety of choosing C: the off-ramp loses a rung, not the story.

**Eliminated — A** (threshold-k hot-prefix replication): dominated by B (same NIXL risk,
plus a threshold to justify and tail-pollution/capacity concerns). **D** (cost-aware
eviction policy): requires engine-image rebuilds (slow dev loop, one-shot-registration
tax) and forks the report into a second workload/eval narrative — fails criteria 2–3
hardest.

## Other survey findings worth recording

- **LMCache development is migrating to "MP mode"** (Q2 roadmap, issue LMCache#2923);
  the v1 `cache_controller` we extend is legacy-but-current for production-stack's
  kvaware routing. Consequence: file the lookup-extension PR early, while v1 is the
  integration surface production-stack actually uses.
- production-stack 2026 roadmap (#855) lists "priority routing" (P2) and router-side
  queuing (P1, #876/#905) — our framing ("placement policy weighing cache-hit benefit
  against queueing cost") sits in an acknowledged, unclaimed gap between them.
- LMCache RFC #3652 ("placement policy extension point") is about DRAM/DAX L1 region
  placement, unrelated to our routing-level placement — no new candidate there.

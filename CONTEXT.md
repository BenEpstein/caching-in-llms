# CONTEXT.md - ubiquitous language

> status: live · 2026-08-07 · the vocabulary the code, report and tickets all use; definitions
> verified against `patches/vllm_router/routers/routing_logic.py` on 2026-08-05 (issue #29);
> doc pointers re-verified 2026-08-07 (issue #59)

Glossary of project terms. Code, docs, the report, and conversations should use these
words with exactly these meanings. Implementation details live elsewhere.

## Terms

- **Instance** — one serving-engine replica (vLLM + its private LMCache), one per GPU.
  Two exist. An instance's cache tiers are private to it unless explicitly transferred.
- **Router** — the CPU-only front pod that assigns each request to an Instance. Hosts the
  **Controller**.
- **Controller** — the LMCache control-plane process (registry of what each instance
  caches, lookup, move/copy commands). Runs *inside the router pod*, not in the engines.
- **Worker Registration** — an instance's one-shot announcement to the Controller at
  engine startup. Never retried: a Controller restart empties the registry until the
  engines restart. (Root of the router-restart ⇒ engine-restart rule.)
- **Cache-Hit Benefit** - the **fraction** of a request's prompt tokens an instance already
  holds in its cache tiers, `matched_tokens / prompt_tokens`. kvaware maximizes the raw
  count; `loadaware` normalizes it, which is what makes β dimensionless and therefore
  prompt-length invariant.
- **Load Penalty** - a live measure of how busy an instance is: its in-flight requests,
  `in_prefill_requests + in_decoding_requests`. **No queue term** - `num_requests_waiting`
  is collected as a run diagnostic but does not enter the score. The quantity kvaware ignores.
- **Relative Load** - Load Penalty expressed as a signed fraction of the fleet mean,
  `(load − mean) / max(1, mean)`, recomputed per request. 0.0 is "average", +1.0 is "twice
  the fleet average". The denominator is clamped at 1 so a near-idle fleet reports no
  imbalance to act on. This is the term β actually weighs.
- **Load Imbalance** - the *measured outcome*, not an input to the score: the ratio of busiest
  to idlest instance mean in-flight count over a seed's send-timestamp window
  (`analyze.py:per_seed_imbalance`, engine job only). 1.0 is perfectly even. Distinct from
  **Relative Load**, which is the per-request term β weighs; Load Imbalance is what that term
  is trying to reduce, and it is one of the two pre-registered tested claims. Do not use the
  two words interchangeably.
- **Placement Policy** - the router's rule for choosing an instance. `kvaware` = baseline
  placement policy (pure cache-hit benefit, first-match). **`loadaware`** = our enhanced
  placement policy: `cache_hit_benefit − β·relative_load`, where relative_load is the
  engine's in-flight count as a signed fraction of the fleet mean. Framing rule: this is
  *KV-cache-aware request placement*, never headlined "load balancing".
- **Lookup Extension** — extending the Controller's lookup from "first instance holding
  the prefix" to per-instance match info for all instances. Core infrastructure for any
  placement or replication decision; fixes an acknowledged upstream TODO.
- **Replication Mechanism** — the existing LMCache transfer chain (controller-issued
  copy → source worker pushes KV to the destination's CPU tier). Exists upstream; gated
  on P2P/NIXL; nothing invokes it today.
- **Replication Policy** — the (nonexistent upstream) logic deciding *which* entries earn
  replication and *when*. Candidate second optimization; parked - rationale archived on
  [issue #1](https://github.com/BenEpstein/caching-in-llms/issues/1#issuecomment-5218375689).
- **Hot Prefix** — a shared prompt prefix popular enough that its placement materially
  skews load; produced deliberately by the Zipfian workload.
- **TTFT SLO** - a service-level objective on time-to-first-token, in seconds. The
  tunable behind **Goodput**; `analyze.TTFT_SLO_S` holds the provisional default.
  Always say "objective", never "threshold" - the latter is what the pre-registered
  alpha is, and the two must not blur.
- **Goodput** - the fraction of requests *sent* whose first token arrived under the
  **TTFT SLO**. Denominator is requests sent, so an error is a miss. The quantity the
  paired test consumes is its complement, the *miss rate* (`ttft_slo_miss`), because
  every test here is one-sided lower-is-better. EXPLORATORY as of 2026-08-06: first
  computed after the pre-registered TTFT p95 null, so it is not a tested claim until a
  pre-registration fixes the metric and the objective before the data exists.
- **Affinity Probe** — the smoke test: N requests sharing a long prefix; pass = the
  Controller reports both workers registered and follow-up requests land on the
  cache-holding instance.

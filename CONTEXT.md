# CONTEXT.md — ubiquitous language

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
- **Cache-Hit Benefit** — how many of a request's prompt tokens an instance already holds
  in its cache tiers; the quantity kvaware maximizes.
- **Load Penalty** — a live measure of how busy an instance is (running + queued
  requests); the quantity kvaware ignores.
- **Placement Policy** — the router's rule for choosing an instance. `kvaware` = baseline
  placement policy (pure cache-hit benefit, first-match). **`loadaware`** = our enhanced
  placement policy: `α·cache_hit_benefit − β·load_penalty`. Framing rule: this is
  *KV-cache-aware request placement*, never headlined "load balancing".
- **Lookup Extension** — extending the Controller's lookup from "first instance holding
  the prefix" to per-instance match info for all instances. Core infrastructure for any
  placement or replication decision; fixes an acknowledged upstream TODO.
- **Replication Mechanism** — the existing LMCache transfer chain (controller-issued
  copy → source worker pushes KV to the destination's CPU tier). Exists upstream; gated
  on P2P/NIXL; nothing invokes it today.
- **Replication Policy** — the (nonexistent upstream) logic deciding *which* entries earn
  replication and *when*. Candidate second optimization; parked - see
  `docs/decisions/second-optimization.md` (frozen; handoff doc removed 2026-08-01, in git history).
- **Hot Prefix** — a shared prompt prefix popular enough that its placement materially
  skews load; produced deliberately by the Zipfian workload.
- **Affinity Probe** — the smoke test: N requests sharing a long prefix; pass = the
  Controller reports both workers registered and follow-up requests land on the
  cache-holding instance.

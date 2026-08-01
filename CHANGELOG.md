# Changelog

All notable changes to this project are documented here, newest first.
Format adapted from [Keep a Changelog](https://keepachangelog.com/en/1.1.0/): one entry per
work session (or significant commit), with **Added / Changed / Decided / Fixed** subsections as
applicable. Since this is a research project, **Decided** captures project-direction decisions
with a pointer to the evidence — those matter as much as code.

## [Unreleased]

### Open for next session (final implementation planning)
- **Benchmark plan is deliberately undecided** — workload data/shape, sweep grid, run
  lengths, and stats methodology to be finalized as their own decision (a proposal was
  discussed 2026-08-01 but nothing locked). Everything else is locked per the
  2026-08-01 entries below: two PRs, load-signal tunable (count/work-left), adaptive β,
  F dropped.

## 2026-08-01 (evening) — Scope lock: PRs and load signal

### Decided
- **Upstream-PR scope narrowed to the two the project requires** (Ben): LMCache
  per-instance lookup (core dependency, filed early) and production-stack loadaware
  router (filed after benchmarks). Ports fix stays a documented deploy workaround;
  re-registration stays a gotcha note. No drive-by fixes/PRs.
- **Core locked with G folded in** (Ben): loadaware score's load term is a tunable
  signal — `count` (in-flight requests, default/baseline) vs `work-left` (estimated
  remaining tokens from existing RequestStats; the G idea) — compared as one ablation
  rung. F dropped (partially fixed upstream by #1025; residual value not worth a rung).
  Ladder: kvaware → +per-instance lookup → +load(count) → +work-left → +adaptive β.

## 2026-08-01 (later still) — FullLookup overlap verified

### Decided
- **Lookup extension proceeds unchanged; cite + build on LMCache #1420.** "FullLookup"
  (= LMCache PR #1420, feeding production-stack #670) is functionally the same
  per-instance lookup capability we're building, but was auto-closed stale 2025-12-25,
  unmerged, no design objections. Verified at LMCache HEAD `0427938a`: no FullLookup in
  code/history, `kv_controller.py` identical to our pin, multi-results TODO still open.
  Our PR = revive the capability with the benchmarks both dead attempts (#1420, #884)
  lacked. Evidence: `docs/router-optimization-ideas.md` (FullLookup section).

## 2026-08-01 (later) — Prior-art check on the core idea + F correction

### Added
- Prior-art section in `docs/router-optimization-ideas.md`: core blended-score idea still
  unclaimed upstream, but warm — #884 (switch-based load+kvaware combo, died 2026-06 for
  lack of benchmarks; citable prior art), #852 (least-QPS only, stalled), #670 (TTFT
  routing draft, dormant, closest to idea G; uses LMCache "FullLookup" — **verify overlap
  with our lookup extension**). Urgency reinforced: file the lookup-extension PR early.

### Changed
- Idea F corrected: upstream #1016/#1025 (2026-07-29) already fix the event-loop-blocking
  half (thread offload only, explicitly no tokenization caching) — F narrows to
  prefix-cached tokenization + the overhead benchmark; upstream angle = extend #1025.

## 2026-08-01 — Fresh optimization-idea survey of production-stack `main`

### Added
- `docs/router-optimization-ideas.md` — survey of `main` @ `3314ee6` for second-optimization
  candidates beyond the 2026-07-05 menu: F fast-path/tokenization (blocking event-loop work
  in `KvawareRouter`, strongest new find), G work-left load signal (roadmap P2 "predictive
  routing"), H tier-aware benefit discount (gated on a ½-day spike), I queuing policy
  (Discussion/RFC-comment only), plus a prefixaware micro-PR for the upstream track.

### Decided
- **Adaptive β stays the second optimization** — upstream barely moved since the pin
  (18 commits, no routing-logic changes; #876/#905 still open with the locality-vs-fairness
  question still deferred), so the 2026-07-05 memo's evidence holds. New ideas slot in as:
  G folded into the load-signal definition, F as low-risk third rung / first upstream PR.
  Evidence: `docs/router-optimization-ideas.md`.

## 2026-07-05 (later) — Second-optimization deep-dive

### Decided
- **Second optimization = adaptive β** (feedback-controlled load weight, pluggable
  `BetaPolicy`, driven by the router's fresh event-driven `RequestStats` instead of the
  15–30 s-stale scraped `EngineStats`). Runners-up with flip conditions: B (pre-warm,
  gated on NIXL spike + spare days) and E (core-only). Full rationale + survey evidence:
  `docs/decisions/second-optimization.md` (Claude session, per handoff brief).
- NIXL spike **not** run: A/B eliminated on criteria 1–3 regardless of outcome; but the
  key static unknown was settled live — `nixl` 0.7.1 IS importable in the running
  `v0.3.9post2` engine pods, so B's flip condition is realistic.

### Added
- `docs/decisions/second-optimization.md` — decision memo (also records: lookup TODO
  still open at `bf20f51`; LMCache migrating to MP mode → file the lookup PR early;
  upstream #876/#905 confirm the load-signal-freshness gap).

## 2026-07-05 — Requirements grounding session (grilling)

### Decided
- **Core contribution locked:** `loadaware` placement policy (α·cache_benefit − β·load) +
  the multi-instance Lookup Extension it requires; ships router-image-only (the controller
  runs in the router pod — engine images stay official/pinned) (Eliad).
- **Framing rule** for §2/report: "KV-cache-aware request placement", never headline
  "load balancing"; hit rate stays a first-class metric (Eliad).
- **Second optimization parked** — deep-dive delegated to a dedicated session;
  self-contained brief with candidate menu, decision criteria, and NIXL-spike gate in
  `docs/handoff-second-optimization.md` (Eliad).
- Upstream-PR portfolio (service ports, one-shot registration, image matrix) is an
  always-on parallel track, independent of the second-optimization choice (Eliad).

### Added
- `CONTEXT.md` — project glossary (ubiquitous language: Instance, Controller, Placement
  Policy, Lookup Extension, Replication Mechanism vs Policy, Affinity Probe, …) (Eliad).
- `docs/handoff-second-optimization.md` — handoff brief for the second-optimization
  deep-dive (Eliad).

## 2026-07-04 (later) — Baseline VALIDATED end-to-end

### Added (post-validation polish)
- **External Routes — no port-forward needed** (VPN required, documented in `deploy/README.md`):
  model API `https://llm-cache-llm.apps.gapu-2.customers.k8s.co.il/v1` (OpenAI-compatible,
  no key), Grafana `https://grafana-cache-llm.apps.gapu-2.customers.k8s.co.il`
  (admin / cache-llm, password set in values — fine for VPN-only cluster) (Eliad).

### Fixed (post-validation polish)
- LMCache/vLLM dashboards showed "datasource not found": the shipped dashboard JSONs
  hardcode datasource uid `prometheus`; pinned our provisioned datasource to that uid in
  values. Verified: dashboards load, all 3 Prometheus targets up (Eliad).

### Fixed
- **Found the working official image pairing:** router `lmstack-router:0.1.9.dev9-g37bafbcf5.d20260107`
  + engines `vllm-openai:v0.3.9post2` — both lmcache 0.3.9post2 (pin-history archaeology:
  production-stack pinned 0.3.9post2 until 2026-01-14; no official engine image carries the
  0.3.11 that `:latest` needs). Older router = single controller socket on 9000, so the chart's
  stock Service suffices; reply/heartbeat flags removed from routerSpec (Eliad).

### Added
- `docs/upstream-findings.md` — the four control-plane findings written up for the final
  report + upstream issues/PRs: one-shot worker registration (router restart silently kills
  kvaware), chart Service missing controller ports, the image-pairing incompatibility matrix
  (incl. broken official tutorial), and the silent-failure design critique; includes the
  reusable affinity-probe snippet (Eliad).
- **Prefix-affinity validation PASSED:** both workers registered
  (`Registered instance-worker` ×2 in router logs); 3 requests sharing a 1200-token prefix —
  first falls back (cold), second and third log `found by kvaware router` and land on the same
  instance, latency 0.83s → 0.39s from the KV hit. Ecosystem fully operational: 2×A10 engines,
  kvaware router, Grafana dashboards, Prometheus, benchmark harness. Next: agree on the
  optimization design (Eliad).

## 2026-07-04 — Direction pivot, deep-research verdict, baseline decision, first benchmark code

### Decided
- **No modifications to any production-stack component until the optimization design is agreed**
  (Eliad, during deployment session). Official images only; the in-progress custom router-image
  build (needed to align lmcache versions) was cancelled and its BuildConfig removed. Consequence:
  baseline runs on `lmstack-router:latest` (lmcache 0.3.11) + `vllm-openai:v0.3.9post2`
  (lmcache 0.3.9.post2) — the closest official pairing; kvaware registration with this pairing
  is being validated now that the service-port fix is in.
- Router-image builds, when we get to them, are additionally blocked by a cluster-level
  QuayIntegration admission webhook (defunct Quay install never provisioned the builder SA in
  `cache-llm`). Unblock options recorded in the session log; needs a cluster-scoped denylist
  patch or local container tooling.
- **Pivot from the May direction (fork SGLang + GDSF eviction) to KV-cache infrastructure.**
  A full-tier deep-research run (~180 sources, 6 depth investigations, adversarial review)
  compared KV offload/onload vs. KV-aware routing head-to-head for our rubric and hardware.
  Verdict: fork **vLLM Production Stack** and build a combined cache-hit + load weighted routing
  policy against its still-primitive shipped routers. Report:
  `research/notes/final_report_kv-offload-vs-routing.md` (local, gitignored) — key findings:
  the honest-baseline inversion (production-stack is the one repo whose default router is not
  yet hybrid), the A10 offload roofline (naive offload is PCIe-bound; only a κ-aware admission
  policy is defensible), and the N=2 statistical-benchmark gap in the literature.
- **Baseline = vLLM Production Stack + LMCache** with a `loadaware` routing strategy; design in
  `docs/project-brief.md`, code-level feasibility with file/line references in
  `docs/feasibility-verification.md` (Eliad).
- Named alternatives with flip conditions (report §6): Dynamo `lib/kv-router` bandwidth-calibrated
  tier discount; κ-aware offload admission; vllm-project/router RFC #51 (Rust prestige lane).

### Added
- `benchmarks/` — seedable Zipfian prefix workload generator + unit tests, async load driver
  emitting TTFT/E2E/token metrics to CSV (Eliad).
- `deploy/` — OpenShift configs fit to cluster reality: RWX CephFS model PVC, burstable memory,
  `values-baseline-kvaware.yaml` (Eliad).
- Observability on the cluster: Grafana (via kube-prometheus-stack subchart, operator/CR
  disabled) + plain single-pod Prometheus (`deploy/prometheus.yaml`, 5s pod-SD scrape of
  engines + router) with the shipped vLLM/LMCache dashboards preloaded (Eliad).
- Stack deployed to `cache-llm` on gapu-2: router + 2× Qwen2.5-3B replicas, one per A10 (Eliad).

### Fixed (deployment debugging — all documented in `deploy/README.md` gotchas)
- **Upstream chart bug (PR candidate):** router Service omits LMCache controller ports
  9001/9002 → worker registration hangs silently → kvaware degrades to QPS routing.
  Patched the Service; diagnose via router `/metrics` `registered_workers_count` (Eliad).
- **lmcache version skew router↔engine breaks the controller↔worker ZMQ protocol silently.**
  Tried `vllm-openai:v0.5.1rc2` — its lmcache is 0.5.1rc2, a *major* jump past the router's
  0.3.11 (register messages arrive post-port-fix but fail to decode; `reply_socket_message_count`
  grows while `registered_workers_count` stays 0). Reverted to `v0.3.9post2`, the closest
  official engine release; first fair compatibility test with ports fixed is in progress (Eliad).
- OpenShift arbitrary-UID crash (`HOME=/` unwritable → flashinfer dies): `HOME=/tmp`;
  GPU rolling-update deadlock: `strategy: Recreate`; router startup-probe kill during
  ~20s kvaware init: relaxed threshold; Grafana default-datasource collision:
  `defaultDatasourceEnabled: false` (Eliad).
- `docs/project-brief.md`, `docs/feasibility-verification.md` (Eliad).
- `CHANGELOG.md` (this file) + changelog discipline in `CLAUDE.md`.
- NotebookLM notebooks for grounded Q&A over the research corpora: `d5a7565e` (offload-vs-routing
  report + 272 sources), `e4f7c11c` (May baseline-selection corpus, 132 sources).

### Changed
- `CLAUDE.md` slimmed for collaboration; machine-specific tooling moved to each collaborator's
  gitignored `CLAUDE.local.md`; recorded the baseline decision.
- Prior research run's pipeline artifacts archived to `research/runs/llm-cache-baseline/` (local).

### Fixed
- Cleaned up 9 duplicate NotebookLM notebooks created by an auto-push hook bug (dedup key
  included file hash, so every report edit re-pushed); hook fix tracked separately in the
  claude-config repo.

## 2026-07-03 — Repository bootstrap

### Added
- Initial commit: project docs (`docs/references/` course PDFs, `docs/caching-landscape.md`),
  `CLAUDE.md`, research skill files, `.gitignore` (excludes `research/` working data and
  `.hyperresearch/`).
- Private GitHub repo `BenEpstein/caching-in-llms`; Eliad added as collaborator.

### Decided
- (Superseded 2026-07-04) May deep-research verdict: fork SGLang RadixAttention + cost-aware
  GDSF eviction policy. Report: `research/notes/final_report_llm-cache-baseline.md` (local).

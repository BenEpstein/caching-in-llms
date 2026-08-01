# Changelog

All notable changes to this project are documented here, newest first.
Format adapted from [Keep a Changelog](https://keepachangelog.com/en/1.1.0/): one entry per
work session (or significant commit), with **Added / Changed / Decided / Fixed** subsections as
applicable. Since this is a research project, **Decided** captures project-direction decisions
with a pointer to the evidence — those matter as much as code.

## [Unreleased]

## 2026-08-01 (implementation) — Change 2 landed: `loadaware` placement (issue #5)

### Added
- **`loadaware` placement policy** in `patches/vllm_router/routers/routing_logic.py`:
  `LOADAWARE` enum value, a factory branch mirroring `KVAWARE`, and a `LoadAwareRouter`
  (subclass of `KvawareRouter`) that scores **every** endpoint by
  `α·(matched_tokens/prompt_tokens) − β·(in_prefill + in_decoding)` and routes to the argmax.
  The patch is **additions only** — `KvawareRouter` is byte-identical, so the baseline arm of
  the experiment is untouched. Registered in both `get_routing_logic()` and
  `cleanup_routing_logic()`.
- **α/β exposed as tunables** (§4 requires this): `LOADAWARE_ALPHA` / `LOADAWARE_BETA` env
  vars, defaults 1.0 / 0.1, overridable by kwargs. Documented in `patches/README.md`.
- **31 more offline unit tests** (`tests/test_loadaware_routing.py`; suite now 50, still no
  cluster/GPU/install). `tests/conftest.py` grew a second loader that stubs the `vllm_router`,
  `requests`, `fastapi` and `uhashring` import surface and loads the tracked patch file itself.
  Covers the α/β crossover, ties, cold start, the fallbacks, and a regression test that
  `kvaware` still pins to the loaded cache holder.

### Decided
- **Cache-hit benefit is normalized to the fraction of the prompt cached**, not the raw
  matched-token count the handoff brief sketched. With raw counts the meaningful α:β ratio is
  ~1:1000 *and* shifts with prompt length, so one (α, β) pair would be a different policy for a
  500- and a 4000-token prompt — unusable for the §5 sweep. Normalized, `1/β` reads directly as
  "in-flight requests that cancel a full cache hit". Evidence:
  `test_benefit_is_normalized_so_the_weights_are_prompt_length_invariant`.
- **`loadaware` does not apply `kv_aware_threshold`.** Upstream needs that band because kvaware
  cannot weigh a small match against anything; the argmax can. Keeping it would also route
  every sub-threshold prompt by QPS in *both* arms, making that slice of the workload an
  identical no-op comparison. `kvaware` keeps the band (baseline unchanged). Evidence:
  `test_short_prompts_are_placed_not_dropped_to_the_qps_fallback`.
- **α/β travel by environment variable, not a CLI flag.** The parser lives in
  `parsers/parser.py` and is consumed in `app.py`; a flag would make this a three-file patch to
  mount and keep in sync with upstream. The factory still forwards `loadaware_alpha`/
  `loadaware_beta` kwargs, so adding a flag later touches no code here.
- **Not applied to the cluster in this session.** With issue #13 open (a router restart empties
  the KV registry and engines never re-admit), a live `loadaware` run would see `layout_info={}`
  and degenerate to the fallback — the ~7 min engine restart buys nothing until #13 lands.
  Offline tests + PR is the whole of #5.

## 2026-08-01 (implementation) — Change 1 landed: multi-instance lookup (issue #4)

### Added
- **`patches/` — tracked copies of the router-image Python files we modify**, mirroring their
  path under `/opt/venv/lib/python3.12/site-packages/`. The dev loop and the future §6 image
  apply the *same* bytes. Conventions in `patches/README.md`.
- **Multi-instance lookup** in `patches/lmcache/v1/cache_controller/controllers/kv_controller.py`:
  `lookup()` now credits **every** instance holding each chunk instead of `kv_pool[key][0]`,
  so `layout_info` reports per-instance matched-token counts. Wire-compatible — `LookupRetMsg`
  was already `{instance_id: (location, matched_tokens)}`.
- **`tests/` — 18 offline unit tests** (`pytest tests/`, no cluster/GPU/lmcache install).
  `tests/conftest.py` stubs the `lmcache` import surface and loads the *tracked patch file
  itself* by path, so the bytes under test are the bytes that get mounted. Includes a verbatim
  reference implementation of the stock lookup for the regression assertions.

### Decided
- **Prefix credit is contiguous per instance.** An instance stops earning matched tokens at its
  first missing chunk even if it holds later ones — a cache match is a prefix match, so tokens
  after a hole are unusable. The upstream global `break` is subsumed: the walk ends when no
  instance is still contiguous. Evidence: `tests/test_kv_controller_lookup.py`
  (`test_gap_stops_credit_at_the_gap_not_after_it`). This is a real design decision and belongs
  in §5 of the report.
- **`kvaware` is *not* behaviourally invariant under this patch**, even though
  `routing_logic.py` is untouched. The *instance* it selects is unchanged — both
  implementations insert `kv_pool[key0][0]` first and Python keeps a key's original position
  on re-assignment — but that instance's **`matched_tokens` can grow**, because an instance is
  now credited on every chunk it holds rather than only on chunks where it happens to be `[0]`.
  kvaware bands `matched_tokens` against `kv_aware_threshold` (`routing_logic.py:354-369`) to
  choose the cache path over the QPS fallback, so a larger count can flip that branch.
  **The baseline arm must be measured with `revert-router-patch.sh` applied**, never with
  Change 1 mounted. Evidence: `test_selected_instance_is_unchanged_even_with_several_holders`
  and `test_matched_tokens_of_the_selected_instance_can_grow`.

### Fixed
- **`deploy/dev/apply-router-patch.sh` was unrunnable on macOS** — `declare -A` needs bash 4 and
  macOS ships bash 3.2, which mis-parsed the subscripts as arithmetic and killed the script.
  Replaced the associative array with a `patch_target()` `case`.

### Verified live (cluster `gapu-2`, patch mounted, both engines)
- `[LOADAWARE] lookup matched 2 instance(s): {'…-pm79x': ('LocalCPUBackend', 5691),
  '…-x9dkx': ('LocalCPUBackend', 5691)}` — two instances reported for one prefix, which stock
  lookup structurally cannot do. Recipe: two *concurrent* cold requests on a fresh >2000-token
  prefix split across both engines (QPS fallback), then a third request to observe the lookup.

### Found — blocks evaluation (new issue #13)
- **The controller's `kv_pool` does not survive a router restart, and the engines do not
  repopulate it**: across three router pods the engines stored new chunks four times
  (`Storing KV cache for 2048 …`, local hit rate fine) while the controller stayed at
  `pool_size=0` and logged zero admits. Only an **engine restart** brought admits back.
  Consequence: the "router-only restart self-heals" claim in `deploy/dev/README.md` is wrong
  for the KV registry — worker *re-registration* self-heals, KV *admission* does not, and
  re-registration is what the previous session checked. Every patch iteration and every
  baseline/measurement run therefore needs an engine restart plus a fresh warm-up, which
  changes the dev loop from ~60 s to ~7 min. Mechanism not yet isolated.

## 2026-08-01 (docs consolidation) - Ticket #12 resolved

### Changed
- Deleted superseded exploration docs: `caching-landscape.md`, `router-optimization-ideas.md`,
  `handoff-second-optimization.md` (git history keeps them). Remaining five docs carry
  `status: live | frozen` headers; `handoff-core-implementation.md` header-only (in use by #4).

### Decided
- **Docs discipline (issue #12)**: decisions land in tickets + CHANGELOG only; `docs/` holds
  artifacts, never rationale; `docs/decisions/` closed to new entries. Recorded in CLAUDE.md.

## 2026-08-01 (later still) - Ticket #2 resolved: repo cleanup + CI skeleton

### Added
- Root `README.md` (project summary, repo layout, setup/test instructions),
  `requirements.txt` (httpx + pytest), and `.github/workflows/ci.yml` running
  `pytest benchmarks/` on every push and PR.

### Changed
- Pruned for the submission repo: personal hyperresearch skills moved out of
  `.claude/skills/` (now user-level on Ben's machine), course lecture PDFs and the
  scheduling docx removed from `docs/references/` (git history keeps them). Only the
  authoritative `Final Project Guidelines.pdf` remains.

## 2026-08-01 (later) - Wayfinder map: e2e plan on GitHub Issues

### Decided
- **The e2e plan now lives on GitHub Issues** - map issue #1 (label `wayfinder:map`),
  9 sub-issue tickets (#2-#10) with native blocked-by edges. Frontier (start now, in
  parallel): #2 repo cleanup + CI, #3 benchmark methodology, #4 Change 1 lookup.
- **All four 2026-08-01 reconciliation items settled (Ben, at map charting; recorded in
  issue #1 "Decisions so far"):**
  1. Upstream PR target = production-stack, not LMCache v1 (newer re-verification
     evidence wins; LMCache#4025 deprecation).
  2. PR breadth = two PRs only (#10); extra fixes become upstream *issues*, not PRs.
  3. The map is the final implementation plan - coding starts immediately off
     `docs/handoff-core-implementation.md`, no further planning session.
  4. Second optimization = nothing; kvaware fast path (F) only becomes a ticket if
     evaluation is running by day 5.
- **Benchmark plan remains deliberately undecided** - now ticket #3 (grilling), day-3
  latest; it gates evaluation (#7) via the harness (#6).

## 2026-08-01 (end of session) — Deadline known: scope cut to core-only

### Decided
- **Submission deadline is ~2026-08-10** (9 days, stated by Eliad 2026-08-01).
- **Adaptive β is OUT; flipped to runner-up E (core-only + evaluation + upstream PRs).**
  This executes the flip condition `docs/decisions/second-optimization.md` pre-registered —
  "core not landed with most of the schedule still ahead" — which triggered because the
  project was idle 2026-07-05 → 08-01 and no implementation code exists on day 1 of 9.
  Rubric backs it: correctness 40 + reproducibility 30 = 70% vs performance gain 15%.
  What survives of adaptive β is the α/β sensitivity sweep §5 requires anyway.
- Remaining scope, in order: multi-instance lookup → `loadaware` (static α/β) → benchmark
  harness → evaluation → report. Upstream PRs are opportunistic only.

### Changed
- `docs/decisions/second-optimization.md` marked SUPERSEDED with the trigger recorded; its
  load-signal-staleness analysis stays load-bearing for the report's motivation.
- `docs/handoff-core-implementation.md` — added the deadline, a day-by-day schedule with no
  slack, a cut-scope trigger for day 3, and a hardened definition of done.

## 2026-08-01 (end of session) — Implementation handoff

### Added
- `docs/handoff-core-implementation.md` — self-contained brief for the next session to start
  writing the core feature: exact file paths, line numbers and verbatim current code for both
  changes (read live out of the running router pod, not from upstream HEAD), the dev loop, the
  available routing signals, the offline test plan, and the measurement trap in §5.

### Next session starts here
Implement Change 1 (multi-instance `lookup()`), observe two instances in a single
`layout_info`, keep `kvaware` byte-identical as the baseline arm.

## 2026-08-01 (later) — Dev loop solved; two doc corrections

### Added
- `deploy/dev/` — in-cluster dev loop for router/LMCache code: ConfigMap + `subPath`
  overlay onto the running router pod, no image build and no container runtime required.
  `apply-router-patch.sh` / `revert-router-patch.sh` + README. **Validated end-to-end**:
  a marked-up `routing_logic.py` was mounted, confirmed live (log attributed to
  `routing_logic.py:393`), then cleanly reverted to stock.

### Fixed
- **`deploy/README.md` gotcha #1 was wrong** — "router restart ⇒ engine restart" does not
  hold in our configuration. The controller re-registers unknown workers on heartbeat
  (`registration_controller.py:176-192`), gated on `lmcache_worker_heartbeat_time > 0`,
  which `values-baseline-kvaware.yaml:58` sets to 30. Verified live: router-only restart,
  both workers back in ~30 s, engines untouched. **Dev loop is ~60 s, not ~25 min.**
- **`docs/upstream-findings.md` Finding 1 corrected** — "workers never re-register" is
  false; do not file it. Reframed to the real defect: `workerHeartbeatTime` is not a chart
  default, so stock deployments silently degrade kvaware on router restart. That is a
  one-line chart fix and a much easier merge.

### Changed
- **Lookup extension is far smaller than the July design assumed.** In the deployed
  lmcache 0.3.9post2, `kv_controller.lookup()` already returns
  `layout_info: Dict[instance_id → (location, matched_tokens)]` — the wire format already
  expresses per-instance match info. The defect is a single `[0]`: `self.kv_pool[key][0]`
  credits only the *first* holder of each chunk, discarding the rest. So the change is
  ~10 lines with **no protocol/message-schema change**, and the router-side counterpart is
  replacing `list(layout_info.keys())[0]` (`routing_logic.py:349`) with the α/β argmax.
- Recorded design consequence: under pure `kvaware` a prefix is rarely held by more than
  one instance, so the lookup fix is a near-no-op in isolation — it only pays once routing
  spreads requests. Rungs 2 and 3 of the ablation ladder are co-dependent and the workload
  must be designed so replication actually occurs. First live evidence:
  `layout_info={'…cc926': ('LocalCPUBackend', 2048)}` — one holder, 2048 matched tokens.

## 2026-08-01 — Restart after 4-week pause; upstream re-verification

### Decided
- **Sequencing: core first, second optimization deferred.** Build and validate the core
  (multi-instance lookup extension + `loadaware` static β) before committing to adaptive β.
  Adaptive β remains the intended rung 4 but is no longer a precondition for a complete
  project — it collapses to the α/β sensitivity sweep §5 requires anyway (Eliad).
- **Upstream-PR target moves off LMCache v1 `cache_controller`.** LMCache Q3 roadmap
  (LMCache#4025, opened 2026-07-06) begins deprecating non-MP mode this quarter, so a
  lookup-extension PR into v1 is unlikely to be accepted. The PR portfolio retargets to
  production-stack (router Service ports 9001/9002, one-shot registration, image matrix)
  plus production-stack#1016. Evidence: upstream re-verification, this session.
- **Contribution reframed:** the novelty is the *placement policy*, not the lookup itself —
  LMCache#4275 (merged 2026-07-28) added a fleet-wide key directory with per-instance
  placement lists in the new `mp_coordinator`. Still novel for the path production-stack
  actually uses; cite #4275/#4226 as concurrent related work.

### Changed
- Report must cite the **configured** `--engine-stats-interval 5` (deployed value), not the
  15 s chart / 30 s CLI defaults, when arguing load-signal staleness for adaptive β.

### Fixed
- Baseline restored: engines were externally scaled to 0 for ~11 days and one A10 was held
  by another namespace. Both GPUs reclaimed, `stack-llm-deployment-vllm` back to 2 replicas,
  both workers re-registered with the controller, end-to-end completion verified via the
  public Route (needs `curl -k` — self-signed cert in the ingress chain).

### Verified (upstream, still true at 2026-08-01)
- LMCache v1 `lookup()` is still first-instance-only (`kv_controller.py:380-387` TODO;
  `utils.py:580` `find_kv()` returns on first hit) and **unclaimed by any PR**;
  production-stack's `KvawareRouter` still takes `[0]` from `layout_info`.
- No `loadaware`/priority/hybrid strategy in production-stack's `RoutingLogic`;
  router-queuing PRs #876/#905 still open, nothing merged.
- Pinned image tags `lmstack-router:0.1.9.dev9-g37bafbcf5.d20260107` and
  `vllm-openai:v0.3.9post2` both still active on Docker Hub.
- Correction to the 2026-07-05 memo: the `:402` TODO is on `batched_p2p_lookup`; the one
  that matters is the block at lines 380-387 above `async def lookup()`.
## 2026-08-01 (evening, parallel planning session) — Scope lock: PRs and load signal

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

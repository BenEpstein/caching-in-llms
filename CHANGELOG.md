# Changelog

All notable changes to this project are documented here, newest first.
Format adapted from [Keep a Changelog](https://keepachangelog.com/en/1.1.0/): one entry per
work session (or significant commit), with **Added / Changed / Decided / Fixed** subsections as
applicable. Since this is a research project, **Decided** captures project-direction decisions
with a pointer to the evidence — those matter as much as code.

## [Unreleased]

## 2026-08-03 - Ticket #21 root-caused: both router-stability issues explained

### Fixed
- `load_driver.py` now reads the response body on a failed request before raising. Streamed
  responses arrive body-unread, so `raise_for_status()` was discarding the server's error
  detail - all six demo CSVs recorded a bare "500 Internal Server Error" with no cause. The
  error column is widened 200 -> 500 chars to fit a traceback summary.

### Decided
- **Issue 1 (SIGKILL at 4 req/s) is a liveness starvation caused by a blocking HF call on the
  event loop, and it is baseline-inherent - it caps the pilot rate, it does not invalidate the
  comparison.** Evidence, all on the live `:c68ccfc` deploy: the router pod cannot write an HF
  cache (runs as uid 1001020000, `HF_HOME` unset, no writable `/.cache`), so
  `AutoTokenizer.from_pretrained` fails on **every** request - the exception path never sets
  `self.tokenizer`, so it is retried per request, and the failing call costs **245 ms median**
  because it reaches huggingface.co over the network before failing. Measured in-router
  blocking is **0.248 s/request mean** (n=644, p95 0.282 s), giving a single-event-loop ceiling
  of **4.04 req/s** - the router was killed at exactly 4 req/s. The kill is the liveness probe,
  not memory: `reason: "Error"` (not `OOMKilled`) with `timeoutSeconds: 1`, period 5s,
  threshold 3. `KvawareRouter.route_request` contains the identical try/except, so the stock
  baseline arm pays the same cost.
- **Issue 2 (background 500s) is not ours: 16/16 tracebacks are
  `aiohttp.client_exceptions.ServerDisconnectedError`** raised in upstream
  `request.py:164` while opening the upstream connection, i.e. after the routing decision had
  already succeeded. The router's aiohttp pool holds idle engine connections **15.0 s**
  (`TCPConnector(limit=0)`, aiohttp default) while the engine closes them at **5 s**
  (vLLM `VLLM_HTTP_TIMEOUT_KEEP_ALIVE=5`, not overridden). Arm-independent: the code path is
  shared by every routing logic.
- **That timeout mismatch alone is NOT sufficient, and the leading hypothesis now links the two
  issues.** A controlled sequential probe (90 requests, idle gaps 1.0/2.0/2.5/3.0/4.0/5.0 s,
  n=15 each) returned **90/90 HTTP 200** - with one connection in play aiohttp processes the
  engine's FIN and discards the dead connection cleanly. The demo 500s occurred only under
  concurrency, and the suspected mechanism is that the 0.25 s/request event-loop block above
  *delays the router's processing of the engine's FIN*, so closed connections linger in the
  pool as reusable and get handed out. **That experiment has now run and both issues cleared
  on the single fix** (below), which is the intervention evidence for the link.

### Fixed
- **`HF_HOME=/tmp/hf`, applied to BOTH arms via `benchmarks/run_cell.sh`, clears both issues.**
  It cannot live in values: chart 0.1.11's `deployment-router.yaml` hardcodes the router env
  list and declares no volumes at all (verified against the pulled chart, 2026-08-03), so there
  is neither a `routerSpec.env` passthrough nor a volume hook - and `/tmp` is the only writable
  path in the container. Set on baseline cells too; an arm-only fix would flatter loadaware.
  Verified live after rollout:
  - in-router blocking **221 ms -> 1 ms median**; first request costs 3.50 s once (the tokenizer
    load), which the existing warm-up gate already absorbs
  - remote `/tokenize` calls **933/7 -> 0/0** across the two engines
  - 2 req/s x 3 seeds x 100: **300/300 OK, 0 tracebacks** (pre-fix 16/600 = 2.7%; under a 2.7%
    rate, 0-in-300 has p ~ 3e-4)
  - the 4 req/s repro that previously SIGKILLed the router: **100/100 OK, 0 restarts**
  Caveat: this is a before/after on a shared cluster, not a controlled A/B - the workloads are
  the frozen seeds 101-103 in both cases, but engine cache state differed.

### Changed
- Confounder found while root-causing: because local tokenization always fails, every request
  falls back to a synchronous `requests.post(.../tokenize)` aimed at `endpoints[0]` - **933
  tokenize calls landed on engine-0 vs 7 on engine-1** over the demo window. That is an
  unmeasured load asymmetry pointed at one engine, which biases a *load-aware* routing
  experiment specifically. Must be fixed before #7, identically in both arms.
- Verified the live router overlay (`router-patch` configmap: `routing_logic.py`,
  `kv_controller.py`, `parser.py`) is **byte-identical** to the branch, so the demo runs did
  exercise the intended code. Corrects the handoff's "NO dev overlay mounted" note.

## 2026-08-01 (late night) - Ticket #6 completed: benchmark harness built

### Added
- Benchmark harness implementing the locked methodology (issue #3): `freeze_workloads.py`
  (6 frozen seeds pinned by SHA-256 manifest), `warmup.py`, `collectors/prom_dump.py` +
  `collectors/dcgm_poll.py`, `run_cell.sh` per-cell choreography (deploy → #13 gates →
  warm-up → 6 seeds → collect → `run.json`), `run_sweep.sh`, `rate_pilot.sh`, and
  `analyze.py` with the pre-registered exact one-sided Wilcoxon + bootstrap CI
  (stdlib-only). 30 new unit tests; `benchmarks/README.md` pre-registers the validity rules.

### Changed
- `workload_gen.py`: `pool_seed` split from `seed` so all 6 replay seeds share ONE frozen
  prefix pool (a single warm-up must cover every seed). `load_driver.py`: `send_ts` is now
  wall-clock epoch (aligns rows with Prometheus/DCGM windows), optional `--summary-json`,
  and `--insecure` opt-in TLS flag (gapu-2 self-signed route) instead of implicit trust.

### Changed
- **Review + simplify pass over PR #20** (/code-review + /simplify, 4 findings applied,
  ~90 lines net removed): `percentile` now lives only in `analyze.py` (driver imports it);
  tautological Wilcoxon brute-force test replaced with a hand-computed midrank-ties case;
  driver `--summary-json` dropped (windows derive from CSV `send_ts`); unused
  `prom_dump --metric` dropped; redundant bootstrap sorts removed; DCGM poll sleeps to a
  deadline. Correctness from review: driver pins OSL via `ignore_eos` (early EOS was
  leaking output-length variance into E2E/throughput), unbounded httpx connection pool
  (pool-queueing would count as TTFT past the knee), per-chunk JSON parse removed from the
  measurement path, `--since` log windows floored at the gate timestamp (probe traffic
  could satisfy the warm-up gate), router image asserted per cell and
  `analyze.py compare` refuses runs with differing rate/workload manifests
  (validity rules now enforced, not recorded).

### Fixed
- **Live verification on gapu-2 falsified four harness assumptions** (issue #6, PR #20):
  (1) chart 0.1.11 silently ignores `routerSpec.env` - α/β now travel via `oc set env`
  per cell, removed on baseline cells so a stale β can't leak through the three-way merge;
  (2) the pinned router exposes NO `registered_workers_count` gauge - registration gate is
  now the router's `Registered instance-worker` log lines (deploy/README diagnostic
  corrected too); (3) `lmcache:request_cache_hit_rate` is a histogram, prom_dump now pulls
  `_sum`/`_count`; (4) DCGM is a DaemonSet and a Service port-forward pins to ONE pod - now one port-forward per exporter pod, poller takes multiple `--url`s (verified rows
  from both workers). Also: `helm upgrade` now pins `--version 0.1.11` (0.1.12 has schema
  drift), and a mounted dev overlay (`router-patch` ConfigMap - found live!) is
  auto-reverted before measuring. Driver, warm-up gate line, registry probe, prom_dump,
  and DCGM poller all exercised against the real cluster.

### Decided
- **Workload JSONLs are not committed; the manifest is** (issue #6): 6×~6 MB of synthetic
  filler would bloat the submission repo, and generation is deterministic - the committed
  `workloads/manifest.json` (config + SHA-256 per seed) plus a mandatory
  regenerate-and-verify in `run_cell.sh` gives the same frozen-dataset guarantee.
- **Registry probe skipped on the roundrobin cell** (issue #6): roundrobin routing ignores
  the KV registry, and the probe's pinning signal is meaningless there; the worker
  registration wait still applies. Documented in `benchmarks/README.md`.

## 2026-08-01 (night) - Ticket #16 completed: image pipeline pushes to Quay

### Fixed
- **Dockerfile was stale vs Change 2:** only `patches/lmcache` was copied into the image, so
  the "§6 deliverable" would have run stock kvaware routing. Now also copies
  `patches/vllm_router` (loadaware policy + CLI widening) and the CI verify step greps all
  three patched files for the `LOADAWARE PATCH` marker before pushing.

### Changed
- `deploy/values-loadaware-image.yaml`: `CHANGEME` → `quay.io/rhl193000/lmstack-router-loadaware`
  (public Quay repo, robot-account push from CI); `routingLogic: kvaware` → `loadaware` now
  that Change 2 is landed.

### Decided
- **Quay repo is public** (issue #16): cluster pulls with no imagePullSecret and a grader can
  pull the exact SHA-tagged image cited in the report. Credentials live only in GitHub Actions
  secrets (`QUAY_USERNAME`/`QUAY_TOKEN`, robot account scoped to this one repo).

### Added
- **Image path verified end to end on gapu-2** (closes #16): first CI push
  (`:c68ccfc`, digest `sha256:ae5772fe…`) deployed via
  `helm upgrade --reuse-values -f deploy/values-loadaware-image.yaml` (rev 13); router pod
  pulled the exact digest from Quay, booted `loadaware` (α=1.0, β=0.1 defaults), and after
  the expected #13 blind window the registry probe pinned 4/4 - prefix affinity confirmed on
  the built image, dev-loop overlay uninvolved. #7 unblocked.

## 2026-08-01 (evening) - Ticket #3 resolved: benchmark methodology locked

### Decided
- **Benchmark methodology (issue #3, full detail in its resolution comment):** one frozen
  Zipfian prefix workload (s=1.2, 20 prefixes × 2048 tok, OSL 64, committed JSONL); merged
  6-cell sweep (loadaware β ∈ {0, 0.1, 0.5, 1.0} + kvaware + roundrobin) × 6 seeds × 500
  requests, full engine-restart choreography per cell (#13); headline pre-registered as
  shipped β=0.1 vs kvaware on client-observed TTFT p95, one-sided paired Wilcoxon p<0.05 +
  bootstrap CI; mechanism metric = per-instance load CV.
- **Measured runs use built images only** (stock image for baselines, SHA-tagged Quay image
  for loadaware; overlay never measured) - settles #16's open question and makes #16 a hard
  blocker for #7 (blocked-by edge wired). No mock/simulation anywhere.
- **Metric plumbing verified live on gapu-2:** driver CSV is the only percentile-capable
  latency source (router exposes averages only; engine TTFT histogram is coarse and starts
  at the engine); Prometheus dump per run for load/queue/LMCache-hit metrics; DCGM polled
  directly via port-forward (stack Prometheus does not scrape it; no ServiceMonitor added).

### Added
- `docs/handoffs/claude-wayfinder-3-e533a3.md` (session log + metric verification) and
  `docs/handoffs/claude-wayfinder-3-e533a3-decisions.md` (workload exploration, D1-D4).

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
- **`--routing-logic loadaware` accepted by the CLI**
  (`patches/vllm_router/parsers/parser.py`): the flag's `choices` are hard-coded literals, not
  derived from `RoutingLogic`, so the enum value alone would have been rejected by argparse and
  the router would have exited before the factory ran. One-line widening; `apply-router-patch.sh`
  learned the `parser.py` target. An AST-based test asserts `choices` and `RoutingLogic` stay in
  lockstep in both directions.
- **38 more offline unit tests** (`tests/test_loadaware_routing.py`; suite now 50, still no
  cluster/GPU/install; suite now 57). `tests/conftest.py` grew a second loader that stubs the `vllm_router`,
  `requests`, `fastapi` and `uhashring` import surface and loads the tracked patch file itself.
  Covers the α/β crossover, ties, cold start, the fallbacks, and a regression test that
  `kvaware` still pins to the loaded cache holder.

### Fixed
- **The instance_id → URL bridge is refreshed when it goes stale, not once.** Review caught two
  silent failures the first cut had: a count-of-entries guard never notices a restarted engine
  (it registers under a *fresh* instance_id while the bridge only ever grows), so every holder
  would read as unmapped and placement would degenerate to least-loaded for the life of the
  router — an invalidated evaluation run with nothing in the logs. And when two ids share a URL,
  only the **live** one may be credited: the Controller's `kv_pool` keeps the dead instance's
  chunks until an explicit deregister, but the restarted engine came back with an empty cache, so
  that match is phantom. Evidence: `test_an_engine_restart_refreshes_the_bridge_instead_of_scoring_it_cold`,
  `test_a_dead_instance_id_earns_no_phantom_credit`.
  Known residual window, documented in the docstring: the bridge only learns the fresh id once
  the restarted engine appears in a `layout_info`, so until its first admit the dead id's match
  still reads as credit. Closing it means an unconditional Controller round-trip per request on a
  path that already blocks the event loop (production-stack#1016) — so the operational answer
  stands: gate runs on `registry-probe.sh`, do not restart engines mid-run. `kvaware` has the
  same hole and routes purely on that credit; §5 material.

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
- **α/β travel by environment variable, not a CLI flag.** Registering a flag means the parser
  *and* `app.py` (which builds the `initialize_routing_logic` kwargs), i.e. one more file to
  mount and keep in sync than the one-line `choices` widening already forced. The factory still forwards `loadaware_alpha`/
  `loadaware_beta` kwargs, so adding a flag later touches no code here.
- **Not applied to the cluster in this session.** With issue #13 open (a router restart empties
  the KV registry and engines never re-admit), a live `loadaware` run would see `layout_info={}`
  and degenerate to the fallback — the ~7 min engine restart buys nothing until #13 lands.
  Offline tests + PR is the whole of #5.
## 2026-08-01 (§6 image path) — Ticket #16: build in CI, not on a laptop

### Added
- `Dockerfile` — the §6 reproducibility deliverable: the pinned stock router (**by digest**,
  not just tag — router and engines must carry the same lmcache minor or the controller
  protocol fails silently) plus a straight copy of `patches/`, which already mirrors the
  site-packages layout. No compilation, no CUDA, no weights.
- `.github/workflows/router-image.yml` — builds on every change to `patches/`/`Dockerfile`,
  **verifies the overlay actually landed** inside the built image (a silently-unpatched image
  would read as a failed experiment rather than a failed build), then pushes to Quay tagged
  by git short SHA. Without credentials configured it still builds and verifies, and skips
  only the push.
- `deploy/values-loadaware-image.yaml` — Helm overlay pointing `routerSpec` at the built
  image, layered on top of the baseline values.

### Decided
- **The image is built in CI, not locally.** The handoff assumed a local build, which needs a
  container runtime on a laptop (none installed) and a manual registry login. CI has Docker,
  keeps the credential in repo secrets, and — the part that earns the 30% — means a grader
  reproduces the image by pushing a commit rather than trusting an artifact one person built
  by hand.
- **Tag by commit SHA, never `latest`.** A floating tag makes the router/engine lmcache
  pairing impossible to audit after the fact; the report cites the exact tag its numbers
  came from.
- **Credentials are a Quay robot-account token in GitHub Actions secrets**, never a user
  password, and the image repository should be public so the cluster needs no pull secret.

### Not done — needs a repo admin
Quay robot account + `QUAY_USERNAME`/`QUAY_TOKEN` secrets + `QUAY_IMAGE` variable, then a
first push and a pull test from the cluster. The build path is otherwise complete and is
exercised by CI on every push.

## 2026-08-01 (investigation) — Ticket #13 resolved: the KV registry's blind window

### Found — three facts that compose into a silent measurement trap
- The Controller's `kv_pool` is **in-memory**: a router restart empties it.
- Admission is **one-shot per chunk** — `LocalCPUBackend.submit_put_task` returns early on
  `if key in self.hot_cache`, so a chunk is announced exactly once, at first store. Nothing
  already cached is ever re-announced. (`RegisterMsg`, `HeartbeatMsg` and `KVAdmitMsg` all
  share one PUSH socket, so this is not a dead-socket problem — re-registration recovers
  because heartbeats *repeat*; admission never does.)
- Admits are lost for **~40 s** after a router restart, until both workers re-register
  (10 s heartbeat delay + 30 s interval).
- **Composed:** a prefix first stored inside that window is invisible to the Controller for
  the life of the engine process, while the engine still serves it from its own cache
  perfectly. Both arms of an experiment then degrade to QPS routing and look identical for
  the wrong reason.

### Measured (fresh prefix per probe, from rollout completion)
```
t+4s … t+30s   requests spread 2/2   → registry empty
t+42s          requests pinned 4/4   → registry live (both workers re-registered)
```

### Added
- `deploy/dev/registry-probe.sh` — no-patch health check for the registry. Sends the same
  >2000-token prefix N times: `kvaware` pins all N to one Instance when the registry is
  populated and spreads them when it is empty. Exit 0/1, so it gates a run.

### Decided
- **Every measurement is gated on `registry-probe.sh` with an unused seed**, run after each
  `apply-router-patch.sh` and each `revert-router-patch.sh`, before warm-up. Poisoned
  prefixes are never reused.
- **No engine restart is required** — this walks back the same-day claim that it was. Waiting
  ~40 s for re-registration is enough, so the dev loop stays ~60 s + the probe.
- **Upstream:** the principled fix is for a worker to re-announce its `hot_cache` on
  (re-)registration, making admission self-healing like registration. That is LMCache
  `v1/cache_controller` code, which is being deprecated (LMCache#4025) and is off our PR
  target — so it belongs as an upstream **issue**, per the two-PRs-only rule in #10.

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

### Found — blocks evaluation (issue #13, since resolved — see the entry above)
- **A router restart leaves the Controller's `kv_pool` empty and every `lookup()` returning
  `{}`**, with nothing in the logs saying so. Cost most of this session's live-verification
  time. Investigated and characterised in #13.

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

# Handoff: implement the core (multi-instance lookup + `loadaware` placement)

> Written 2026-08-01. **This brief is self-contained** — a fresh Claude Code session should
> be able to start writing code from this file alone, without re-reading the July design docs.
> Every code fact below was read out of the *running* router pod on cluster `gapu-2` on
> 2026-08-01, not from upstream HEAD. Trust these over the July documents where they differ.
>
> **Expected outcome of the next session:** rung 2 of the ablation ladder landed and observed
> live — `layout_info` reporting *all* instances that hold a prefix, not just the first.

## ⏱ DEADLINE — 9 days from 2026-08-01, i.e. **~2026-08-10** (confirm exact date with Eliad)

This dominates every other consideration in this document. Consequences, decided 2026-08-01:

- **Adaptive β is OUT.** `docs/decisions/second-optimization.md` pre-registered the flip to
  runner-up E ("core-only, invest in evaluation + upstream PRs") *if the core is not landed
  with most of the schedule still ahead*. Zero implementation code exists on day 1 of 9, so
  the condition has triggered. Do not start a `BetaPolicy`. The α/β **sensitivity sweep** §5
  requires anyway is what remains of it, and it is enough.
- **Rubric math backs this:** correctness 40 + reproducibility 30 = **70%**; performance gain
  is only 15%. A second optimization buys at most a slice of 15 while putting the 70 at risk.
  A modest, rock-solid, reproducible gain wins.
- **Scope is now:** multi-instance lookup → `loadaware` (static α/β) → benchmark harness →
  evaluation → report. Upstream PRs are opportunistic, only from work already done.

### Indicative schedule (no slack — treat slippage as a scope signal, not a reason to work later)

| Day | Work |
|---|---|
| 1-2 | Change 1 (lookup) + Change 2 (`loadaware`) + unit tests, via the dev loop |
| 3 | Benchmark harness: hit rate, p95/p99, throughput to CSV. **Solve the §6 image path today** |
| 4-5 | Evaluation runs: Zipf-s × QPS grid, kvaware vs loadaware, α/β sweep. Mostly machine time |
| 6-7 | Report (8-12 pp) + plots |
| 8 | Buffer, repo hygiene, reproducibility check from a clean clone, upstream PRs if time |
| 9 | Submit |

**The §6 image path is a day-3 item, not a day-8 item.** Reproducibility is 30% of the grade
and the image is its load-bearing artifact; see "Open, not yet solved" below. Eliad deferred
the decision to the next session — raise it early, do not let it drift.

> Note on notation: `§2`…`§6` in this document always mean sections of the **assignment PDF**
> (§4 Extension, §5 Evaluation, §6 Report), never sections of this file.

**If day 3 arrives without Change 1 + Change 2 landed**, cut to: `loadaware` measured against
`kvaware` at a single well-chosen α/β, no sweep, and spend the recovered time on the report.
Correctness and clarity of a small claim beat an unfinished big one.

## 0. Read first, in this order

1. This file.
2. `deploy/dev/README.md` — the dev loop. You will use it every iteration.
3. `CONTEXT.md` — glossary. Use these words exactly (Instance, Controller, Placement Policy…).
4. `CHANGELOG.md` top two entries — what the 2026-08-01 sessions established.

Skip `docs/handoff-second-optimization.md` (its job is done — the decision it asked for is in
`docs/decisions/second-optimization.md`) unless you need the candidate-menu rationale.

## 1. Locked — do not re-litigate

- **Contribution:** a `loadaware` placement policy scoring `α·cache_hit_benefit − β·load_penalty`,
  plus the multi-instance lookup it requires. Router-image-only; engines stay stock and pinned.
- **Framing:** *"KV-cache-aware request placement"*. Never headline it "load balancing".
  Hit rate stays a first-class metric.
- **Novelty is the placement policy, NOT the lookup.** LMCache PR #4275 (merged 2026-07-28)
  added a fleet-wide key directory with per-instance placements in the new `mp_coordinator`.
  Cite it as concurrent related work; our work is still novel for the path production-stack
  actually uses (`v1/cache_controller`).
- **Second optimization (adaptive β) is OUT** — flipped to runner-up E on 2026-08-01 under the
  condition pre-registered in `docs/decisions/second-optimization.md` (see the deadline section
  above). The static-β core is the whole project; it is complete and defensible on its own.
- **Upstream PRs do not target LMCache `v1/cache_controller`** — it is being deprecated
  (LMCache#4025, Q3). Target production-stack instead.

## 2. Environment (verified working 2026-08-01)

- Cluster OpenShift `gapu-2`, namespace `cache-llm`, **both A10 GPUs are ours** until the
  project ends. VPN required. Login token expires — ask Eliad for a fresh `oc login`.
- Running: router (1) + engines (2, one per GPU) + Prometheus + Grafana. Baseline is stock,
  no patches mounted.
- Pinned pairing — **do not change**: router `lmstack-router:0.1.9.dev9-g37bafbcf5.d20260107`
  + engines `vllm-openai:v0.3.9post2` (both lmcache 0.3.9post2). Version skew fails *silently*.
- Model: `Qwen/Qwen2.5-3B-Instruct` (ungated, no HF token). Do not change it.
- Query it: `curl -k https://llm-cache-llm.apps.gapu-2.customers.k8s.co.il/v1/completions …`
  — **`-k` is required**, the ingress cert chain is self-signed.
- Router API is on pod port **8000**. Ports 9000/9001/9002 are the LMCache controller
  (pull/reply/heartbeat).
- Registration evidence is the router **log** line `Registered instance-worker`. There is no
  `registered_workers_count` metric in this build — don't go looking for it.

### The dev loop (this is the important part)

Router code is plain Python in the pod at `/opt/venv/lib/python3.12/site-packages/`.
`deploy/dev/apply-router-patch.sh` mounts your edited files over the installed ones via a
ConfigMap `subPath`. **No image build, no container runtime, no engine restart. ~60 s.**

```bash
cd deploy/dev
mkdir -p work
oc exec -n cache-llm deploy/stack-deployment-router -- \
  cat /opt/venv/lib/python3.12/site-packages/lmcache/v1/cache_controller/controllers/kv_controller.py \
  > work/kv_controller.py
# edit work/kv_controller.py
./apply-router-patch.sh work/kv_controller.py
oc logs -f deploy/stack-deployment-router -n cache-llm | grep -iE "layout_info|kvaware|loadaware"
```

A router-only restart self-heals: workers re-register from their 30 s heartbeat, engines are
never touched. (The old `deploy/README.md` rule "router restart ⇒ engine restart" was **wrong**
and has been corrected.)

> ⚠️ **`./revert-router-patch.sh` before ANY measurement.** The overlay is invisible to
> `helm list` and survives restarts. A baseline number measured with a patch mounted is invalid.

## 3. Change 1 — multi-instance lookup (rung 2)

**File in pod:** `/opt/venv/lib/python3.12/site-packages/lmcache/v1/cache_controller/controllers/kv_controller.py`
**Current code, lines 158-170** (lmcache 0.3.9post2, verbatim):

```python
async def lookup(self, msg: LookupMsg) -> LookupRetMsg:
    tokens = msg.tokens
    layout_info = {}
    for start, end, key in self.token_database.process_tokens(
        tokens, make_key=False
    ):
        if key not in self.kv_pool:
            break
        matched_instance = self.kv_pool[key][0].instance_id
        matched_location = self.kv_pool[key][0].location
        layout_info[matched_instance] = (matched_location, end)
    return LookupRetMsg(layout_info=layout_info, event_id=msg.event_id)
```

**The whole defect is `[0]`.** `self.kv_pool[key]` is a *list* of every instance holding that
chunk; the code credits only the first and discards the rest. Above it sits the upstream TODO
that acknowledges this (*"does not consider the location of the kv chunks. It simply returns
the `instance_id` with longest prefix"*).

**What to do:** iterate all holders of each chunk and accumulate per-instance matched-token
counts. Note `end` is the running token offset, so the last write per instance is its longest
matched prefix — preserve that semantic.

**No protocol change is needed.** `LookupRetMsg.layout_info` is already
`Dict[str, Tuple[str, int]]` = `{instance_id: (location, matched_tokens)}` (`message.py:470`).
Populating it for every holder rather than the first is wire-compatible.

**Subtlety to decide and document:** the loop `break`s at the first chunk missing from
`kv_pool`, i.e. it walks the *common* prefix. With multiple holders, different instances may
diverge at different depths. Decide whether a chunk missing on instance A but present on B
should stop the walk (current: yes, globally) or be tracked per instance. Per-instance is the
honest reading of "how many tokens does each instance actually hold" — and is what the
placement score needs. Write the reasoning into the report; it is a real design decision, not
an implementation detail.

## 4. Change 2 — the `loadaware` router (rung 3)

**File in pod:** `/opt/venv/lib/python3.12/site-packages/vllm_router/routers/routing_logic.py`
(584 lines). Three touch points:

**(a) Enum, line 52:**
```python
class RoutingLogic(str, enum.Enum):
    ROUND_ROBIN = "roundrobin"
    SESSION_BASED = "session"
    KVAWARE = "kvaware"
    PREFIXAWARE = "prefixaware"
    DISAGGREGATED_PREFILL = "disaggregated_prefill"
    # add: LOADAWARE = "loadaware"
```

**(b) Factory, `initialize_routing_logic()` line 519** — add a `LOADAWARE` branch. Mirror the
`KVAWARE` branch: it constructs the router then calls `router.start_kv_manager()`.

**(c) The decision itself.** `KvawareRouter` is at line 236; the routing decision is in
`route_request()` lines 297-393. The line to replace, at 348-352:

```python
if len(list(instance_id.layout_info.keys())) > 0:
    matched_instance_id = list(instance_id.layout_info.keys())[
        0
    ]  # Get the first key
    matched_tokens = instance_id.layout_info[matched_instance_id][1]
```
and the return at 393: `return self.instance_id_to_ip[queried_instance_ids[0]]`.

Both `[0]`s become an argmax over `α·matched_tokens(i) − β·load(i)`.

**Recommended shape:** subclass or compose rather than editing `KvawareRouter` in place —
you need `kvaware` to stay byte-identical as the baseline arm of the experiment. A
`LoadAwareRouter` that reuses the lookup/tokenize machinery and overrides only the selection
step keeps the diff small and the comparison honest.

### Signals available at the decision point

`route_request(self, endpoints, engine_stats, request_stats, request, request_json)`:

- `request_stats: Dict[str, RequestStats]` — **the fresh, event-driven one. Use this.**
  Fields (`stats/request_stats.py`): `qps`, `ttft`, `in_prefill_requests`,
  `in_decoding_requests`, `finished_requests`, `uptime`, `avg_decoding_length`,
  `avg_latency`, `avg_itl`, `num_swapped_requests`.
  Load penalty ≈ `in_prefill_requests + in_decoding_requests`.
- `engine_stats: Dict[str, EngineStats]` — scraped, **stale**. Our deployment sets
  `--engine-stats-interval 5` (not the 15 s chart / 30 s CLI default — cite *5* in the report).
  Optional blend only; never the primary signal.

Keys of both dicts are engine URLs; `layout_info` is keyed by *instance_id*. The existing
`self.instance_id_to_ip` mapping (built lazily at lines 371-389) bridges them — you need that
bridge for scoring, so build it before scoring rather than after selection.

### Parameters

Expose `α` and `β` as tunable, documented parameters (§4 deliverable requires this).
`KvawareRouter.__init__` already takes `kv_aware_threshold: int = 2000` from
`kwargs.get("kv_aware_threshold")` — follow that pattern. Chart passthrough for extra router
flags exists (`routerSpec` "extra router commandline arguments").

## 5. The measurement trap — read before designing the experiment

First live routing decision captured 2026-08-01:

```
[PATCHED] kvaware chose ...cc926 | layout_info={'...cc926': ('LocalCPUBackend', 2048)}
```

**One holder.** Under pure `kvaware` a prefix always lands on the same instance, so it is
rarely replicated, so `kv_pool[key]` usually has exactly one entry — and the Change-1 fix
measures as a **no-op in isolation**. Rungs 2 and 3 are co-dependent.

Plan the workload so replication actually occurs, e.g.: warm both instances on the hot
prefixes first, or drive enough concurrency that `kvaware`'s own threshold fallback
(`matched_tokens < len(tokens) - threshold` → QPS routing, line 354-369) spreads them. State
the mechanism explicitly in §5 of the report — a grader will ask why rung 2 alone moves nothing.

Also note `kv_aware_threshold` defaults to **2000** tokens: short prompts never take the
kvaware path at all. Any workload prompt must be comfortably longer than one chunk (LMCache
chunks are 256 tokens); the smoke test used ~2000-token prompts and got a 2048-token match.

## 6. Tests (§4 requires unit tests; correctness is 40% of the grade)

Write these **offline, no cluster, no GPU** — that is the whole point of a pure-Python
router change:

- Lookup: synthetic `kv_pool` with 1 / 2 / N holders per chunk, divergent prefix depths,
  empty pool, single-chunk prompt. Assert per-instance matched-token counts.
- Placement: hand-built `layout_info` + `request_stats` pairs asserting the α/β crossover —
  warm-but-loaded vs cold-but-idle, ties, all-cold (must degrade to the existing fallback),
  single endpoint.
- Regression: `kvaware` selection is unchanged when only one instance holds the prefix.

`benchmarks/` already uses pytest (`test_workload_gen.py`); match its conventions. CI wiring
is still an open §3 deliverable.

## 7. Definition of done for the next session

With a 9-day budget the next session must clear **all** of this — it is day 1 of the
2-day implementation block, not an exploratory sitting:

1. `kv_controller.lookup()` returns per-instance match info; unit tests green.
2. Applied via `deploy/dev/apply-router-patch.sh` and observed live: a `layout_info` log
   showing **two** instances for a prefix both hold.
3. `LOADAWARE` enum + factory branch + the α/β argmax, with unit tests. `kvaware` left
   byte-identical as the baseline arm.
4. `CHANGELOG.md` updated (**mandatory** — see the discipline section in `CLAUDE.md`).
5. Reverted to stock, baseline confirmed serving.
6. The §6 image-path decision raised with Eliad (runtime + registry) — needed by day 3.

If (1) and (2) prove harder than expected, land them properly and push (3) to the next day
rather than half-landing both. Do not start anything not on this list.

## 8. Open, not yet solved — do not let these surprise you late

- **Image build path for §6 (reproducibility = 30%).** The dev loop is *not* the deliverable.
  A real image (`FROM lmcache/lmstack-router:0.1.9…` + `COPY` + `routerSpec.repository`/`tag`)
  needs a container runtime (none installed on Eliad's Mac — `brew install colima docker` is
  the likely route) and a registry the cluster can pull from (no exposed internal registry on
  `gapu-2`; Docker Hub or Quay account needed). **Solve this well before the deadline.**
- **Deadline is unknown.** Asked twice, never answered. It decides whether adaptive β happens
  at all. Ask Eliad in the first message.
- **production-stack #1016** — `KvawareRouter` blocks the event loop per request (sync
  tokenizer fetch + `/tokenize` POST + controller lookup), causing probe starvation and router
  CrashLoops *under load*. Our benchmarks drive load through exactly this path, so it may bite
  during evaluation. Also a cheap, well-scoped upstream PR candidate in a healthy repo.
- **CI** (§3 deliverable) is not wired up.

## 9. Collaboration

Shared repo with Ben Epstein. **`git pull --rebase` before starting and before pushing.**
Update `CHANGELOG.md` in the same commit as the work; `Decided` entries must point at evidence.

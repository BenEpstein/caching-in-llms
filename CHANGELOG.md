# Changelog

All notable changes to this project are documented here, newest first.
Format adapted from [Keep a Changelog](https://keepachangelog.com/en/1.1.0/): one entry per
work session (or significant commit), with **Added / Changed / Decided / Fixed** subsections as
applicable. Since this is a research project, **Decided** captures project-direction decisions
with a pointer to the evidence — those matter as much as code.

## [Unreleased]

## 2026-07-04 — Direction pivot, deep-research verdict, baseline decision, first benchmark code

### Decided
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
- **lmcache version skew router↔engine** (0.3.11 vs 0.3.9.post2) silently breaks the
  controller↔worker ZMQ protocol; engines moved to `vllm-openai:v0.5.1rc2` (Eliad).
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

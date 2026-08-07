# CLAUDE.md

> status: live · 2026-08-07 · standing instructions for agent sessions; keep it current, it is read
> at the start of every session

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A **BGU university final project**: take an existing open-source LLM caching library, design and
implement an **enhanced cache policy**, prove a **measurable, statistically significant performance
improvement** over the unmodified baseline, and write it up as a publishable report. The full spec
is `docs/references/Final Project Guidelines.pdf` — it is the single source of truth and overrides
assumptions made here.

## The rubric drives every priority

Grading weights, not engineering taste, decide what to work on first:

| Criterion | Weight | What it demands |
|---|---|---|
| **Correctness** | 40% | The cache (baseline + your policy) passes tests and behaves as specified |
| **Reproducibility** | 30% | Anyone can clone, set up, and rerun the benchmarks to get the same numbers |
| **Performance Gain** | 15% | Clear, *statistically significant* improvement on a chosen metric |
| **Clarity** | 15% | Documented code + a report that tells one coherent story |

Correctness + Reproducibility = **70%**: a modest, rock-solid, reproducible gain beats an
impressive-but-flaky one. Every claim in the report must be backed by a figure, table, or test the
reader can rerun.

**The 100-grade carrot (§4):** an exceptional grade goes to extensions merged upstream via PR.
Stay strictly compatible with the baseline's cache interface to keep that path open.

## Deliverables (PDF section → artifact)

- **§2 Baseline justification** — ≤1-page doc: chosen library, main features, default eviction policy, why it fits.
- **§3 Performance test suite** — benchmark scripts + workload profiles (repetitive short prompts for hit/miss; novel long prompts for overhead) + benchmark README + sample logs, wired into CI.
- **§4 Extension** — the new cache policy on a **feature branch**, tunable parameters exposed and documented, unit tests included.
- **§5 Evaluation** — vanilla vs. extended under *identical* workloads; parameter sweeps; latency-distribution & hit-rate plots; relative-improvement numbers; ablation if ideas are combined.
- **§6 Report** — 8–12 page PDF (Introduction → Extension Design → Experimental Setup → Results → Discussion → Conclusion) + clean repo with install/benchmark instructions and a Dockerfile or `environment.yml`.

Metrics the benchmark harness must emit (§3): per-request latency (mean, **p95, p99**), cache hit
rate, memory and CPU/GPU utilization, throughput. Hold the workload constant across
vanilla-vs-extended runs so differences are attributable to the policy alone.

## Baseline framework: DECIDED (2026-07-04)

Baseline = **vLLM Production Stack + LMCache** (KV-cache layer + its router). The project:
extend LMCache's controller lookup to per-instance match info, add a `loadaware` routing
strategy scoring cache-hit benefit vs. live instance load. Hot-prefix KV replication was
considered as a second optimization and is **parked** - see `CONTEXT.md` ("Replication Policy")
and the archived rationale on [issue #1](https://github.com/BenEpstein/caching-in-llms/issues/1#issuecomment-5218375689).
The upstream diff is `README.md` ("What we changed upstream"), the §2 baseline argument is
`docs/baseline-justification.md`, and the measured story is `docs/report/report.md`. Deployment
configs for the 2×A10 OpenShift cluster: `deploy/`.

## Tooling

The PDF mandates Python with `pytest`/`pytest-benchmark`, CI running the suite on every commit, and
a Dockerfile or `environment.yml`. Match the baseline library's conventions rather than imposing a
parallel stack.

## Collaboration

Several branches and agent sessions run against this repo in parallel. **Always `git pull` from the
remote before starting work** so you're building on the latest code, and pull again before pushing
to avoid conflicts. Check for open PRs touching your files before you edit them.

## Changelog discipline

`CHANGELOG.md` is the project's shared memory. **Update it in the same commit as the work** (or at
minimum once per session): add to the current date's entry, newest entry on top, using
**Added / Changed / Decided / Fixed** subsections. `Decided` is mandatory for any
project-direction decision and must point at the evidence (doc, report, or benchmark) behind it —
decisions without recorded rationale get re-litigated. Keep entries one or two lines each; the
changelog indexes the work, it doesn't duplicate it. Before starting a session, read the top entry
to catch up on what the other collaborator did.

## Docs discipline

Decisions live in **GitHub issues + CHANGELOG only**; `docs/` holds artifacts, never rationale.
`docs/decisions/` is gone (#59) - its last file was archived onto issue #1.

The **graded** surface is five documents: `README.md` (front door + the upstream diff),
`docs/baseline-justification.md` (§2), `benchmarks/README.md` (§3 operator manual),
`docs/report/report.md` (§6), and `CHANGELOG.md`. `CONTEXT.md` is the glossary those five share.
**Do not add to that set** - fold new writing into one of them, or put it on the ticket.
Every doc except `README.md` and `docs/report/report.md` carries a status header
(`> status: live | frozen · date · one-line trust note`); `frozen` means historical record:
verify against tickets/CHANGELOG before relying on it, never base new decisions on it.

## Reference material

- `docs/references/Final Project Guidelines.pdf` - the spec. Authoritative.
  (Course lecture PDFs were pruned 2026-08-01 for the submission repo; recover from git history if needed.)

## Machine-specific setup

Personal tooling (research CLI, local paths) lives in each collaborator's `CLAUDE.local.md`
(gitignored) — nothing in this shared file should reference one person's machine.

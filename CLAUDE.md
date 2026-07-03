# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A **BGU university final project**. The mission: take an existing open-source LLM caching
library, design and implement an **enhanced cache policy**, prove a **measurable, statistically
significant performance improvement** over the unmodified baseline, and write it up as a
publishable report. The full spec is `docs/references/Final Project Guidelines.pdf` — re-read it
when in doubt; it is the single source of truth and overrides assumptions made here.

**Status: greenfield.** As of this writing the repo contains only the reference docs — no baseline
library, no code, no tests, no benchmarks. The directory is *untracked* and sits inside a larger
git root at `~/Code` (`git rev-parse --show-toplevel` returns `/Users/benepstein/Code`, not this
folder). The "clean GitHub repo" deliverable (§6) means this project will need its **own** git repo;
do not commit project files into the `~/Code` parent. Confirm the intended repo boundary before
running any `git` command here.

## The rubric drives every priority

Grading is the professor's *impression* of four weighted criteria. Internalize the weights — they,
not engineering taste, decide what to work on first:

| Criterion | Weight | What it demands |
|---|---|---|
| **Correctness** | 40% | The cache (baseline + your policy) passes tests and behaves as specified |
| **Reproducibility** | 30% | Anyone can `git clone`, set up the env, and rerun the benchmarks to get the same numbers |
| **Performance Gain** | 15% | Clear, *statistically significant* improvement on a chosen metric |
| **Clarity** | 15% | Documented code + a report that tells one coherent story |

Correctness + Reproducibility = **70%**. A modest, rock-solid, fully reproducible gain beats an
impressive-but-flaky one. Lead with tests and a one-command reproducible benchmark; treat raw
performance as the smaller prize. The report's *persuasiveness* is graded directly — every claim
must be backed by a figure, table, or test the reader can rerun.

**The 100-grade carrot (§4):** the professor awards an exceptional grade to extensions contributed
upstream via a merged PR. Maintaining strict compatibility with the baseline's cache interface keeps
that path open — prefer it over forking the API.

## Deliverables map (PDF section → artifact)

Each guideline section produces a concrete artifact. Track these as the definition of done:

- **§2 Baseline justification** — a ≤1-page doc: chosen library, its main features, default eviction policy, why it fits.
- **§3 Performance test suite** — benchmark scripts (pytest-benchmark or custom Python) + workload profiles (repetitive short prompts to stress hit/miss; novel long prompts to measure overhead) + a "how to benchmark" README + sample CSV/JSON logs. Wire it into CI so every commit reruns the suite.
- **§4 Extension** — the new cache policy on a **feature branch**, with tunable parameters (cache size, weights, similarity threshold) exposed and documented, plus unit tests covering the new policy.
- **§5 Evaluation** — vanilla vs. extended under *identical* workloads; parameter sweeps (e.g. 50/100/200 MB); latency-distribution & hit-rate plots; relative-improvement numbers (e.g. % p95 latency reduction); ablation if multiple ideas are combined.
- **§6 Report** — single 8–12 page PDF: Introduction → Extension Design → Experimental Setup → Results → Discussion → Conclusion. Plus a clean repo with install + benchmark instructions and a Dockerfile/`environment.yml` for reproducibility.

## Metrics that must be collectable (§3)

Per-request latency (mean, **p95, p99** — not just mean), cache hit rate, memory and CPU/GPU
utilization, throughput (queries/tokens per second). Any benchmark harness built here must emit
these, and the vanilla-vs-extended comparison must hold the workload constant so differences are
attributable to the policy alone.

## Open decision: baseline framework (do this first)

§2 is the first real task and nothing else can proceed without it. Selection criteria from the PDF:
maturity/community support and **ease of modification** (clear structure, tests, docs — because you
must extend its eviction policy and ship unit tests). The PDF cites "KV cache" as a maturity example
and the candidate extensions (cost-aware eviction, hierarchical RAM+disk cache, approximate/semantic
matching) all point toward a request/response-level cache with a pluggable eviction policy rather than
an attention-level KV cache that is hard to modify. **This choice is not yet made** — surface
candidates and trade-offs to the user; do not silently assume one.

## Expected tooling (specified by the PDF, not yet present)

When scaffolding, the guidelines mandate: Python with `pytest`/`pytest-benchmark` for the benchmark
suite, CI running the suite on every commit, and a `Dockerfile` or `environment.yml` for reproducible
setup. Don't introduce these until the baseline is chosen and its own toolchain is known — match the
baseline library's conventions rather than imposing a parallel stack.

## Reference material

- `docs/references/Final Project Guidelines.pdf` — the spec. Authoritative.
- `docs/references/שיבוץ להרצאות.docx` — Hebrew lecture-scheduling doc; course logistics, unrelated to the project's technical content.

<!-- hyperresearch:start -->
## Research Base (hyperresearch) — Today is 2026-05-25

**CLI path: `/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch`** — use this exact path for every hyperresearch command. It may not be on your system PATH.

**Paths in this document are relative to your current working directory**, not to the CLI binary's location. Use `research/notes/final_report_<vault_tag>.md` (not a prefix with the binary path) when you save files.

This project uses hyperresearch as an agent-driven research knowledge base. The `research/` directory contains markdown notes collected from web sources and original research. Append `--json` to any command for structured output.

### How to do research

**Run a research session with `/hyperresearch <query>`.** This invokes the V8 16-step pipeline. The entry skill at `.claude/skills/hyperresearch/SKILL.md` is a thin ROUTER. The 16 step procedures live in their own skills (`hyperresearch-1-decompose` through `hyperresearch-16-readability-audit`) and are loaded fresh into context via the `Skill` tool when each step runs. This solves V7's context-compaction problem: each step's procedure lands in context only when needed. Read the entry skill before you start a research session; it explains the chain mechanics.

Step 1 classifies the query into one of two tiers (`light` or `full`) and the rest of the pipeline scales accordingly — short bounded queries skip the depth investigations, critics, and patcher (~30-40 min); argumentative deep-research queries run all 16 steps with adversarial review (~1.5-2.5 hours).

**Do NOT use WebFetch for source pages** — use `/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch fetch` instead. The skill files explain when to fetch vs. search.

### What the skill files own

The skill files own everything about how to research. That includes:
- The pipeline phases and what each phase does
- Which subagents exist and what each one is for (fetcher, loci-analyst, depth-investigator, 4 critics, patcher, polish-auditor)
- The tool-lock invariant (patcher and polish-auditor can only Read + Edit, never Write)
- The subagent spawn contract (every Task call passes the verbatim research_query + pipeline position + inputs)
- Artifact locations (`research/scaffold.md`, `research/prompt-decomposition.json`, `research/loci.json`, `research/comparisons.md`, interim notes, patch / polish logs)
- The curation pass after every research session

If you need to know how hyperresearch works, read the skill file. This document does NOT duplicate that content — when the skill file and this file disagree, the skill file wins.

### Canonical research query

In a normal run, the canonical research query is the user's verbatim prompt. In wrapped runs, if `research/prompt.txt` exists, that file is gospel and overrides any wrapping instructions. The pipeline persists the query as `research/query-<vault_tag>.md` with YAML frontmatter — this is the canonical query reference for all downstream layers. Wrapper requirements (save path, citation format, terminal sections) are a separate contract, captured in the scaffold — not pasted into the `## User Prompt (VERBATIM — gospel)` section.

### Academic APIs before web search

For any topic with a research literature, hit academic APIs BEFORE running web searches. They return citation-ranked canonical papers; web search returns derivative commentary.

- **Semantic Scholar:** `https://api.semanticscholar.org/graph/v1/paper/search?query=<q>&fields=title,year,citationCount,externalIds&limit=10` — then citation-chain the top papers forward + backward.
- **arXiv:** `https://export.arxiv.org/api/query?search_query=cat:cs.LG+AND+all:<q>&sortBy=relevance&max_results=25`
- **OpenAlex:** `https://api.openalex.org/works?search=<q>&sort=cited_by_count:desc&per-page=15&mailto=research@example.com`
- **PubMed:** `https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=<q>&retmode=json&retmax=20`

After the academic sweep, run web searches for context, news, non-academic angles, and at least one adversarial search ("criticism of X", "limitations of X").

### PDFs fetch directly

`/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch fetch` auto-detects PDF URLs (arXiv, NBER, SSRN, direct `.pdf` links) and extracts full text via pymupdf. Fetch them aggressively. Raw PDFs land in `research/raw/<note-id>.pdf` and the note's frontmatter links back via `raw_file:`.

### Searching the vault

```bash
/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch search "query" --json                # Full-text search
/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch search "query" --tag ml --json       # Filter by tag / status / date / parent
/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch search "query" --include-body --json # Full-body search, not just titles
/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch note show <id> --json                # Read one note
/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch note show <id1> <id2> <id3> --json   # Batch-read notes in one call
/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch note list --json                     # List all notes with summaries
/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch tags --json                          # Existing tag vocabulary
```

### Images, screenshots, and assets

```bash
/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch fetch "<url>" --tag <topic> --save-assets -j   # Saves screenshot + top images
/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch assets list --note <note-id> --json            # Assets for a specific note
/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch assets path <note-id> --type screenshot -j     # Get screenshot path (viewable with Read)
```

### Authenticated crawling

Login-gated content (LinkedIn, Twitter, paywalled news) needs a browser profile. Set up once via `/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch setup` or `crwl profiles`. Config in `.hyperresearch/config.toml` under `[web]`: `profile = "research"`, `magic = true`. LinkedIn / Twitter / Facebook / Instagram / TikTok auto-use a visible browser to avoid session kills.

If a fetch returns a login wall, tell the user to run `/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch setup` and create a login profile.

### Curate after every session

Every research session must end with a curation pass:

```bash
/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch note list --status draft -j                                        # Find unprocessed notes
/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch note show <id> -j                                                  # Read the content
/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch note update <id> --summary "<specific summary>" --add-tag <t> -j   # Add summary + tags
/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch lint -j                                                            # Find missing tags / summaries / broken links
/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch repair -j                                                          # Auto-fix broken links, rebuild indexes
/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch status -j                                                          # Overall vault health
```

Lifecycle: `draft` → `review` → `evergreen` (or `stale` → `deprecated` → `archive` for outdated material).

Summaries must be specific — "Mamba achieves linear-time sequence modeling via selective state spaces" beats "Paper about Mamba". Reuse the existing tag vocabulary (`/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch tags -j`) rather than inventing new tags.

### Key conventions

- Notes live in `research/notes/` as markdown with YAML frontmatter
- Link notes with `[[note-id]]` syntax
- After editing `.md` files directly, run `/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch sync` to update the index
- Run `/Users/benepstein/.local/pipx/venvs/hyperresearch/bin/hyperresearch --help` for the full command list
<!-- hyperresearch:end -->

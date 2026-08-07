> status: live · 2026-08-08 · index of the committed run data. The claims these runs back live in
> `docs/report/report.md`; this file only says which directory is which.

# `results/` - 11 run directories, two generations

Two generations measured on **two different instruments**. Keeping them apart is the point of
this file: mixing them is the one mistake this data invites.

## Generation 1 - the reported results (in-cluster driver)

Every reported number and figure comes from these six. `summary-per-seed.csv` is derived from
**these only**, so one table means one instrument. n=20 paired seeds, rate 16.

| Directory | Arm | Role |
|---|---|---|
| `20260805-230541-kvaware` | `kvaware` | baseline |
| `20260806-002645-loadaware-b0` | β=0 | ablation |
| `20260805-232541-loadaware-b0.5` | β=0.5 | **headline** |
| `20260805-234559-loadaware-b1.0` | β=1.0 | shipped default |
| `20260806-000626-loadaware-b2.0` | β=2.0 | top of grid |
| `20260806-144135-roundrobin` | `roundrobin` | cache-blind comparator |

`roundrobin` is a framing cell for fig12, **not** a point on the β grid: pass it to
`plot_results.py` as `--comparator`, never positionally.

## Generation 2 - the instrument problem (laptop driver, superseded)

`20260805-005210-kvaware`, `-011148-loadaware-b0`, `-013208-loadaware-b0.5`,
`-015202-loadaware-b1.0`, `-021215-loadaware-b2.0`.

Same arms, workload and rate, driven from a laptop over the public internet. **Not results** - the evidence for *An instrument problem, not a result* in the report, and deliberately excluded
from `summary-per-seed.csv`. Figures in `docs/figures-wan/`; their figure data is diffed by
`reproduce.sh` check 6.

The decomposition they exist to establish, recomputable from these directories alone:

| Cell | client TTFT | engine TTFT | non-engine | share |
|---|---|---|---|---|
| `kvaware` | 279.3 ms | 154.7 ms | 124.6 ms | 44.6% |
| β=0 | 287.8 ms | 158.9 ms | 128.9 ms | 44.8% |
| β=0.5 | 253.8 ms | 132.5 ms | 121.2 ms | 47.8% |
| β=1.0 | 332.6 ms | 137.7 ms | 194.8 ms | 58.6% |
| β=2.0 | 316.8 ms | 157.6 ms | 159.2 ms | 50.2% |

45–59% of every recorded TTFT never touched the model, and the non-engine term swung 121 → 195 ms
between two cells an hour apart - a systematic per-cell offset larger than the arm differences
under test, which more seeds cannot average away. In-cluster, the same term is 48.5 ms (25.8%)
for `kvaware` and 45.2 ms (27.2%) for β=0.5.

## Directory shape

Every run has `driver-seed<N>.csv` (per-request `send_ts`, `ttft_s`, `e2e_s`, `itls_ms`,
`status`), `prom/` (one JSON per Prometheus series), `dcgm.csv`, and `run.json`.

**`run.json` is what makes a comparison auditable**: arm, β, rate, measurement window, router
image and its sha256 digest, git commit, and a per-seed SHA-256 for all 20 frozen seed files.
`analyze.py compare` refuses to pair two runs whose `run.json` disagrees on rate or workload
manifest.

Generation 1 additionally carries `window.env`, `driver` (location, node, image, target) and
`workload_profile`; those three postdate generation 2, where `driver` and `workload_profile` are
`null`. Generation 1 also carries two extra `lmcache_*` series.

## `expected/`

`reproduce.sh`'s baselines: `stats.txt`, `figure-data.json` (gen 1), `figure-data-wan.json`
(gen 2). Regenerated and diffed on every run; a mismatch fails the build.

## What is not here

The 2026-08-03 characterization sweeps (7.5–16 req/s), the 2026-08-04 absolute-β cells, and the
pilots, probes and aborted runs were pruned by
[#57](https://github.com/BenEpstein/caching-in-llms/issues/57). They used the pre-normalization
policy or a worse instrument. They are in git history - `git log - results/` finds them.

Two numbers in the report are measured on the 2026-08-04 cells and are **not** recomputable
here: the 240.6 ms TTFT p10 floor and the ~226 → ~21 ms non-engine collapse. Tracked on
[#8](https://github.com/BenEpstein/caching-in-llms/issues/8), and flagged in the report beside
the numbers themselves.

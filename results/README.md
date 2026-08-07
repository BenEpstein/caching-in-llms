> status: live · 2026-08-08 · index of the committed run data; regenerated claims are verified by
> `scripts/reproduce.sh`

# `results/` - what is here and what each run is for

Eleven run directories, in **two generations measured on two different instruments**. Both are
kept on purpose. The confirmatory generation produces every number the report *reports*; the WAN
generation is the evidence for the report's methodology section, which is about why the first
instrument could not answer its own question.

Every directory has the same shape: `driver-seed<N>.csv` (per-request `send_ts`, `ttft_s`,
`e2e_s`, `itls_ms`, `status`), `prom/` (Prometheus range scrapes, one JSON per series),
`dcgm.csv` (per-GPU telemetry), `window.env` (the measurement window), and `run.json`.

**`run.json` is the run's manifest and the thing that makes a comparison auditable**: arm, β,
offered rate, output length, measurement window, router image *and* its sha256 digest, driver
location/node/image/target, git commit, workload profile, and a per-seed SHA-256 for all 20
frozen seed files. `analyze.py compare` refuses to pair two runs whose `run.json` disagrees on
rate or workload manifest - "identical workload across arms" is enforced, not assumed.

## Generation 1 - the confirmatory sweep (in-cluster driver)

**This is the evidence base for every reported result.** Driver runs as a Job inside the
cluster, so no wide-area network sits in the latency numbers. n = 20 paired seeds, offered rate
16 req/s, one frozen Zipfian shared-prefix workload.

| Directory | Arm | Role |
|---|---|---|
| `20260805-230541-kvaware` | `kvaware` | baseline |
| `20260806-002645-loadaware-b0` | `loadaware` β=0 | ablation - the load term switched off |
| `20260805-232541-loadaware-b0.5` | `loadaware` β=0.5 | **headline** |
| `20260805-234559-loadaware-b1.0` | `loadaware` β=1.0 | shipped default |
| `20260806-000626-loadaware-b2.0` | `loadaware` β=2.0 | top of the β grid |
| `20260806-144135-roundrobin` | `roundrobin` | cache-blind comparator - the capacity floor |

`summary-per-seed.csv` is derived from **these six only**: one table, one instrument. Figures →
`docs/figures/`. Statistics → `results/expected/stats.txt`.

`roundrobin` is a **framing cell, not a point on the β grid**. Pass it to `plot_results.py` as
`--comparator`, never positionally.

## Generation 2 - the WAN sweep (laptop driver, superseded)

**Not results. Evidence for the instrument problem.** Same arms, same workload, same rate, but
driven from a laptop over the public internet. These are the cells that show why the original
latency measurement had to be thrown out and re-run rather than reinterpreted.

| Directory | Arm |
|---|---|
| `20260805-005210-kvaware` | `kvaware` |
| `20260805-011148-loadaware-b0` | `loadaware` β=0 |
| `20260805-013208-loadaware-b0.5` | `loadaware` β=0.5 |
| `20260805-015202-loadaware-b1.0` | `loadaware` β=1.0 |
| `20260805-021215-loadaware-b2.0` | `loadaware` β=2.0 |

What they establish, and it reproduces from exactly these files:

| Cell | client mean TTFT | engine mean TTFT | non-engine | share |
|---|---|---|---|---|
| `kvaware` | 279.3 ms | 154.7 ms | 124.6 ms | 44.6% |
| β=0 | 287.8 ms | 158.9 ms | 128.9 ms | 44.8% |
| β=0.5 | 253.8 ms | 132.5 ms | 121.2 ms | 47.8% |
| β=1.0 | 332.6 ms | 137.7 ms | 194.8 ms | 58.6% |
| β=2.0 | 316.8 ms | 157.6 ms | 159.2 ms | 50.2% |

**45–59% of every recorded TTFT never touched the model**, and the non-engine term swung 121 ms
to 195 ms between two cells an hour apart in the same session - a per-cell systematic offset
larger than the 10–60 ms arm differences being tested. More seeds cannot fix a systematic
offset. That is the whole argument for rebuilding the instrument instead of adding power.

For contrast, the same decomposition on the in-cluster generation: `kvaware` 188.3 → 139.7 ms
(non-engine 48.5 ms, 25.8%), β=0.5 166.0 → 120.8 ms (45.2 ms, 27.2%).

These cells are **deliberately excluded from `summary-per-seed.csv`** - mixing two instruments in
one per-seed table is exactly the confusion the table exists to prevent. Their figures live in
`docs/figures-wan/` and their figure data is checked by `reproduce.sh` against
`results/expected/figure-data-wan.json`.

## `expected/` - the reproduce baselines

`stats.txt`, `figure-data.json` (confirmatory), `figure-data-wan.json` (WAN). `reproduce.sh`
regenerates each and diffs; a mismatch is a build failure, not a warning.

## What is not here

Earlier generations - the 2026-08-03 characterization sweeps at 7.5–16 req/s, the 2026-08-04
absolute-β cells, and the pilots, probes and aborted runs - are **in git history**, not in the
working tree. They were superseded rather than comparable: they used the pre-normalization
policy (absolute load with an α term) or a worse instrument. `git log - results/` finds them,
and the provenance SHAs in `CHANGELOG.md` still resolve.

Two numbers in `docs/report/report.md` are measured on the 2026-08-04 generation and so are not
recomputable from this directory: the **240.6 ms TTFT p10 floor** (`20260804-213425-kvaware`) and
the **~226 ms → ~21 ms** non-engine collapse. Tracked on
[#8](https://github.com/BenEpstein/caching-in-llms/issues/8) - either repoint them at the WAN
sweep above (157.4 → 101.9 ms p10, 124.6 → 48.5 ms non-engine, both verified here) or restore the
one cell.

## Recomputing, with no cluster and no GPU

```bash
./scripts/reproduce.sh          # everything below, diffed against expected/

# reported results (generation 1)
python3 benchmarks/analyze.py compare \
  results/20260805-232541-loadaware-b0.5 results/20260805-230541-kvaware
python3 benchmarks/plot_results.py results/20260805-2* results/20260806-0* \
  --comparator results/20260806-144135-roundrobin --cand loadaware-b0.5 --out /tmp/figs

# the instrument problem (generation 2)
python3 benchmarks/plot_results.py results/20260805-0* \
  --cand loadaware-b0.5 --out /tmp/figs-wan
```

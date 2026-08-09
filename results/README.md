> status: live · 2026-08-08 · index of the committed run data. The claims these runs back live in
> `docs/report/report.md`; this file only says which directory is which.

# `results/` - 18 run directories, three sweeps, one directory each

**One directory per sweep, one `summary-per-seed.csv` beside the runs it summarises** (#75).
Generations are numbered in the order they were measured. Generation 1 and the others were
measured on **two different instruments**; keeping them apart is the point of this file, and
mixing them is the one mistake this data invites. Generation 3 shares generation 2's
instrument but is a **separate window** - pairing a gen-3 cell against a gen-2 cell would
compare two evenings, not two policies.

| Directory | `sweep_id` | Cells | Window | Instrument |
|---|---|---|---|---|
| `gen1-wan/` | `gen1-wan` | 5 | 2026-08-05 00:52 → 02:12 | laptop over WAN (superseded) |
| `gen2-confirmatory/` | `gen2-confirmatory` | 6 | 2026-08-05 23:05 → 08-06 00:47 (+ roundrobin 08-06 14:41) | in-cluster driver |
| `gen3-7cell/` | `gen3-7cell` | 7 | 2026-08-08 02:39 → 05:02 | in-cluster driver |

## Generation 1 - the instrument problem (`gen1-wan/`, superseded)

`20260805-005210-kvaware`, `-011148-loadaware-b0`, `-013208-loadaware-b0.5`,
`-015202-loadaware-b1.0`, `-021215-loadaware-b2.0`.

Driven from a laptop over the public internet. **Not results** - the evidence for *An
instrument problem, not a result* in the report. No `summary-per-seed.csv`: it is superseded
and backs only that section. Figures in `docs/figures-wan/`; their figure data is diffed by
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

## Generation 2 - the reported results (`gen2-confirmatory/`)

Every reported number and figure comes from these six. Its `summary-per-seed.csv` (120 rows)
is derived from **these only**, so one table means one instrument. n=20 paired seeds, rate 16.

| Directory | Arm | Role |
|---|---|---|
| `20260805-230541-kvaware` | `kvaware` | baseline |
| `20260806-002645-loadaware-b0` | β=0 | ablation |
| `20260805-232541-loadaware-b0.5` | β=0.5 | **headline** |
| `20260805-234559-loadaware-b1.0` | β=1.0 | shipped default |
| `20260806-000626-loadaware-b2.0` | β=2.0 | top of grid |
| `20260806-144135-roundrobin` | `roundrobin` | cache-blind comparator |

⚠️ **`roundrobin` shares this sweep's id but not its window**: it was measured ~14 h after the
other five (2026-08-06 14:41). The `sweep_id` guard in `analyze.py compare` therefore will
**not** refuse a paired test against it - the protection is convention: `roundrobin` is a
framing cell for fig12, passed to `plot_results.py` as `--comparator`, **never positionally**,
and it enters no paired test. Folding it into the sweep was decided on
[#75](https://github.com/BenEpstein/loadaware-vllm-router/issues/75); the window provenance survives
in its `run.json` timestamps.

## Generation 3 - the 7-cell sweep (`gen3-7cell/`)

Same instrument as generation 2 (in-cluster driver), same rate 16 / OSL 64 / n=20 frozen seeds,
router `loadaware:acf43d1` and bench `bench-driver:42e6a32` - the same images generation 2 used,
so the policy code is held constant and only the cell set differs. One unattended window,
2026-08-08 02:39-05:02. All seven passed the validity gate (pooled error 0.05-0.66%).

| Directory | Arm | Role |
|---|---|---|
| `20260808-023919-kvaware` | `kvaware` | baseline |
| `20260808-025932-loadaware-b0.5` | β=0.5 | **headline** (configuration of record) |
| `20260808-031955-loadaware-b1.0` | β=1.0 | shipped default |
| `20260808-034018-loadaware-b2.0` | β=2.0 | top of grid |
| `20260808-040053-loadaware-b0.25` | β=0.25 | descriptive, low end |
| `20260808-042133-loadaware-b0` | β=0 | ablation + drift sentinel |
| `20260808-044202-roundrobin` | `roundrobin` | cache-blind comparator |

Two cells are **descriptive** and carry no p-value, so the pre-registered alpha=0.025 pair is
unaffected: `b0.25` and `roundrobin`. `roundrobin` saturates here (10.24 req/s achieved against
16 offered, 65%) - pass it to `plot_results.py` as `--comparator`, never positionally, and
report its throughput shortfall rather than its latency ratio.

Figures in `docs/figures-gen3/`; the figure data is diffed by `reproduce.sh` check 7. Its
per-seed table is `gen3-7cell/summary-per-seed.csv` (140 rows), regenerated and diffed by
check 3 alongside the reported one.

## Directory shape

Every run has `driver-seed<N>.csv` (per-request `send_ts`, `ttft_s`, `e2e_s`, `itls_ms`,
`status`), `prom/` (one JSON per Prometheus series), `dcgm.csv`, and `run.json`.

**`run.json` is what makes a comparison auditable**: arm, β, rate, measurement window, router
image and its sha256 digest, git commit, and a per-seed SHA-256 for all 20 frozen seed files.
`analyze.py compare` refuses to pair two runs whose `run.json` disagrees on rate, workload
manifest, or **`sweep_id`**.

`sweep_id` names the batch a cell was measured in, and it is the only thing separating the
generations at the data level. Rate and workload manifest cannot do it: every sweep replays the
same frozen dataset at the same rate, so those two match across generations *by construction*.
Before the field existed, pairing a gen-3 cell against a gen-2 cell returned `p=0.0000` and a
45.1% effect - three days of cluster drift reported as a policy result. It now exits 1.

`run_sweep.sh` defaults its results root to `results/$SWEEP_ID/`, so every future sweep lands
in its own directory with a matching id - the layout above is what the scripts now produce,
not a convention to remember. The 18 directories that predate the field were back-filled;
runs without it still pair, so third-party or archived directories keep working.

Generation 2 additionally carries `window.env`, `driver` (location, node, image, target) and
`workload_profile`; those three postdate generation 1, where `driver` and `workload_profile`
are `null`. Generation 2 also carries two extra `lmcache_*` series.

## `expected/`

`reproduce.sh`'s baselines: `stats.txt`, `figure-data.json` (gen 2, reported),
`figure-data-wan.json` (gen 1), `figure-data-gen3.json` (gen 3). Regenerated and diffed on
every run; a mismatch fails the build.

## What is not here

The 2026-08-03 characterization sweeps (7.5–16 req/s), the 2026-08-04 absolute-β cells, and the
pilots, probes and aborted runs were pruned by
[#57](https://github.com/BenEpstein/loadaware-vllm-router/issues/57). They used the pre-normalization
policy or a worse instrument. They are in git history - `git log - results/` finds them.
The flat pre-#75 layout (`results/<run>/` with the generations interleaved) is also in history;
paths in CHANGELOG entries and closed tickets from before 2026-08-08 use it.

Two numbers in the report are measured on the 2026-08-04 cells and are **not** recomputable
here: the 240.6 ms TTFT p10 floor and the ~226 → ~21 ms non-engine collapse. Tracked on
[#8](https://github.com/BenEpstein/loadaware-vllm-router/issues/8), and flagged in the report beside
the numbers themselves.

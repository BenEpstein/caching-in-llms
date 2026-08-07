#!/usr/bin/env bash
# The measured replay, running INSIDE the cluster (#27). Baked into the bench image;
# benchmarks/bench_job.sh is what wraps it in a Job and collects the result.
#
# CONTRACT WITH collect_job.py - stdout is the only channel out of this pod:
#
#   NODE <node-name>
#   CELL_START <epoch>                       <- after dataset verification, before traffic
#   ==> seed N            ... plus load_driver's own summary lines   (progress only)
#   CELL_END <epoch>                         <- immediately after the last seed
#   BEGIN driver-seedN.csv sha256=<hex of the PLAINTEXT csv> bytes=<n>
#   <gzip -9 | base64, default 76-column wrap>
#   END driver-seedN.csv
#   ALL_DONE
#
# Three deliberate choices in that format:
#
#   1. The window markers come from THIS pod's clock, not the laptop's - a laptop-clock
#      window would drag warm-up traffic into the Prometheus dump and contaminate the
#      imbalance co-primary (benchmarks/README.md, "The measured replay runs in-cluster").
#   2. Blob emission happens AFTER CELL_END, so the gzip work does not widen the window.
#   3. Per-seed frames with a checksum, not one blob for the cell: truncation is then
#      detectable per seed instead of silently costing the whole cell. Default base64
#      wrapping is also deliberate - an unwrapped single-line blob gets split into
#      partial-line records by the CRI log format, and reassembly is not worth
#      depending on.
set -euo pipefail

: "${TARGET_URL:?}" "${MODEL:?}" "${RATE:?}" "${MAX_TOKENS:?}" "${SEEDS:?}"

WORKLOADS=/tmp/workloads
OUT=/tmp/out
mkdir -p "$OUT"

# Drift exits non-zero here; with backoffLimit: 0 that fails the Job loudly rather than
# measuring on the wrong dataset (validity rule 4).
/app/verify_dataset.sh "$WORKLOADS"

echo "NODE ${NODE_NAME:-unknown}"
echo "CELL_START $(date +%s)"

for seed in $SEEDS; do
  echo "==> seed $seed"
  python3 /app/load_driver.py \
    --base-url "$TARGET_URL" --model "$MODEL" \
    --workload "$WORKLOADS/seed-$seed.jsonl" \
    --rate "$RATE" --seed "$seed" --max-tokens "$MAX_TOKENS" \
    --out "$OUT/driver-seed$seed.csv"
done

echo "CELL_END $(date +%s)"

for seed in $SEEDS; do
  f="$OUT/driver-seed$seed.csv"
  echo "BEGIN driver-seed$seed.csv sha256=$(sha256sum "$f" | cut -d' ' -f1) bytes=$(wc -c <"$f")"
  gzip -9 -c "$f" | base64
  echo "END driver-seed$seed.csv"
done

echo "ALL_DONE"

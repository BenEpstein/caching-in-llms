#!/usr/bin/env bash
# Regenerate the frozen workloads into a writable directory and verify them against the
# committed manifest. Exits non-zero on any drift.
#
# Two callers, deliberately - benchmarks/in_pod.sh before every measured cell, and
# .github/workflows/bench-image.yml on every bench-image build - so "the frozen dataset is
# reconstructible from source" is a tested claim rather than an asserted one.
#
# Lives in the image, not on the laptop path: run_cell.sh still calls freeze_workloads.py
# directly at step 0 as a fast pre-flight before the cell's setup.
set -euo pipefail

OUT="${1:?usage: verify_dataset.sh <out-dir>}"
# `novel` is the no-reuse cache-overhead profile (§3); see benchmarks/README.md.
# default stays zipfian - three callers rely on it (run_cell.sh, bench_job.sh)
WORKLOAD_PROFILE="${WORKLOAD_PROFILE:-zipfian}"
case "$WORKLOAD_PROFILE" in
  zipfian) DEFAULT_MANIFEST=/app/workloads/manifest.json ;;
  novel)   DEFAULT_MANIFEST=/app/workloads/novel/manifest.json ;;
  *) echo "unknown WORKLOAD_PROFILE=$WORKLOAD_PROFILE (want: zipfian|novel)" >&2; exit 2 ;;
esac
MANIFEST_SRC="${MANIFEST_SRC:-$DEFAULT_MANIFEST}"
FREEZE="${FREEZE:-/app/freeze_workloads.py}"

mkdir -p "$OUT"

# freeze_workloads.py looks for manifest.json INSIDE --out-dir, and the image's /app is not
# writable under the restricted SCC's arbitrary uid - hence copying the committed manifest
# into the writable directory rather than writing the JSONL next to it.
cp "$MANIFEST_SRC" "$OUT/manifest.json"

python3 "$FREEZE" --profile "$WORKLOAD_PROFILE" --out-dir "$OUT"

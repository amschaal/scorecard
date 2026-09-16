#!/usr/bin/env bash
# Backfill job accounting for a date range. Run ON THE CLUSTER login node.
#   scripts/backfill.sh hive 2026-01-01 2026-09-01
# Collects one day per sacct call from FROM (inclusive) to TO (exclusive), oldest first.
set -euo pipefail
CLUSTER=${1:?cluster}; FROM=${2:?from YYYY-MM-DD}; TO=${3:?to YYYY-MM-DD (exclusive)}
COLLECTOR=${COLLECTOR:-$HOME/hpcusage/slurm_collector.py}
d=$FROM
while [[ "$d" < "$TO" ]]; do
  next=$(date -d "$d + 1 day" +%F)
  echo "== $d"
  "$COLLECTOR" --cluster "$CLUSTER" --jobs --date "$next" --days-back 1 "${@:4}"
  d=$next
done

#!/usr/bin/env bash
# Rebuild a local, read-only copy of the pipeline from a backup under .data/ (docs/SHOWCASE.md):
# the database dump becomes work/sde.db, and the artifact folders are linked in beside it.
#   bash scripts/restore_local.sh [.data]
set -euo pipefail
cd "$(dirname "$0")/.."
data="${1:-.data}"
dump="$(ls -t "$data"/postgres/*.sql.gz | head -1)"
work="$data/work"
rm -rf "$work" && mkdir -p "$work"
env -u DATABASE_URL -u REDIS_URL -u S3_BUCKET SDE_WORK_DIR="$PWD/$work" "${PYTHON:-.venv/bin/python}" scripts/pg_to_sqlite.py "$dump"
for d in bundles worlds traces drafts leak_reviews accepted; do
  [ -d "$data/s3-artifacts/$d" ] && ln -s "../s3-artifacts/$d" "$work/$d"
done
echo "restored into $work; start the API with:"
echo "  env -u DATABASE_URL -u REDIS_URL -u S3_BUCKET SDE_WORK_DIR=$PWD/$work SDE_API_KEY=local ${PYTHON:-.venv/bin/python} -m api"

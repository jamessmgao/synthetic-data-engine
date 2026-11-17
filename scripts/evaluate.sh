#!/usr/bin/env bash
# Measure a task's difficulty: N_RUNS rollouts of terminus-2 + glm-5.3-flash
# (reasoning_effort=high), then report the pass distribution and band verdict.
# Rerunning after a kill reuses every rollout that already finished.
# Usage: bash scripts/evaluate.sh output/my-task [--runs 5] [--workers 4]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

if [ $# -lt 1 ]; then
    echo "Usage: bash scripts/evaluate.sh <task_dir> [--runs N] [--workers N]"
    exit 1
fi

TASK_DIR="$1"
shift
if [ ! -d "$TASK_DIR" ] && [ -d "$ROOT_DIR/$TASK_DIR" ]; then
    TASK_DIR="$ROOT_DIR/$TASK_DIR"
fi
TASK_DIR="$(cd "$TASK_DIR" && pwd)"

cd "$ROOT_DIR"
python -m evaluation.evaluate "$TASK_DIR" "$@"

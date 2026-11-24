#!/usr/bin/env bash
# Structural check of one task directory, from any working directory:
#   scripts/validate.sh <task_dir>
# PYTHON chooses the interpreter (default: python3). Exit 0 = passed, 1 = errors.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONPATH="$repo${PYTHONPATH:+:$PYTHONPATH}" exec "${PYTHON:-python3}" -m validator.validate "$@"

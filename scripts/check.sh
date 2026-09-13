#!/usr/bin/env bash
# Run every gate the way CI does, so a green run here means a green run there.
# Exits on the first failure, and uses `python -m` so the tools resolve against
# this project's environment rather than whatever happens to be on PATH.
set -euo pipefail
PY=${PYTHON:-python3}
"$PY" -m ruff check src tests
"$PY" -m ruff format --check src tests
"$PY" -m mypy
"$PY" -m pytest -q
echo "all checks passed"

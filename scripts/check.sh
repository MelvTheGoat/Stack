#!/usr/bin/env bash
# Run every gate the way CI does, so a green run here means a green run there.
# Exits on the first failure; no pipes that hide an exit code.
set -euo pipefail
ruff check src tests
ruff format --check src tests
mypy
pytest -q
echo "all checks passed"

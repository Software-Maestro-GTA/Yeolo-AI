#!/bin/bash
# Check the installed environment without installing packages or updating the lock.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
ENV_DIR="${UV_PROJECT_ENVIRONMENT:-$REPO_ROOT/.venv}"

if ! command -v uv >/dev/null 2>&1; then
    printf 'ENV FAIL: uv is missing; install uv and run uv sync --locked --group dev.\n' >&2
    exit 127
fi
for file in pyproject.toml uv.lock; do
    if [[ ! -f "$file" ]]; then
        printf 'ENV FAIL: missing %s\n' "$file" >&2
        exit 1
    fi
done
for tool in python ruff pytest; do
    if [[ ! -x "$ENV_DIR/bin/$tool" ]]; then
        printf 'ENV FAIL: missing %s; run uv sync --locked --group dev.\n' "$ENV_DIR/bin/$tool" >&2
        exit 1
    fi
done
"$ENV_DIR/bin/python" --version
if uv --no-cache sync --check --locked --offline --no-python-downloads --group dev; then
    printf 'ENV PASS: Python requirement, lockfile and installed dev dependencies match.\n'
else
    status=$?
    printf 'ENV FAIL: inspect the uv diagnostic above; prepare the environment with uv sync --locked --group dev.\n' >&2
    exit "$status"
fi

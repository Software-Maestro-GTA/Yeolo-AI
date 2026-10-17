#!/bin/bash
# Run reproducible checks from the repository root, without syncing dependencies.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"
LOG_FILE="$REPO_ROOT/log.md"
SCOPE=FULL
PYTEST_ARGS=(tests)

if [[ $# -gt 0 ]]; then
    if [[ "$1" != -- || $# -eq 1 ]]; then
        printf 'Usage: bash .agents/hooks/test.sh [-- <pytest arguments>]\n' >&2
        exit 2
    fi
    shift
    SCOPE=TARGETED
    PYTEST_ARGS=("$@")
fi

# An inherited -k/-m option must not turn a full run into a selected run.
unset PYTEST_ADDOPTS
export LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false

printf '\n## Validation %s (%s)\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" "$SCOPE" >> "$LOG_FILE"
printf 'Validation scope: %s\n' "$SCOPE"

run_check() {
    local label="$1" status
    shift
    printf '\n[%s] ' "$label"
    printf '%q ' "$@"
    printf '\n'
    {
        printf -- '- %s command: ' "$label"
        printf '%q ' "$@"
        printf '\n'
    } >> "$LOG_FILE"
    # Keep diagnostics live in the terminal; persist only commands and statuses.
    if "$@"; then
        status=0
    else
        status=$?
    fi
    printf '[%s] exit=%s\n' "$label" "$status"
    printf -- '- %s exit=%s\n' "$label" "$status" >> "$LOG_FILE"
    if [[ "$status" -ne 0 ]]; then
        printf 'Validation FAIL (%s); see terminal diagnostics.\n' "$label" | tee -a "$LOG_FILE"
        exit "$status"
    fi
}

for hook in .agents/hooks/*.sh; do
    run_check "SHELL:$hook" bash -n "$hook"
done
run_check PREFLIGHT bash .agents/hooks/preflight.sh
run_check HARNESS uv --no-cache run --no-sync --offline python scripts/check_harness.py
run_check LINT uv --no-cache run --no-sync --offline ruff check .
run_check TEST uv --no-cache run --no-sync --offline pytest -q "${PYTEST_ARGS[@]}"
printf 'Validation PASS (%s: shell syntax, environment, harness, Ruff, pytest). TYPE: NOT CONFIGURED.\n' "$SCOPE" | tee -a "$LOG_FILE"

#!/bin/sh
# Run the same workflow and shell checks locally and in the CI container.
set -eu

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

for tool in actionlint shellcheck; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf 'Missing %s; install actionlint and ShellCheck before running static checks.\n' "$tool" >&2
        exit 127
    fi
done

actionlint -color .github/workflows/*.yml
shellcheck .agents/hooks/*.sh scripts/*.sh

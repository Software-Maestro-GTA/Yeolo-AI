#!/bin/bash
# Create missing work records without overwriting previous work.
set -euo pipefail

HARNESS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$HARNESS_DIR/.." && pwd)"

if [[ ! -e "$REPO_ROOT/progress.md" ]]; then
    if [[ ! -r "$HARNESS_DIR/templates/progress_template.md" ]]; then
        printf 'Missing readable progress template: %s\n' "$HARNESS_DIR/templates/progress_template.md" >&2
        exit 1
    fi
    # noclobber also protects a board created by another process after the check.
    (set -o noclobber; cat "$HARNESS_DIR/templates/progress_template.md" > "$REPO_ROOT/progress.md")
    printf 'Created progress.md\n'
else
    printf 'Preserved progress.md\n'
fi

if [[ ! -e "$REPO_ROOT/log.md" ]]; then
    (set -o noclobber; printf '# Harness Execution Log\n' > "$REPO_ROOT/log.md")
    printf 'Created log.md\n'
else
    printf 'Preserved log.md\n'
fi

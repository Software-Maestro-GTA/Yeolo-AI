"""Run the real branch guard with literal, potentially shell-like branch names."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize('branch,expected_status', [
    ('dev', 0),
    ('feature/travel', 1),
    ('test/$(printf${IFS}BRANCH_INPUT_EXECUTED)', 1),
    ('test/`printf${IFS}BRANCH_INPUT_EXECUTED`', 1),
])
def test_pr_branch_guard_treats_branch_as_data(branch, expected_status):
    path = Path(__file__).resolve().parents[1] / '.github/workflows/pr-target-guard.yml'
    workflow = yaml.safe_load(path.read_text())
    step = workflow['jobs']['validate-source-branch']['steps'][0]
    assert step['env']['SOURCE_BRANCH'] == '${{ github.head_ref }}'
    assert '${{' not in step['run']
    result = subprocess.run(
        ['bash', '-e', '-c', step['run']],
        env={**os.environ, 'TARGET_BRANCH': 'main', 'SOURCE_BRANCH': branch},
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == expected_status
    assert f'Source Branch: {branch}\n' in result.stdout

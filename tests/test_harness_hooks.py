"""Exercise harness hooks in an isolated checkout without invoking package tools."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def harness(tmp_path):
    root = tmp_path / 'checkout with spaces'
    source = Path(__file__).resolve().parents[1] / '.agents'
    shutil.copytree(source / 'hooks', root / '.agents/hooks')
    templates = root / '.agents/templates'
    templates.mkdir()
    (templates / 'progress_template.md').write_text('New progress board\n')
    for name in ('pyproject.toml', 'uv.lock'):
        (root / name).write_text('')
    environment = root / '.venv/bin'
    environment.mkdir(parents=True)
    for name in ('python', 'ruff', 'pytest'):
        executable = environment / name
        executable.write_text('#!/bin/bash\nprintf "fake installed tool\\n"\n')
        executable.chmod(0o755)
    binary = tmp_path / 'bin'
    binary.mkdir()
    uv = binary / 'uv'
    uv.write_text(
        '#!/bin/bash\n'
        'printf "%s\\n" "$PWD" "$*" "${PYTEST_ADDOPTS-unset}" '
        '"$LANGSMITH_TRACING/$LANGCHAIN_TRACING_V2" >> "$CALLS_FILE"\n'
        'case " $* " in\n'
        '  *" sync "*) exit "${PREFLIGHT_STATUS:-0}" ;;\n'
        '  *" python "*) exit "${HARNESS_STATUS:-0}" ;;\n'
        '  *" ruff "*) exit "${LINT_STATUS:-0}" ;;\n'
        '  *" pytest "*) printf "pytest diagnostics\\n"; exit "${TEST_STATUS:-0}" ;;\n'
        'esac\n'
    )
    uv.chmod(0o755)
    env = {
        **os.environ,
        'PATH': f'{binary}{os.pathsep}{os.environ["PATH"]}',
        'CALLS_FILE': str(tmp_path / 'calls'),
        'PYTEST_ADDOPTS': '-k inherited_selection',
        'LANGSMITH_TRACING': 'true',
        'LANGCHAIN_TRACING_V2': 'true',
        'LINT_STATUS': '0',
        'TEST_STATUS': '0',
        'PREFLIGHT_STATUS': '0',
        'HARNESS_STATUS': '0',
        'UV_PROJECT_ENVIRONMENT': str(root / '.venv'),
    }

    def run(name, *args, **overrides):
        return subprocess.run(
            ['bash', str(root / '.agents/hooks' / name), *args],
            cwd=tmp_path,
            env={**env, **overrides},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    return root, run, Path(env['CALLS_FILE'])


def test_init_creates_missing_records_and_preserves_existing_work(harness):
    root, run, _ = harness
    assert run('init.sh').returncode == 0
    assert (root / 'progress.md').read_text() == 'New progress board\n'
    assert (root / 'log.md').is_file()
    (root / 'progress.md').write_text('Work in progress\n')
    (root / 'log.md').write_text('Previous verification\n')
    assert run('init.sh').returncode == 0
    assert (root / 'progress.md').read_text() == 'Work in progress\n'
    assert (root / 'log.md').read_text() == 'Previous verification\n'


def test_init_missing_template_fails_without_clobbering_log(harness):
    root, run, _ = harness
    (root / '.agents/templates/progress_template.md').unlink()
    (root / 'log.md').write_text('Previous verification\n')
    assert run('init.sh').returncode != 0
    assert (root / 'log.md').read_text() == 'Previous verification\n'
    assert not (root / 'progress.md').exists()


@pytest.mark.parametrize('args,scope,target', [
    ((), 'FULL', 'tests'),
    (('--', 'tests/test_gemini_parameters.py'), 'TARGETED', 'tests/test_gemini_parameters.py'),
])
def test_validation_uses_repo_root_and_preserves_log(harness, args, scope, target):
    root, run, calls = harness
    (root / 'log.md').write_text('Previous verification\n')
    result = run('test.sh', *args)
    assert result.returncode == 0, result.stderr
    assert calls.read_text().splitlines() == [
        str(root), '--no-cache sync --check --locked --offline --no-python-downloads --group dev', 'unset', 'false/false',
        str(root), '--no-cache run --no-sync --offline python scripts/check_harness.py', 'unset', 'false/false',
        str(root), '--no-cache run --no-sync --offline ruff check .', 'unset', 'false/false',
        str(root), f'--no-cache run --no-sync --offline pytest -q {target}', 'unset', 'false/false',
    ]
    log = (root / 'log.md').read_text()
    assert log.startswith('Previous verification\n')
    assert f'Validation PASS ({scope}:' in log
    assert 'TYPE: NOT CONFIGURED' in log
    assert 'pytest diagnostics' in result.stdout


@pytest.mark.parametrize('stage,status,executed', [
    ('PREFLIGHT', '3', 1), ('HARNESS', '1', 2), ('LINT', '7', 3), ('TEST', '1', 4), ('TEST', '5', 4),
])
def test_validation_propagates_failures_without_false_success(harness, stage, status, executed):
    root, run, calls = harness
    result = run('test.sh', **{f'{stage}_STATUS': status})
    assert result.returncode == int(status)
    log = (root / 'log.md').read_text()
    assert f'{stage} exit={status}' in log
    assert 'Validation FAIL' in log
    assert 'Validation PASS' not in log
    commands = calls.read_text().splitlines()[1::4]
    assert len(commands) == executed


def test_invalid_shell_stops_before_python_tools(harness):
    root, run, calls = harness
    (root / '.agents/hooks/init.sh').write_text('#!/bin/bash\nif then\n')
    result = run('test.sh')
    assert result.returncode != 0
    assert not calls.exists()
    assert 'Validation FAIL (SHELL:' in (root / 'log.md').read_text()


@pytest.mark.parametrize('args', [('--',), ('tests/test_harness_hooks.py',)])
def test_invalid_arguments_do_not_run_checks(harness, args):
    root, run, calls = harness
    result = run('test.sh', *args)
    assert result.returncode == 2
    assert 'Usage:' in result.stderr
    assert not calls.exists()
    assert not (root / 'log.md').exists()


@pytest.mark.parametrize('missing', ['uv.lock', '.venv/bin/python', '.venv/bin/ruff', '.venv/bin/pytest'])
def test_preflight_rejects_incomplete_environment_before_running_tools(harness, missing):
    root, run, calls = harness
    (root / missing).unlink()
    result = run('preflight.sh')
    assert result.returncode != 0
    assert 'ENV FAIL: missing' in result.stderr
    assert not calls.exists()


def test_preflight_reports_missing_uv(harness):
    root, run, calls = harness
    tools = root / 'without-uv'
    tools.mkdir()
    for name in ('bash', 'dirname'):
        (tools / name).symlink_to(shutil.which(name))
    result = run('preflight.sh', PATH=str(tools))
    assert result.returncode == 127
    assert 'ENV FAIL: uv is missing' in result.stderr
    assert not calls.exists()

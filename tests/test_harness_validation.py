"""Check harness diagnostics using small temporary repositories with deliberate defects."""

import json

import pytest

from scripts.check_harness import check_harness


@pytest.fixture
def harness_root(tmp_path):
    files = {
        'AGENTS.md': '[Guide](.agents/system.md)\n',
        'README.md': '[Site](https://example.invalid)\n',
        '.agents/AGENTS.md': '',
        '.agents/system.md': '',
        '.agents/hooks/init.sh': '#!/bin/bash\n',
        '.agents/hooks/test.sh': '#!/bin/bash\n',
        '.agents/skills/sample/SKILL.md': '---\nname: sample\ndescription: Example skill\n---\n',
    }
    for name, contents in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    config = {
        'environments': {'ai-server': {
            'init_command': 'bash .agents/hooks/init.sh',
            'verify_command': 'bash .agents/hooks/test.sh',
            'test_command': 'uv --no-cache run --no-sync --offline pytest -q tests',
            'lint_command': 'uv --no-cache run --no-sync --offline ruff check .',
        }},
    }
    (tmp_path / '.agents/config.json').write_text(json.dumps(config))
    return tmp_path


def test_valid_harness_ignores_examples_and_independent_spec_checkout(harness_root):
    (harness_root / 'README.md').write_text(
        '[Guide](.agents/system.md#section)\n'
        '[Web](https://example.invalid)\n'
        '[Spec](.agents/Yeolo-SPEC/not-checked-out.md)\n'
        '`[Inline example](missing.md)`\n'
        '```markdown\n[Example](missing.md)\n```\n'
        '~~~markdown\n[Other example](missing.md)\n~~~\n'
    )
    # Historical boards are not active documentation.
    (harness_root / '.agents/log.md').write_text('[Old](missing.md)')
    assert check_harness(harness_root) == []


@pytest.mark.parametrize('metadata', [
    '',
    '---\nname: [\n---\n',
    '---\n- sample\n---\n',
    '---\nname: wrong-directory\ndescription: Description\n---\n',
    '---\nname: sample\ndescription: 123\n---\n',
])
def test_invalid_skill_reports_file_and_reason(harness_root, metadata):
    path = harness_root / '.agents/skills/sample/SKILL.md'
    path.write_text(metadata)
    errors = check_harness(harness_root)
    assert len(errors) == 1
    assert errors[0].startswith('.agents/skills/sample/SKILL.md:')


@pytest.mark.parametrize('link,reason', [
    ('missing.md', 'broken local link'),
    ('../outside.md', 'link leaves repository'),
])
def test_invalid_link_is_attributed_to_document(harness_root, link, reason):
    (harness_root / 'README.md').write_text(f'[Guide]({link})')
    assert check_harness(harness_root) == [f'README.md: {reason}: {link}']


def test_configured_hook_must_exist_without_executing_command(harness_root):
    (harness_root / '.agents/hooks/test.sh').unlink()
    assert check_harness(harness_root) == ['verify_command: missing repository hook: .agents/hooks/test.sh']


def test_invalid_config_is_reported_without_traceback(harness_root):
    (harness_root / '.agents/config.json').write_text('{')
    assert check_harness(harness_root) == ['.agents/config.json: invalid JSON or missing ai-server commands']


def test_config_cannot_point_validation_at_another_directory(harness_root):
    path = harness_root / '.agents/config.json'
    config = json.loads(path.read_text())
    config['environments']['ai-server']['path'] = '../'
    path.write_text(json.dumps(config))
    assert check_harness(harness_root) == ['ai-server path: expected repository root']


@pytest.mark.parametrize('field,command', [
    ('test_command', 'uv run nonexistent_test_command'),
    ('test_command', 'uv --no-cache run --no-sync --offline pytest --collect-only tests'),
    ('test_command', 'uv --no-cache run --no-sync --offline pytest -q tests/test_harness_validation.py'),
    ('lint_command', 'uv --no-cache run --no-sync --offline ruff check --fix .'),
    ('lint_command', 'uv --no-cache run --no-sync --offline ruff check .; true'),
])
def test_config_rejects_commands_that_do_not_perform_the_full_read_only_check(harness_root, field, command):
    path = harness_root / '.agents/config.json'
    config = json.loads(path.read_text())
    config['environments']['ai-server'][field] = command
    path.write_text(json.dumps(config))
    errors = check_harness(harness_root)
    assert len(errors) == 1
    assert errors[0].startswith(f'{field}: expected uv ')

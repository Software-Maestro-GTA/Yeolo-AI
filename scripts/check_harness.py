"""Validate repository-owned harness metadata and local documentation links."""

import argparse
import json
import re
import shlex
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml


def prose_only(text: str) -> str:
    """Exclude fenced examples and inline code from documentation link checks."""
    lines = []
    fence = None
    for line in text.splitlines():
        marker = re.match(r'^\s*(`{3,}|~{3,})(.*)$', line)
        if marker:
            token, suffix = marker.groups()
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence) and not suffix.strip():
                fence = None
            continue
        if fence is None:
            lines.append(re.sub(r'`+[^`]*`+', '', line))
    return '\n'.join(lines)


def check_skill(path: Path) -> list[str]:
    """Check required skill metadata without importing a machine-local validator."""
    text = path.read_text(encoding='utf-8')
    match = re.match(r'\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)', text, re.DOTALL)
    if not match:
        return ['missing YAML frontmatter']
    try:
        metadata = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return ['invalid YAML frontmatter']
    if not isinstance(metadata, dict):
        return ['frontmatter must be a mapping']
    errors = []
    name = metadata.get('name')
    if (
        not isinstance(name, str)
        or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name)
        or len(name) > 64
        or name != path.parent.name
    ):
        errors.append('name must match the skill directory and use lowercase hyphenated words')
    description = metadata.get('description')
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        errors.append('description must contain 1–1024 characters')
    return errors


def check_links(path: Path, root: Path) -> list[str]:
    """Check inline file links in prose; skip URLs, anchors and optional spec checkout."""
    errors = []
    spec = (root / '.agents/Yeolo-SPEC').resolve()
    for destination in re.findall(r'\[[^\]\n]*\]\(([^)\n]+)\)', prose_only(path.read_text(encoding='utf-8'))):
        destination = destination.strip()
        if destination.startswith('<'):
            destination = destination[1:destination.index('>')] if '>' in destination else destination
        else:
            destination = destination.split(' "', 1)[0]
        parsed = urlsplit(destination)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        target = (path.parent / unquote(parsed.path)).resolve()
        if target.is_relative_to(spec):
            # CI does not need credentials for the independent specification repository.
            continue
        if not target.is_relative_to(root):
            errors.append(f'link leaves repository: {destination}')
        elif not target.exists():
            errors.append(f'broken local link: {destination}')
    return errors


def check_config(root: Path) -> list[str]:
    """Validate configured entry points as data, without executing their commands."""
    path = root / '.agents/config.json'
    try:
        config = json.loads(path.read_text(encoding='utf-8'))
        environment = config['environments']['ai-server']
        commands = {key: environment[key] for key in ('init_command', 'verify_command', 'test_command', 'lint_command')}
    except (OSError, ValueError, KeyError, TypeError):
        return ['.agents/config.json: invalid JSON or missing ai-server commands']
    errors = []
    location = environment.get('path', './')
    if not isinstance(location, str) or (root / location).resolve() != root:
        errors.append('ai-server path: expected repository root')
    for name, command in commands.items():
        try:
            args = shlex.split(command) if isinstance(command, str) else []
        except ValueError:
            args = []
        if name in ('init_command', 'verify_command'):
            if len(args) != 2 or args[0] != 'bash':
                errors.append(f'{name}: expected bash <repository hook>')
                continue
            script = (root / args[1]).resolve()
            if not script.is_relative_to(root / '.agents/hooks') or not script.is_file():
                errors.append(f'{name}: missing repository hook: {args[1]}')
        else:
            # These fields describe the full checks in test.sh, not arbitrary commands.
            target = ['pytest', '-q', 'tests'] if name == 'test_command' else ['ruff', 'check', '.']
            expected = ['uv', '--no-cache', 'run', '--no-sync', '--offline', *target]
            if args != expected:
                errors.append(f'{name}: expected {shlex.join(expected)}')
    return errors


def check_harness(root: Path) -> list[str]:
    """Return actionable errors from active harness files, excluding historical logs."""
    root = root.resolve()
    harness = root / '.agents'
    errors = check_config(root)
    documents = [root / 'AGENTS.md', root / 'README.md', harness / 'AGENTS.md', harness / 'system.md']
    documents.extend(harness.glob('agents/*.md'))
    documents.extend(harness.glob('templates/*.md'))
    for directory in sorted((harness / 'skills').iterdir()):
        if not directory.is_dir():
            continue
        skill = directory / 'SKILL.md'
        if not skill.is_file():
            errors.append(f'{skill.relative_to(root)}: missing SKILL.md')
            continue
        errors.extend(f'{skill.relative_to(root)}: {error}' for error in check_skill(skill))
        documents.append(skill)
    for document in documents:
        if not document.is_file():
            errors.append(f'{document.relative_to(root)}: missing document')
            continue
        errors.extend(f'{document.relative_to(root)}: {error}' for error in check_links(document, root))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        errors = check_harness(args.root)
    except (OSError, ValueError) as error:
        errors = [str(error)]
    if errors:
        print('\n'.join(f'HARNESS FAIL: {error}' for error in errors))
        return 1
    print('HARNESS PASS: skill metadata, inline file links, configured hook paths')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

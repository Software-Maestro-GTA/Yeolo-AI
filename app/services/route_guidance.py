"""Format optional provider navigation steps without creating route facts."""

import html
import re
import unicodedata
from typing import Any

ROUTE_METRIC_FIELDS = frozenset({'routes.duration', 'routes.distanceMeters'})
ROUTE_GUIDANCE_FIELDS = frozenset({
    'routes.legs.steps.navigationInstruction.instructions',
    'routes.legs.steps.travelMode',
    'routes.legs.steps.transitDetails.stopDetails.departureStop.name',
    'routes.legs.steps.transitDetails.stopDetails.arrivalStop.name',
    'routes.legs.steps.transitDetails.transitLine.name',
    'routes.legs.steps.transitDetails.transitLine.nameShort',
    'routes.legs.steps.transitDetails.headsign',
})
ROUTE_FIELD_MASK = ','.join(sorted(ROUTE_METRIC_FIELDS | ROUTE_GUIDANCE_FIELDS))
MAX_MEMO_LENGTH = 240


def _clean(value: Any) -> str:
    """Sanitize whole facts; discard oversized inputs rather than cut names."""
    if not isinstance(value, str) or len(value) > 8000:
        return ''
    value = html.unescape(value)
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
    value = re.sub(r'<[^>]*>', '', value)
    value = re.sub(r'https?://\S+', '', value)
    value = ''.join(char if ord(char) >= 32 and ord(char) != 127 else ' ' for char in value)
    return ' '.join(value.split())


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def guidance_matches_language(value: str, language: str, *, allow_line_codes: bool = False) -> bool:
    """Reject incompatible writing systems without translating navigation facts.

    Args:
        value: Complete supplied or formatted navigation text.
        language: Requested BCP 47 locale.
        allow_line_codes: Permit short uppercase identifiers in transit copy.
    Returns:
        Whether supported copy uses its expected script. Transit line codes
        and measured units remain valid identifiers. This is a script guard,
        not a language classifier for
        languages that share the same writing system.
    """
    text = unicodedata.normalize('NFKC', _clean(value))
    base = language.split('-', 1)[0].lower()
    latin_languages = {'en', 'fr', 'es', 'de', 'pt'}
    if base not in {'ko', 'ja', 'zh'} | latin_languages:
        # Preserve the previous localization safeguard for other locales.
        return bool(text) and not any('HANGUL' in unicodedata.name(char, '') for char in text)
    # Keep identifiers such as JR, MRT, G09 and M2.1, never transliterate names.
    tokens = re.findall(r'[A-Za-z][A-Za-z0-9]*(?:[./-][A-Za-z0-9]+)*', text) if allow_line_codes else []
    for token in set(tokens):
        if re.fullmatch(r'[A-Z]{1,3}|[A-Z][A-Z0-9./-]*\d[A-Z0-9./-]*', token) and len(token) <= 16:
            text = re.sub(r'(?<![A-Za-z0-9])' + re.escape(token) + r'(?![A-Za-z0-9])', '', text)
    text = re.sub(r'(?<=\d)\s*(?:km|m)(?![A-Za-z])', '', text)
    letters = [unicodedata.name(char, '') for char in text if unicodedata.category(char).startswith('L')]
    if not letters:
        return False
    allowed = {
        'ko': ('HANGUL',),
        'ja': ('HIRAGANA', 'KATAKANA', 'CJK UNIFIED IDEOGRAPH', 'CJK COMPATIBILITY IDEOGRAPH', 'IDEOGRAPHIC'),
        'zh': ('CJK UNIFIED IDEOGRAPH', 'CJK COMPATIBILITY IDEOGRAPH'),
    }.get(base, ('LATIN',))
    return all(name.startswith(allowed) for name in letters)


def _sentence(instruction: str) -> str:
    """Keep a supplied instruction whole, adding only terminal punctuation."""
    return instruction if instruction.endswith(('.', '!', '?', '。', '！', '？')) else instruction + '.'


def _object_particle(name: str) -> str:
    """Select the object particle for a complete displayed Korean line name."""
    last = ord(name[-1])
    return '을' if name[-1] in '013678LMNR' or 0xAC00 <= last <= 0xD7A3 and (last - 0xAC00) % 28 else '를'


def _memo(actions: list[str]) -> str:
    """Connect complete chronological actions in one paragraph."""
    return ' '.join(actions)


def navigation_source(data: dict) -> list[dict[str, str]]:
    """Retain complete source actions before optional language filtering.

    Args:
        data: The same provider route response used for measured metrics.
    Returns:
        At most three chronological actions, including every transit connection.
        Malformed or excessive connections return no optional source. Original
        stop names, directions and walking instructions are never translated or
        sliced here. The existing final-walk/approach selection is preserved.
    """
    routes = _mapping(data).get('routes')
    legs = _mapping(routes[0]).get('legs') if isinstance(routes, list) and routes else None
    if not isinstance(legs, list) or len(legs) > 20:
        return []
    actions = []
    for leg in legs:
        steps = _mapping(leg).get('steps')
        if not isinstance(steps, list) or len(steps) > 120:
            return []
        for step in steps:
            raw = _mapping(step)
            if raw.get('travelMode') == 'TRANSIT':
                details = _mapping(raw.get('transitDetails'))
                stops = _mapping(details.get('stopDetails'))
                line = _mapping(details.get('transitLine'))
                action = {
                    'kind': 'transit',
                    'start': _clean(_mapping(stops.get('departureStop')).get('name')),
                    'end': _clean(_mapping(stops.get('arrivalStop')).get('name')),
                    'line': _clean(line.get('nameShort')) or _clean(line.get('name')),
                    'direction': _clean(details.get('headsign')),
                }
                if not all(action[key] for key in ('start', 'end', 'line')):
                    return []
            elif raw.get('travelMode') in {'WALK', 'DRIVE', 'BICYCLE', 'TWO_WHEELER'}:
                text = _clean(_mapping(raw.get('navigationInstruction')).get('instructions'))
                if not text:
                    continue
                action = {'kind': 'instruction', 'text': text}
            else:
                return []
            actions.append(action)
    required = [i for i, action in enumerate(actions) if action['kind'] == 'transit']
    if len(required) > 3:
        return []
    selected = set(required)
    optional = [i for i in range(len(actions)) if i not in selected]
    for index in ([optional[-1], optional[0]] + optional) if optional else []:
        if len(selected) < 3:
            selected.add(index)
    result = [actions[i] for i in sorted(selected)]
    return result if sum(len(value) for action in result for value in action.values()) <= 1800 else []


def format_route_guidance(data: dict, mode: str, language: str = 'ko') -> str | None:
    """Summarize supplied navigation, including transit connections and final walk.

    Args:
        data: Already validated route response; optional steps may be malformed.
        mode: Existing transport type.
        language: Requested language; provider instructions must use its script.
    Returns:
        One paragraph of at most 240 characters and three complete action units.
        All transit connections must fit; otherwise None is returned.
        No route fact is inferred and no identifier or instruction is sliced.
        Untranslated transit names require usable provider instructions or later
        translation from retained source actions. Optional foreign walking prose
        is omitted. Missing or unusable navigation returns None.
    """
    routes = _mapping(data).get('routes')
    if not isinstance(routes, list) or not routes:
        return None
    legs = _mapping(routes[0]).get('legs')
    if not isinstance(legs, list):
        return None
    parts: list[tuple[int, str, bool]] = []
    transit_count = 0
    count = 0
    # An unprocessed suffix might hide a required connection; never summarize
    # bounded parsing of such a response as a complete transit route.
    if len(legs) > 20:
        return None
    for leg in legs[:20]:
        steps = _mapping(leg).get('steps')
        if not isinstance(steps, list):
            if mode == 'transit':
                return None
            continue
        if len(steps) > 120:
            return None
        for step in steps[:120]:
            count += 1
            raw = _mapping(step)
            supplied_instruction = _mapping(raw.get('navigationInstruction')).get('instructions')
            instruction = _clean(supplied_instruction)
            transit = raw.get('travelMode') == 'TRANSIT'
            if transit and language.split('-', 1)[0].lower() != 'ko':
                # Navigation instructions were requested from Maps in the user
                # language; never apply Korean sentence templates to foreign text.
                if not instruction:
                    return None
                if not guidance_matches_language(instruction, language, allow_line_codes=True):
                    return None
                transit_count += 1
                if transit_count > 3 or len(instruction) > MAX_MEMO_LENGTH:
                    return None
            elif transit:
                details = _mapping(raw.get('transitDetails'))
                stops = _mapping(details.get('stopDetails'))
                start = _clean(_mapping(stops.get('departureStop')).get('name'))
                end = _clean(_mapping(stops.get('arrivalStop')).get('name'))
                line = _mapping(details.get('transitLine'))
                line_name = _clean(line.get('nameShort')) or _clean(line.get('name'))
                supplied_direction = details.get('headsign')
                direction = _clean(supplied_direction)
                if not all((start, end, line_name)) or (supplied_direction and not direction):
                    return None
                if not guidance_matches_language(f'{start} {end} {line_name} {direction}', language, allow_line_codes=True):
                    if instruction and guidance_matches_language(instruction, language, allow_line_codes=True):
                        transit_count += 1
                        if transit_count > 3 or len(instruction) > MAX_MEMO_LENGTH:
                            return None
                        parts.append((count, _sentence(instruction), True))
                        continue
                    return None
                direction_label = ''
                if direction:
                    direction = re.sub(r'(?<=[가-힣])\.(?=[가-힣])', '·', direction)
                    direction_label = f'({direction})' if direction.endswith(('방면', '방향')) else f'({direction} 방면)'
                line_label = f'{line_name}번' if line_name.isdecimal() else line_name
                if transit_count:
                    last = ord(line_label[-1])
                    consonant = (last - 0xAC00) % 28 if 0xAC00 <= last <= 0xD7A3 else 0
                    particle = '으로' if direction or consonant not in (0, 8) else '로'
                    instruction = f'{start}에서 {line_label}{direction_label}{particle} 환승해 {end}에서 내리세요.'
                else:
                    particle = '을' if direction else _object_particle(line_label)
                    instruction = f'{start}에서 {line_label}{direction_label}{particle} 타고 {end}에서 내리세요.'
                transit_count += 1
                if transit_count > 3 or len(instruction) > MAX_MEMO_LENGTH:
                    return None
            if instruction and not guidance_matches_language(instruction, language, allow_line_codes=transit):
                # Walking access is optional; never mix foreign prose into a
                # complete boarding/transfer chain that is already readable.
                instruction = ''
            if instruction:
                parts.append((count, _sentence(instruction), transit))
            else:
                if mode == 'transit' and not raw:
                    return None
    if not parts:
        return None
    # Transit actions are mandatory. Final walking is the first optional action,
    # followed by the approach and remaining chronological walking instructions.
    connections = [part for part in parts if part[2]]
    if len(_memo([part[1] for part in connections])) > MAX_MEMO_LENGTH:
        return None
    selected: dict[int, str] = {}
    for index, instruction, _ in connections:
        selected[index] = instruction
    optional = [part for part in parts if not part[2]]
    priority = ([optional[-1], optional[0]] + optional) if optional else []
    for index, instruction, _ in priority:
        if index in selected:
            continue
        proposed = selected | {index: instruction}
        lines = [proposed[key] for key in sorted(proposed)]
        if len(lines) <= 3 and len(_memo(lines)) <= MAX_MEMO_LENGTH:
            selected[index] = instruction
    if not selected:
        return None
    lines = [selected[index] for index in sorted(selected)]
    result = _memo(lines)
    return result if len(result) <= MAX_MEMO_LENGTH else None

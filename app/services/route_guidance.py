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


def _line_code(raw: dict) -> str:
    """Keep only a short, supplied line code when stop names are untranslated."""
    line = _mapping(_mapping(raw.get('transitDetails')).get('transitLine'))
    value = _clean(line.get('nameShort')) or _clean(line.get('name'))
    if re.fullmatch(r'[A-Z0-9][A-Z0-9./-]{0,15}', value):
        return value
    return ''


def _transit_code_guidance(legs: list, language: str) -> str | None:
    """Keep every supplied transit line in order, with localized map guidance.

    Missing or excessive connections return generic guidance, so a partial
    list of lines is never presented as the complete transfer sequence.
    """
    fallback = fallback_route_guidance('transit', language)
    codes = []
    for leg in legs:
        steps = _mapping(leg).get('steps')
        if not isinstance(steps, list) or len(steps) > 120:
            return fallback
        for step in steps:
            raw = _mapping(step)
            if raw.get('travelMode') not in {'WALK', 'TRANSIT'}:
                return fallback
            if raw.get('travelMode') != 'TRANSIT':
                continue
            code = _line_code(raw)
            if not code:
                return fallback
            codes.append(code)
            if len(codes) > 3:
                return fallback
    if not codes:
        return fallback
    base = language.split('-', 1)[0].lower()
    if base == 'ko':
        labels = [f'{code}번' if code.isdecimal() else code for code in codes]
        action = f'{labels[0]} 노선을 이용하세요.' if len(labels) == 1 else f'{" → ".join(labels)} 순서로 이용하세요.'
        result = action + ' 승차·하차 위치와 행선지는 지도 앱의 대중교통 길찾기에서 확인하세요.'
    elif base == 'en':
        result = f'Take {" → ".join(codes)} in order. Check boarding and alighting stops and direction in your map app.'
    elif base == 'ja':
        result = f'{" → ".join(codes)}を順に利用してください。乗降場所と行き先は地図アプリの公共交通案内で確認してください。'
    elif base == 'zh':
        traditional = any(part in {'hant', 'tw', 'hk', 'mo'} for part in language.lower().split('-')[1:])
        result = (f'依序搭乘{" → ".join(codes)}路線。請在地圖應用程式的大眾運輸導航中確認上下車站和方向。'
                  if traditional else f'依次搭乘{" → ".join(codes)}线路。请在地图应用的公共交通导航中确认上下车站和方向。')
    else:
        return fallback
    return result if len(result) <= MAX_MEMO_LENGTH else fallback


def fallback_route_guidance(mode: str, language: str = 'ko') -> str | None:
    """Return a generic action without inventing missing navigation details.

    Args:
        mode: Existing transport type.
        language: Requested BCP 47 language; supported generic fallbacks use its base.
    Returns:
        Short localized action, or None when a safe translation is unavailable.
    """
    base = language.split('-', 1)[0].lower()
    if base == 'zh' and any(part in {'hant', 'tw', 'hk', 'mo'} for part in language.lower().split('-')[1:]):
        base = 'zh-Hant'
    actions = {
        'ko': {
            'walking': '지도 앱의 도보 길찾기를 열고 안내된 보행 경로를 따라가세요.',
            'transit': '지도 앱의 대중교통 길찾기를 열고 노선과 행선지를 확인해 타세요.',
            'driving': '내비게이션에 목적지를 설정하고 안내된 차량 경로를 따라가세요.',
            'taxi': '택시 기사에게 목적지를 보여주고 내비게이션의 도착지를 확인하세요.',
        },
        'en': {
            'walking': 'Open walking directions in your map app and follow the route.',
            'transit': 'Open transit directions in your map app and check the line and destination before boarding.',
            'driving': 'Set your destination in navigation and follow the driving route.',
            'taxi': 'Show the driver your destination and check it in navigation.',
        },
        'ja': {
            'walking': '地図アプリで徒歩ルートを開き、案内に沿って進んでください。',
            'transit': '地図アプリで公共交通ルートを開き、路線と行き先を確認して乗車してください。',
            'driving': 'ナビに目的地を設定し、案内されたルートを進んでください。',
            'taxi': '運転手に目的地を見せ、ナビの行き先を確認してください。',
        },
        'zh-Hant': {
            'walking': '在地圖應用程式中開啟步行導航，並沿指示路線前進。',
            'transit': '在地圖應用程式中開啟大眾運輸導航，確認路線和方向後搭車。',
            'driving': '在導航中設定目的地，並沿指示路線行駛。',
            'taxi': '向司機出示目的地，並確認導航中的終點。',
        },
        'zh': {
            'walking': '在地图应用中打开步行导航，并沿指示路线前进。',
            'transit': '在地图应用中打开公共交通导航，确认线路和方向后乘车。',
            'driving': '在导航中设置目的地，并沿指示路线行驶。',
            'taxi': '向司机出示目的地，并确认导航中的终点。',
        },
    }
    if base not in actions:
        return None
    return actions[base].get(mode)


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
        All transit connections must fit; otherwise a generic action is returned.
        No route fact is inferred and no identifier or instruction is sliced.
        Untranslated transit names use supplied line codes and localized map
        guidance. Optional foreign walking prose is omitted. Unsupported locales
        without usable provider instructions return None.
    """
    fallback = fallback_route_guidance(mode, language)
    routes = _mapping(data).get('routes')
    if not isinstance(routes, list) or not routes:
        return fallback
    legs = _mapping(routes[0]).get('legs')
    if not isinstance(legs, list):
        return fallback
    parts: list[tuple[int, str, bool]] = []
    transit_count = 0
    count = 0
    # An unprocessed suffix might hide a required connection; never summarize
    # bounded parsing of such a response as a complete transit route.
    if len(legs) > 20:
        return fallback
    language_fallback = _transit_code_guidance(legs, language) if mode == 'transit' else fallback
    for leg in legs[:20]:
        steps = _mapping(leg).get('steps')
        if not isinstance(steps, list):
            if mode == 'transit':
                return fallback
            continue
        if len(steps) > 120:
            return fallback
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
                    return fallback
                if not guidance_matches_language(instruction, language, allow_line_codes=True):
                    return language_fallback
                transit_count += 1
                if transit_count > 3 or len(instruction) > MAX_MEMO_LENGTH:
                    return fallback
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
                    return fallback
                if not guidance_matches_language(f'{start} {end} {line_name} {direction}', language, allow_line_codes=True):
                    if instruction and guidance_matches_language(instruction, language, allow_line_codes=True):
                        transit_count += 1
                        if transit_count > 3 or len(instruction) > MAX_MEMO_LENGTH:
                            return fallback
                        parts.append((count, _sentence(instruction), True))
                        continue
                    return language_fallback
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
                    return fallback
            if instruction and not guidance_matches_language(instruction, language, allow_line_codes=transit):
                # Walking access is optional; never mix foreign prose into a
                # complete boarding/transfer chain that is already readable.
                instruction = ''
            if instruction:
                parts.append((count, _sentence(instruction), transit))
            else:
                if mode == 'transit' and not raw:
                    return fallback
    if not parts:
        return fallback
    # Transit actions are mandatory. Final walking is the first optional action,
    # followed by the approach and remaining chronological walking instructions.
    connections = [part for part in parts if part[2]]
    if len(_memo([part[1] for part in connections])) > MAX_MEMO_LENGTH:
        return fallback
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
        return fallback
    lines = [selected[index] for index in sorted(selected)]
    result = _memo(lines)
    return result if len(result) <= MAX_MEMO_LENGTH else fallback

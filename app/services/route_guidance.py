"""Format optional provider navigation steps without creating route facts."""

import html
import re
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
    return '을' if 0xAC00 <= last <= 0xD7A3 and (last - 0xAC00) % 28 else '를'


def _memo(actions: list[str]) -> str:
    """Connect complete chronological actions in one paragraph."""
    return ' '.join(actions)


def format_route_guidance(data: dict, mode: str, language: str = 'ko') -> str | None:
    """Summarize supplied navigation, including transit connections and final walk.

    Args:
        data: Already validated route response; optional steps may be malformed.
        mode: Existing transport type.
        language: Requested language; non-Korean transit uses provider instructions.
    Returns:
        One paragraph of at most 240 characters and three complete action units.
        All transit connections must fit; otherwise a generic action is returned.
        No route fact is inferred and no identifier or instruction is sliced.
        Unsupported languages without usable provider instructions return None.
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

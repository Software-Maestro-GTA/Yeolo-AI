"""Format optional provider navigation steps without creating route facts."""

import html
import re
from datetime import datetime
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
MAX_MEMO_LENGTH = 1400


def _clean(value: Any, limit: int = 160) -> str:
    """Remove external markup, links and terminal controls before display."""
    if not isinstance(value, str):
        return ''
    value = html.unescape(value[:8000])
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
    value = re.sub(r'<[^>]*>', '', value)
    value = re.sub(r'https?://\S+', '', value)
    value = ''.join(char if ord(char) >= 32 and ord(char) != 127 else ' ' for char in value)
    return ' '.join(value.split())[:limit]


def _mapping(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def fallback_route_guidance(mode: str, destination_name: str, departure_time: datetime | None = None) -> str:
    """Return a generic action without inventing missing navigation details.

    Args:
        mode: Existing transport type.
        destination_name: Verified destination name.
        departure_time: Actual aware transit query departure, if supplied.
    Returns:
        Bounded memo with the actual query basis and no repeated metrics.
    """
    label = {'walking': '도보', 'transit': '대중교통', 'driving': '차량', 'taxi': '택시'}.get(mode, '이동')
    name = _clean(destination_name, 100) or '다음 방문 장소'
    timing = '조회 시점 기준 이동 안내입니다.'
    if mode == 'transit' and departure_time is not None:
        timing = f'{departure_time.isoformat(sep=" ", timespec="minutes")} 출발 기준 예상 이동 안내입니다.'
    action = {
        'walking': '지도 앱에서 목적지를 설정해 도보 길찾기를 열고, 안내된 보행 경로를 따라가세요.',
        'transit': '지도 앱의 대중교통 길찾기를 열고, 탑승 전 노선과 행선지 표시를 안내와 대조하세요.',
        'driving': '내비게이션의 도착지 이름을 목적지와 대조한 뒤 안내된 차량 경로를 따라가세요.',
        'taxi': '탑승 시 기사에게 목적지 이름을 보여주고 내비게이션의 도착지와 대조하세요.',
    }.get(mode, '지도 앱에서 도착지 이름을 목적지와 대조하며 이동하세요.')
    return f'{name}까지 {label} 이동입니다. {action} {timing}'


def format_route_guidance(data: dict, mode: str, destination_name: str, departure_time: datetime | None = None) -> str:
    """Summarize supplied navigation, including transit connections and final walk.

    Args:
        data: Already validated route response; optional steps may be malformed.
        mode: Existing transport type.
        destination_name: Verified arrival venue name.
        departure_time: Actual transit query departure, when supplied.
    Returns:
        Sanitized memo of at most 1,400 characters. Missing or omitted steps are
        marked as a summary; no stop, line, direction or maneuver is inferred.
    """
    fallback = fallback_route_guidance(mode, destination_name, departure_time)
    routes = _mapping(data).get('routes')
    if not isinstance(routes, list) or not routes:
        return fallback
    legs = _mapping(routes[0]).get('legs')
    if not isinstance(legs, list):
        return fallback
    parts: list[tuple[int, str, bool]] = []
    partial = False
    boarded = False
    count = 0
    for leg in legs[:20]:
        steps = _mapping(leg).get('steps')
        if not isinstance(steps, list):
            partial = True
            continue
        for step in steps[:120]:
            count += 1
            raw = _mapping(step)
            supplied_instruction = _mapping(raw.get('navigationInstruction')).get('instructions')
            instruction = _clean(supplied_instruction)
            partial |= len(_clean(supplied_instruction, 8000)) > len(instruction)
            transit = raw.get('travelMode') == 'TRANSIT'
            if transit:
                details = _mapping(raw.get('transitDetails'))
                stops = _mapping(details.get('stopDetails'))
                start = _clean(_mapping(stops.get('departureStop')).get('name'), 60)
                end = _clean(_mapping(stops.get('arrivalStop')).get('name'), 60)
                line = _mapping(details.get('transitLine'))
                supplied_line = line.get('nameShort') if _clean(line.get('nameShort')) else line.get('name')
                line_name = _clean(supplied_line, 60)
                direction = _clean(details.get('headsign'), 60)
                for supplied, cleaned in (
                    (_mapping(stops.get('departureStop')).get('name'), start),
                    (_mapping(stops.get('arrivalStop')).get('name'), end),
                    (supplied_line, line_name), (details.get('headsign'), direction),
                ):
                    partial |= len(_clean(supplied, 8000)) > len(cleaned)
                partial |= not all((start, end, line_name))
                if start and line_name and end:
                    action = '환승해 탑승하세요' if boarded else '탑승하세요'
                    direction_label = f' ({direction} 방향)' if direction else ''
                    instruction = f'{start}에서 {line_name}{direction_label}에 {action}. {end}에서 하차하세요.'
                else:
                    # Known fragments are useful, but never join incomplete facts
                    # into a purported boarding-to-alighting instruction.
                    facts = [value for value in (start, line_name, direction, end) if value]
                    instruction = '대중교통 구간 정보: ' + ' · '.join(facts) if facts else ''
                boarded = True
            if instruction:
                parts.append((count, instruction, transit))
            else:
                partial = True
        partial |= len(steps) > 120
    partial |= len(legs) > 20
    if not parts:
        return fallback
    prefix = f'{_clean(destination_name, 100) or "다음 방문 장소"}까지 이동 안내: '
    timing = fallback.rsplit('. ', 1)[-1]
    # Prioritize every available transit connection, then the first approach
    # and final walking instruction; restore chronological order for display.
    priority = [part for part in parts if part[2]]
    priority += [parts[0], parts[-1]]
    priority += parts
    selected: dict[int, str] = {}
    budget = MAX_MEMO_LENGTH - len(prefix) - len(timing) - 90
    for index, instruction, _ in priority:
        if index in selected:
            continue
        if sum(len(value) + 3 for value in selected.values()) + len(instruction) + 3 <= budget:
            selected[index] = instruction
    partial |= len(selected) < len(parts)
    summary = '주요 구간 요약(일부 상세 안내 생략): ' if partial else ''
    if partial:
        timing = '전체 이동은 지도 길찾기의 안내를 따라가세요. ' + timing
    body = ' → '.join(selected[index] for index in sorted(selected))
    if body and body[-1] not in '.!?。！？':
        body += '.'
    return f'{prefix}{summary}{body} {timing}'[:MAX_MEMO_LENGTH]

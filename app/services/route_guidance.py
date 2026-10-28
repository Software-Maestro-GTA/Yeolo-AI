"""Format sanitized provider navigation without translation or Korean templates."""

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


def _memo(actions: list[str]) -> str:
    """Join whole provider actions in chronological order."""
    return ' '.join(actions)


def format_route_guidance(data: dict, mode: str) -> str | None:
    """Select original provider actions without filtering their language.

    Args:
        data: Route response already validated for distance and duration.
        mode: Requested public transport type; optional guidance never sets metrics.
    Returns:
        Up to three complete actions within 240 characters, including every
        transit connection. Original instruction wording is kept after markup
        and control-character cleanup. If a transit instruction is absent,
        show supplied line/direction/stop names with neutral separators only.
        Missing essential connections or oversized guidance returns None.
    """
    routes = _mapping(data).get('routes')
    if not isinstance(routes, list) or not routes:
        return None
    legs = _mapping(routes[0]).get('legs')
    if not isinstance(legs, list) or len(legs) > 20:
        return None
    parts: list[tuple[int, str, bool]] = []
    transit_count = 0
    count = 0
    for leg in legs:
        steps = _mapping(leg).get('steps')
        if not isinstance(steps, list) or len(steps) > 120:
            return None
        for step in steps:
            count += 1
            raw = _mapping(step)
            if mode == 'transit' and raw.get('travelMode') not in {'TRANSIT', 'WALK', 'DRIVE', 'BICYCLE', 'TWO_WHEELER'}:
                return None
            instruction = _clean(_mapping(raw.get('navigationInstruction')).get('instructions'))
            transit = raw.get('travelMode') == 'TRANSIT'
            if transit:
                transit_count += 1
                if transit_count > 3:
                    return None
                if not instruction:
                    details = _mapping(raw.get('transitDetails'))
                    stops = _mapping(details.get('stopDetails'))
                    line = _mapping(details.get('transitLine'))
                    start = _clean(_mapping(stops.get('departureStop')).get('name'))
                    end = _clean(_mapping(stops.get('arrivalStop')).get('name'))
                    label = _clean(line.get('nameShort')) or _clean(line.get('name'))
                    direction = _clean(details.get('headsign'))
                    if not all((start, end, label)):
                        return None
                    instruction = f'{label} ({direction}): {start} → {end}' if direction else f'{label}: {start} → {end}'
                if len(instruction) > MAX_MEMO_LENGTH:
                    return None
            if instruction:
                parts.append((count, instruction, transit))
            elif mode == 'transit' and not raw:
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

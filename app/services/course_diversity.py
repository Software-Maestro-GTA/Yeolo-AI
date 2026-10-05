"""Compare remembered provider IDs and independently generated planning choices."""

import unicodedata
from collections.abc import Iterable
from typing import Literal

type Experience = Literal['nature', 'culture', 'history', 'shopping', 'food', 'relaxation', 'activity']
EXPERIENCES = {'nature', 'culture', 'history', 'shopping', 'food', 'relaxation', 'activity'}
MAX_OVERLAP = .4


def normalize_area(value: str) -> str:
    """Normalize planning text without claiming geographic alias equivalence."""
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def _overlap(current: set[str], previous: set[str]) -> float:
    return len(current & previous) / min(len(current), len(previous)) if current and previous else 0.


def overlap_scores(place_ids: Iterable[str], attraction_ids: Iterable[str], recent_profiles: list[dict]) -> dict[str, float]:
    """Return maximum individual-history overlap, including subset repetition.

    Args:
        place_ids: Selected provider IDs for the proposed course.
        attraction_ids: Selected non-meal attraction IDs.
        recent_profiles: Previous courses; old ID-only entries remain supported.
    Returns:
        Maximum place and comparable attraction overlaps, between zero and one.
    """
    places, attractions = set(place_ids), set(attraction_ids)
    return {
        'place_overlap': max((_overlap(places, set(item.get('place_ids', []))) for item in recent_profiles), default=0.),
        'attraction_overlap': max((_overlap(attractions, set(item.get('attraction_ids', []))) for item in recent_profiles), default=0.),
    }


def planning_overlap(values: Iterable[str], profiles: list[dict], field: str) -> float:
    """Compare model planning labels; these are preferences, not map facts."""
    current = {normalize_area(value) for value in values if value.strip()}
    return max((_overlap(current, {normalize_area(value) for value in item.get(field, []) if isinstance(value, str) and value.strip()}) for item in profiles), default=0.)


def clean_metadata(metadata: dict | None, place_ids: set[str]) -> dict:
    """Whitelist IDs and bounded model planning text; discard provider content."""
    source = metadata or {}
    areas = source.get('areas', [])
    experiences = source.get('experiences', [])
    return {
        'attraction_ids': sorted(set(source.get('attraction_ids', [])) & place_ids),
        'areas': sorted({normalize_area(value)[:100] for value in areas if isinstance(value, str) and value.strip()})[:30],
        'experiences': sorted({value for value in experiences if isinstance(value, str) and value in EXPERIENCES}),
    }

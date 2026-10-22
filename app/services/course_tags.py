"""Label verified course categories without describing expected visit experiences."""

from collections import Counter

from app.schemas.course import DayItinerarySchema
from app.services.place_categories import map_actual_place_category

# Stable category nouns, never visit advice or evidence of a specific activity.
_CATEGORY_TAGS = {
    'FOOD': '음식',
    'CAFE': '카페',
    'SPOT': '명소',
    'CULTURE': '문화',
    'NATURE': '자연',
    'ACTIVITY': '활동',
    'NIGHTLIFE': '밤문화',
}

def verified_course_tags(days: list[DayItinerarySchema]) -> list[str]:
    """Return up to five category nouns for the most frequent verified groups.

    Args:
        days: Final verified visits. Provider types use the public category mapper.
    Returns:
        Korean category tags ordered by frequency, then first appearance.
        Airport and lodging stops are excluded. Visit reason/memo stay unchanged.
    """
    groups = Counter(
        map_actual_place_category(stop.place.category, getattr(stop.place, 'providerTypes', ()))
        for day in days for stop in day.stops
    )
    return [_CATEGORY_TAGS[group] for group, _ in groups.most_common() if group in _CATEGORY_TAGS][:5]

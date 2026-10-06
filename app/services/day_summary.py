"""Compose a short day mood from verified place categories without model calls."""

from collections import Counter

from app.agent.tools.verified_maps import meal_category_supported
from app.schemas.course import StopSchema
from app.services.course_reasons import CAFE, CULTURE, NATURE, SHOPPING, WELLNESS

MAX_DAY_SUMMARY_LENGTH = 200

# Action stems describe opportunities supported by a place type, not venue
# scenery, weather, opening conditions, or the pace of the scheduled itinerary.
_ACTIONS = {
    'exhibition': '전시 공간을 둘러보',
    'culture': '문화 공간을 만나',
    'nature': '자연의 풍경을 만나',
    'view': '전망을 바라보',
    'viewing': '관람 공간을 둘러보',
    'shopping': '마음에 드는 물건을 골라보',
    'wellness': '몸을 돌보',
    'recreation': '놀이와 체험을 즐기',
    'food': '먹고 싶은 음식과 음료를 골라보',
    'visit': '새로운 장소를 만나',
}
_ENDINGS = (
    '눈길이 머무는 순간을 여행의 기억으로 남겨보세요.',
    '마음에 남는 장면으로 오늘의 여행을 채워보세요.',
    '오늘만의 여행 이야기를 하나씩 만들어보세요.',
)


def _theme(category: str) -> str:
    """Translate only a verified provider category into a conservative activity."""
    if category in {'museum', 'art_gallery', 'heritage_museum', 'history_museum'}:
        return 'exhibition'
    if category in CULTURE or category == 'library':
        return 'culture'
    if category in NATURE:
        return 'nature'
    if category == 'observation_deck':
        return 'view'
    if category in {'aquarium', 'zoo', 'planetarium'}:
        return 'viewing'
    if category in SHOPPING:
        return 'shopping'
    if category in WELLNESS:
        return 'wellness'
    if category in {'amusement_park', 'theme_park', 'water_park'}:
        return 'recreation'
    if category in CAFE or meal_category_supported(category, 'lunch'):
        return 'food'
    return 'visit'


def compose_day_summary(stops: list[StopSchema], day_number: int, *, compact: bool = False) -> str:
    """Describe actual day experiences in one or two natural Korean sentences.

    Args:
        stops: Final response stops containing verified provider categories.
        day_number: One-based day number selecting a stable wording variant.
        compact: Whether to disclose the actual count of a compact itinerary.

    Returns:
        A short emotional summary; names, draft prose, and assumed traits are
        never used. No model or provider calls are made and stops are unchanged.
    """
    counts = Counter(_theme(stop.place.category) for stop in stops)
    # Compulsory meals and unknown categories must not overwhelm actual
    # sightseeing experiences. Ties follow a stable, explicit theme order.
    core = {theme: count for theme, count in counts.items() if theme not in {'food', 'visit'}}
    available = core or ({'food': counts['food']} if counts['food'] else {'visit': 1})
    ranked = sorted(available, key=lambda theme: (-available[theme], tuple(_ACTIONS).index(theme)))[:2]
    action = '고 '.join(_ACTIONS[theme] for theme in ranked) + '며'
    variant = (day_number - 1) % len(_ENDINGS)
    summary = f'{action} {_ENDINGS[variant]}'
    if compact:
        summary += f' 오늘은 이동과 식사 시간을 고려해 {len(stops)}곳을 둘러보세요.'
    return summary

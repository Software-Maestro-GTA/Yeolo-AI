"""Label verified course categories without describing expected visit experiences."""

from collections import Counter

from app.schemas.course import DayItinerarySchema
from app.services.place_categories import map_actual_place_category

# Stable category nouns, never visit advice or evidence of a specific activity.
_TAG_LANGUAGES = ('ko', 'en', 'ja', 'zh', 'zh-Hant', 'fr', 'es', 'de', 'pt')
_CATEGORY_TAGS = {
    'FOOD': ('음식', 'Food', '食事', '餐饮', '餐飲', 'Restauration', 'Comida', 'Essen', 'Alimentação'),
    'CAFE': ('카페', 'Cafés', 'カフェ', '咖啡馆', '咖啡館', 'Cafés', 'Cafés', 'Cafés', 'Cafés'),
    'SPOT': ('명소', 'Places of interest', 'スポット', '景点', '景點', 'Sites', 'Lugares de interés', 'Sehenswürdigkeiten', 'Pontos de interesse'),
    'CULTURE': ('문화', 'Culture', '文化', '文化', '文化', 'Culture', 'Cultura', 'Kultur', 'Cultura'),
    'NATURE': ('자연', 'Nature', '自然', '自然', '自然', 'Nature', 'Naturaleza', 'Natur', 'Natureza'),
    'ACTIVITY': ('활동', 'Activities', 'アクティビティ', '活动', '活動', 'Activités', 'Actividades', 'Aktivitäten', 'Atividades'),
    'NIGHTLIFE': ('밤문화', 'Nightlife', 'ナイトライフ', '夜生活', '夜生活', 'Vie nocturne', 'Vida nocturna', 'Nachtleben', 'Vida noturna'),
}


def verified_course_tags(days: list[DayItinerarySchema], *, language: str = 'ko') -> list[str]:
    """Return up to five category nouns for the most frequent verified groups.

    Args:
        days: Final verified visits. Provider types use the public category mapper.
        language: Display locale; unsupported languages use English category names.
    Returns:
        Localized category tags ordered by frequency, then first appearance.
        Airport and lodging stops are excluded. Visit reason/memo stay unchanged.
    """
    parts = language.lower().replace('_', '-').split('-')
    locale = 'zh-Hant' if parts[0] == 'zh' and any(part in {'hant', 'tw', 'hk', 'mo'} for part in parts[1:]) else parts[0]
    column = _TAG_LANGUAGES.index(locale if locale in _TAG_LANGUAGES else 'en')
    groups = Counter(
        map_actual_place_category(stop.place.category, getattr(stop.place, 'providerTypes', ()))
        for day in days for stop in day.stops
    )
    return [_CATEGORY_TAGS[group][column] for group, _ in groups.most_common() if group in _CATEGORY_TAGS][:5]

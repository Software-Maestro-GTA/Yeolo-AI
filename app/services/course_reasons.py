"""Select explicit preference evidence without generating display copy."""

from dataclasses import dataclass

from app.agent.tools.verified_maps import meal_category_supported
from app.schemas.taste_profile import TasteProfileSchema

CULTURE = frozenset({'museum', 'art_gallery', 'cultural_center', 'historical_landmark', 'historical_place', 'monument', 'heritage_museum', 'history_museum', 'buddhist_temple', 'shinto_shrine', 'hindu_temple', 'mosque', 'church', 'synagogue'})
NATURE = frozenset({'park', 'national_park', 'state_park', 'nature_preserve', 'botanical_garden', 'garden', 'hiking_area', 'beach', 'wildlife_park'})
VIEWING = CULTURE | frozenset({'aquarium', 'zoo', 'planetarium', 'observation_deck', 'botanical_garden'})
CAFE = frozenset({'cafe', 'coffee_shop', 'tea_house', 'bakery', 'dessert_shop', 'dessert_restaurant', 'ice_cream_shop', 'chocolate_shop', 'pastry_shop'})
SHOPPING = frozenset({'shopping_mall', 'department_store', 'shopping_center', 'clothing_store', 'book_store', 'gift_shop', 'souvenir_store', 'market'})
WELLNESS = frozenset({'spa', 'wellness_center', 'massage', 'sauna', 'yoga_studio'})
NIGHTLIFE = frozenset({'night_club', 'bar', 'pub', 'wine_bar', 'dance_club'})


@dataclass(frozen=True)
class PreferenceRule:
    """An explicit profile score and a conservative set of compatible place types."""

    section: str
    field: str
    label: str
    categories: frozenset[str]


RULES = (
    PreferenceRule('travelPurpose', 'culturalExperience', '문화 체험', CULTURE),
    PreferenceRule('activityPreference', 'viewing', '관람', VIEWING),
    PreferenceRule('foodPreference', 'cafeDessert', '카페·디저트', CAFE),
    PreferenceRule('travelPurpose', 'natureExploration', '자연 탐방', NATURE),
    PreferenceRule('travelPurpose', 'gourmet', '미식', frozenset({'restaurant'})),
    PreferenceRule('activityPreference', 'gourmetExploration', '미식 탐방', frozenset({'restaurant'})),
    PreferenceRule('travelPurpose', 'shopping', '쇼핑', SHOPPING),
    PreferenceRule('activityPreference', 'shopping', '쇼핑', SHOPPING),
    PreferenceRule('travelPurpose', 'relaxation', '휴식', NATURE | CAFE | WELLNESS),
    PreferenceRule('activityPreference', 'relaxation', '휴식', NATURE | CAFE | WELLNESS),
    PreferenceRule('travelPurpose', 'wellness', '웰니스', WELLNESS),
    PreferenceRule('activityPreference', 'nightlife', '밤문화', NIGHTLIFE),
    PreferenceRule('travelPurpose', 'sightseeing', '관광', VIEWING | NATURE | frozenset({'tourist_attraction'})),
    PreferenceRule('travelPurpose', 'selfDevelopment', '배움', CULTURE | frozenset({'library'})),
    PreferenceRule('preferredLocationType', 'beachResort', '해변 방문', frozenset({'beach'})),
    PreferenceRule('preferredLocationType', 'historicalCity', '역사 공간 방문', frozenset({'historical_landmark', 'historical_place', 'monument', 'heritage_museum', 'history_museum'})),
    PreferenceRule('preferredLocationType', 'themeParkResort', '테마파크 방문', frozenset({'amusement_park', 'theme_park', 'water_park'})),
)


def preference_evidence(profile: TasteProfileSchema, category: str) -> list[tuple[int, PreferenceRule]]:
    """Select explicit high preferences compatible with an actual provider type.

    Args:
        profile: Supplied user preference scores.
        category: Verified provider category, never a model proposal.
    Returns:
        Matching scores and rules with scores at least four; no display prose.
    """
    matches = []
    for rule in RULES:
        score = getattr(getattr(profile, rule.section), rule.field)
        compatible = category in rule.categories
        if rule.field in {'gourmet', 'gourmetExploration'}:
            compatible = compatible or meal_category_supported(category, 'lunch')
        if score >= 4 and compatible:
            matches.append((score, rule))
    return matches

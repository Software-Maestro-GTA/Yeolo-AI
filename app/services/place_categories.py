"""Map verified provider place types to visit, lodging and airport categories.

This presentation mapping makes no provider or model calls. Planning and meal
validation keep using the original verified type; display groups do not establish
popularity, a night view, facilities or the availability of a particular activity.
"""

from collections.abc import Collection
from typing import Literal, cast

PlaceCategoryCode = Literal[
    'FOOD', 'CAFE', 'SPOT', 'CULTURE', 'NATURE', 'ACTIVITY', 'NIGHTLIFE',
    'LODGING', 'AIRPORT',
]

PLACE_CATEGORY_LABELS: dict[PlaceCategoryCode, str] = {
    'FOOD': '맛집·미식',
    'CAFE': '카페·디저트',
    'SPOT': '스팟·명소',
    'CULTURE': '문화·전시',
    'NATURE': '자연·산책',
    'ACTIVITY': '액티비티·체험',
    'NIGHTLIFE': '야경·나이트',
    'LODGING': '숙소',
    'AIRPORT': '공항',
}

LODGING_TYPES = frozenset({
    'lodging', 'hotel', 'resort_hotel', 'motel', 'hostel', 'guest_house',
    'bed_and_breakfast', 'extended_stay_hotel', 'inn',
})

AIRPORT_TYPES = frozenset({'airport', 'international_airport'})

_CATEGORY_TYPES: dict[PlaceCategoryCode, frozenset[str]] = {
    'FOOD': frozenset({
        'restaurant', 'food_court', 'meal_takeaway', 'noodle_shop',
        'sandwich_shop', 'market',
    }),
    'CAFE': frozenset({
        'cafe', 'coffee_shop', 'tea_house', 'bakery', 'dessert_shop',
        'dessert_restaurant', 'ice_cream_shop', 'chocolate_shop', 'pastry_shop',
    }),
    'SPOT': frozenset({
        'observation_deck', 'tourist_attraction', 'landmark', 'aquarium', 'zoo',
        'shopping_mall', 'department_store', 'shopping_center', 'clothing_store',
        'book_store', 'gift_shop', 'souvenir_store',
    }),
    'CULTURE': frozenset({
        'museum', 'art_gallery', 'cultural_center', 'historical_landmark',
        'historical_place', 'monument', 'heritage_museum', 'history_museum',
        'buddhist_temple', 'shinto_shrine', 'hindu_temple', 'mosque', 'church',
        'synagogue', 'library', 'planetarium', 'convention_center',
        'performing_arts_theater', 'movie_theater',
    }),
    'NATURE': frozenset({
        'park', 'national_park', 'state_park', 'nature_preserve',
        'botanical_garden', 'garden', 'hiking_area', 'beach', 'wildlife_park',
    }),
    'ACTIVITY': frozenset({
        'amusement_park', 'theme_park', 'water_park', 'sports_complex', 'stadium',
        'bowling_alley', 'golf_course', 'ski_resort', 'spa', 'wellness_center',
        'massage', 'sauna', 'yoga_studio',
    }),
    'NIGHTLIFE': frozenset({
        'night_club', 'bar', 'pub', 'wine_bar', 'dance_club',
    }),
    'LODGING': LODGING_TYPES,
    'AIRPORT': AIRPORT_TYPES,
}

_TYPE_TO_CATEGORY: dict[str, PlaceCategoryCode] = {
    place_type: category
    for category, place_types in _CATEGORY_TYPES.items()
    for place_type in place_types
}


def map_actual_place_category(category: str, provider_types: Collection[str] = ()) -> PlaceCategoryCode:
    """Group a verified raw provider type for the public place response.

    Args:
        category: Actual provider type selected during venue verification.
            A proposed category, place name or visit time is not evidence.
        provider_types: Additional types returned in the same Essentials lookup.
            Only a generic tourist attraction needs these for a more specific group.

    Returns:
        A stable app category code. Existing public codes remain unchanged.
        Named restaurant subtypes map to ``FOOD``;
        ``dessert_restaurant`` maps to ``CAFE`` before that suffix rule. Unmapped
        types use the general ``SPOT`` group without claiming a special trait.
    """
    if category in PLACE_CATEGORY_LABELS:
        return cast(PlaceCategoryCode, category)
    if category == 'tourist_attraction':
        for group in ('CULTURE', 'NATURE', 'ACTIVITY', 'NIGHTLIFE', 'CAFE', 'FOOD'):
            if any(kind in _CATEGORY_TYPES[group] for kind in provider_types):
                return cast(PlaceCategoryCode, group)
    mapped = _TYPE_TO_CATEGORY.get(category)
    if mapped is not None:
        return mapped
    if category.endswith('_restaurant'):
        return 'FOOD'
    return 'SPOT'

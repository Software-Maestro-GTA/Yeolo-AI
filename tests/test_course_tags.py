"""Verify category-only tags without creating or changing visit descriptions."""

import pytest

from app.services.course_localization import _fallback
from app.services.course_tags import verified_course_tags
from tests.test_course_localization import course, request
from tests.test_course_reasons import content_day


@pytest.mark.parametrize('categories,expected', [
    (('museum', 'art_gallery', 'library', 'shinto_shrine', 'buddhist_temple'), ['문화']),
    (('observation_deck', 'tourist_attraction', 'shopping_mall'), ['명소']),
    (('restaurant', 'ramen_restaurant', 'sushi_restaurant', 'hamburger_restaurant'), ['음식']),
    (('park', 'national_park'), ['자연']),
    (('cafe', 'bakery'), ['카페']),
    (('spa', 'theme_park'), ['활동']),
    (('bar', 'night_club'), ['밤문화']),
    (('hotel', 'airport'), []),
])
def test_tags_only_name_verified_groups_without_describing_an_activity(categories, expected):
    day = content_day(*categories)
    before = day.model_dump()
    assert verified_course_tags([day]) == expected
    assert day.model_dump() == before


def test_tag_frequency_limit_and_ties_follow_verified_public_categories():
    day = content_day('airport', 'hotel', 'cafe', 'park', 'museum', 'restaurant',
                      'theme_park', 'night_club', 'tourist_attraction', 'art_gallery')
    assert verified_course_tags([day]) == ['문화', '카페', '자연', '음식', '활동']


def test_tag_mapping_uses_additional_verified_types_for_generic_attractions():
    from app.agent.tools.verified_maps import InternalPlaceSchema

    day = content_day('tourist_attraction')
    day.stops[0].place = InternalPlaceSchema(
        **day.stops[0].place.model_dump(exclude={'providerTypes'}), providerTypes=['park'],
    )
    assert verified_course_tags([day]) == ['자연']


@pytest.mark.parametrize('language,expected', [
    ('ko-KR', ['문화', '음식']), ('en', ['Culture', 'Food']),
    ('ja', ['文化', '食事']), ('zh-CN', ['文化', '餐饮']),
    ('zh-TW', ['文化', '餐飲']), ('fr', ['Culture', 'Restauration']),
    ('es', ['Cultura', 'Comida']), ('de', ['Kultur', 'Essen']),
    ('pt', ['Cultura', 'Alimentação']), ('vi-VN', ['Culture', 'Food']),
])
def test_localization_failure_uses_the_same_neutral_tag_categories(language, expected):
    value = course()
    value.itinerary.days[0].stops[1].place.category = 'ramen_restaurant'
    before = [stop.place.model_dump() for stop in value.itinerary.days[0].stops]
    result = _fallback(request(language), value)
    assert result.tags == expected
    assert [stop.place.model_dump() for stop in result.itinerary.days[0].stops] == before
    assert all(stop.reason == stop.memo == '' for stop in result.itinerary.days[0].stops)

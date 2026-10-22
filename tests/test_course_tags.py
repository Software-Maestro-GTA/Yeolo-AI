"""Verify category-only tags without creating or changing visit descriptions."""

import pytest

from app.services.course_tags import verified_course_tags
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


def test_tag_names_are_korean_for_every_verified_group():
    day = content_day('museum', 'ramen_restaurant', 'park')
    assert verified_course_tags([day]) == ['문화', '음식', '자연']

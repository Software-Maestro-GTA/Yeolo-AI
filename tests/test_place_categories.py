"""Verify the seven app groups without changing internal provider evidence."""

import pytest
from pydantic import ValidationError

from app.agent.course_state import Candidate
from app.agent.tools.verified_maps import (
    InternalPlaceSchema,
    VerifiedPlace,
    meal_category_supported,
)
from app.schemas.course import CourseResponseSchema, PlaceSchema, StopSchema
from app.services.course_planning import _core_visit
from app.services.place_categories import (
    PLACE_CATEGORY_LABELS,
    map_actual_place_category,
)


@pytest.mark.parametrize(('actual', 'expected'), [
    ('restaurant', 'FOOD'), ('sushi_restaurant', 'FOOD'), ('market', 'FOOD'),
    ('coffee_shop', 'CAFE'), ('bakery', 'CAFE'), ('dessert_restaurant', 'CAFE'),
    ('observation_deck', 'SPOT'), ('tourist_attraction', 'SPOT'),
    ('museum', 'CULTURE'), ('historical_landmark', 'CULTURE'),
    ('park', 'NATURE'), ('hiking_area', 'NATURE'),
    ('theme_park', 'ACTIVITY'), ('spa', 'ACTIVITY'),
    ('pub', 'NIGHTLIFE'), ('night_club', 'NIGHTLIFE'),
    ('unmapped_provider_type', 'SPOT'),
])
def test_actual_provider_types_map_to_requested_app_groups(actual, expected):
    assert map_actual_place_category(actual) == expected


@pytest.mark.parametrize('code', list(PLACE_CATEGORY_LABELS))
def test_public_codes_survive_validation_and_repeated_serialization(code):
    place = PlaceSchema(placeId='id', placeName='장소', category=code,
                        latitude=37.5, longitude=127.)
    wire = place.model_dump(context={'public_response': True})
    assert wire['category'] == code
    assert PlaceSchema.model_validate(wire).model_dump(context={'public_response': True}) == wire


@pytest.mark.parametrize('raw_type', ['museum', 'restaurant', 'hospital', 'UNKNOWN'])
def test_public_contract_rejects_raw_provider_types_and_unknown_codes(raw_type):
    with pytest.raises(ValidationError):
        PlaceSchema(placeId='id', placeName='장소', category=raw_type,
                    latitude=37.5, longitude=127.)


@pytest.mark.parametrize('category', ['museum', 'market', 'brunch_restaurant', 'dessert_restaurant', 'hospital'])
def test_public_serialization_preserves_internal_meal_and_tourism_evidence(category):
    """Public FOOD/SPOT groups must never replace raw safety or copy inputs."""
    raw = InternalPlaceSchema(placeId='places/id', placeName='장소', category=category,
                              address='서울', latitude=37.5, longitude=127.)
    venue = VerifiedPlace(raw)
    proposal = Candidate(name='장소', meal='none')
    before = raw.model_dump()
    meal_before = meal_category_supported(raw.category, 'dinner')
    core_before = _core_visit((proposal, venue))
    stop = StopSchema(sequence=1, stayMinutes=60, memo='방문 팁', reason='추천 이유',
                      place=raw, transportToNext={'type': 'none'})
    wire = stop.model_dump(context={'public_response': True})
    assert wire['place']['placeId'] == 'id'
    assert wire['place']['category'] == map_actual_place_category(category)
    assert set(wire['place']) == {'placeId', 'placeName', 'category', 'latitude', 'longitude'}
    assert raw.category == stop.place.category == category
    assert raw.placeId == stop.place.placeId == 'places/id'
    assert raw.model_dump() == before
    assert InternalPlaceSchema.model_validate(before).category == category
    assert meal_category_supported(raw.category, 'dinner') == meal_before
    assert _core_visit((proposal, venue)) == core_before


@pytest.mark.parametrize('mode', ['validation', 'serialization'])
def test_public_response_schema_exposes_exactly_seven_groups(mode):
    schema = CourseResponseSchema.model_json_schema(mode=mode)
    values = schema['$defs']['PlaceSchema']['properties']['category']['enum']
    assert values == ['FOOD', 'CAFE', 'SPOT', 'CULTURE', 'NATURE', 'ACTIVITY', 'NIGHTLIFE']


@pytest.mark.parametrize(('types', 'expected'), [
    (['tourist_attraction', 'museum', 'point_of_interest'], 'CULTURE'),
    (['tourist_attraction', 'park', 'establishment'], 'NATURE'),
    (['tourist_attraction', 'theme_park'], 'ACTIVITY'),
    (['tourist_attraction', 'observation_deck'], 'SPOT'),
])
def test_generic_tourist_type_uses_existing_specific_facts_only_for_display(types, expected):
    raw = InternalPlaceSchema(placeId='id', placeName='이름만으로는 분류하지 않는 장소',
                              category='tourist_attraction', providerTypes=types,
                              latitude=37.5, longitude=127.)
    wire = raw.model_dump(context={'public_response': True})
    assert wire['category'] == expected
    assert 'providerTypes' not in wire
    assert raw.category == 'tourist_attraction'
    assert raw.providerTypes == types
    assert raw.model_dump()['category'] == 'tourist_attraction'

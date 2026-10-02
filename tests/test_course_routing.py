"""Keep short walking estimates bounded and distinct from provider verified routes."""

import math

import pytest

from app.agent.tools.verified_maps import VerifiedPlace
from app.schemas.course import PlaceSchema, TransportToNextSchema


def venue(identifier, latitude, longitude):
    return VerifiedPlace(PlaceSchema(placeId=identifier, placeName=identifier, category='museum', latitude=latitude, longitude=longitude))


def test_estimate_has_null_route_distance_and_conservative_formula():
    from app.services.course_routing import (
        distance_meters,
        estimated_walking,
        is_estimated_walking,
        valid_route,
    )

    first, second = venue('a', 37.55, 126.98), venue('b', 37.554, 126.98)
    result = estimated_walking(first, second)
    assert result.minutes == math.ceil(distance_meters(first, second) / 1000 * 1.5 / 3 * 60 + 5)
    assert result.type == 'walking' and result.distance is None and result.cost == 0
    assert '[추정 도보]' in result.memo
    assert is_estimated_walking(result)
    assert valid_route(result, first, second)
    assert set(result.model_dump()) == set(TransportToNextSchema.model_fields)


@pytest.mark.parametrize(('latitude', 'longitude'), [(37.56, 126.98), (math.nan, 126.98), (37.55, math.inf), (100, 126.98)])
def test_estimate_rejects_distant_and_invalid_coordinates(latitude, longitude):
    from app.services.course_routing import estimated_walking

    with pytest.raises(ValueError):
        estimated_walking(venue('a', 37.55, 126.98), venue('b', latitude, longitude))


@pytest.mark.parametrize('alteration', [{'minutes': 1}, {'type': 'transit'}, {'distance': 100}, {'memo': 'confirmed walking'}])
def test_estimate_marker_cannot_bypass_formula_or_provenance(alteration):
    from app.services.course_routing import estimated_walking, valid_route

    first, second = venue('a', 37.55, 126.98), venue('b', 37.554, 126.98)
    route = estimated_walking(first, second).model_copy(update=alteration)
    assert not valid_route(route, first, second)


@pytest.mark.parametrize('minutes', [91, 0, None])
def test_known_route_metrics_still_enforce_duration_cap(minutes):
    from app.services.course_routing import valid_route

    route = TransportToNextSchema(type='transit', distance=5000, minutes=minutes)
    assert not valid_route(route, venue('a', 37.55, 126.98), venue('b', 37.58, 126.98))

"""Validate provider route metrics and reject unsupported route estimates."""

import math

import pytest

from app.agent.tools.verified_maps import VerifiedPlace
from app.schemas.course import PlaceSchema, TransportToNextSchema
from app.services.course_routing import distance_meters, valid_route


def venue(identifier, latitude, longitude):
    return VerifiedPlace(PlaceSchema(placeId=identifier, placeName=identifier, category='museum', latitude=latitude, longitude=longitude))


def test_distance_measures_proximity_without_generating_route_times():
    first, second = venue('a', 37.55, 126.98), venue('b', 37.554, 126.98)
    assert 440 < distance_meters(first, second) < 450
    assert distance_meters(first, second) == pytest.approx(distance_meters(second, first))
    assert distance_meters(first, first) == 0


@pytest.mark.parametrize(('latitude', 'longitude'), [(math.nan, 126.98), (37.55, math.inf), (100, 126.98), (37.55, 181)])
def test_proximity_rejects_invalid_coordinates(latitude, longitude):
    with pytest.raises(ValueError):
        distance_meters(venue('a', 37.55, 126.98), venue('b', latitude, longitude))


@pytest.mark.parametrize(('mode', 'distance', 'memo'), [
    ('walking', None, '[추정 도보] 임의 시간 안내'),
    ('walking', 100, '[추정 도보] 임의 시간 안내'),
    ('transit', 100, '[추정 도보] 임의 시간 안내'),
    ('walking', None, 'confirmed walking'),
])
def test_unknown_or_estimated_route_is_never_accepted(mode, distance, memo):
    route = TransportToNextSchema(type=mode, distance=distance, minutes=5, cost=0, memo=memo)
    assert not valid_route(route)


@pytest.mark.parametrize('minutes', [91, 0, None])
def test_known_route_metrics_still_enforce_duration_cap(minutes):
    route = TransportToNextSchema(type='transit', distance=5000, minutes=minutes)
    assert not valid_route(route)


@pytest.mark.parametrize('distance', [None, 0, -1, math.nan, math.inf])
def test_provider_route_requires_positive_finite_distance(distance):
    assert not valid_route(TransportToNextSchema(type='walking', distance=distance, minutes=10))


def test_valid_provider_metrics_are_retained():
    assert valid_route(TransportToNextSchema(type='transit', distance=500, minutes=10))

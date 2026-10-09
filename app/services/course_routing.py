"""Validate actual route facts; retain disclosed estimates for legacy callers only."""

import math

from app.agent.tools.verified_maps import NoRouteError, VerifiedPlace
from app.schemas.course import TransportToNextSchema

ESTIMATE_MARKER = '[추정 도보]'
MAX_ESTIMATE_METERS = 1000


def distance_meters(origin: VerifiedPlace, destination: VerifiedPlace) -> float:
    """Return straight-line meters for finite geographic coordinates.

    Raises:
        ValueError: Either endpoint has invalid coordinates.
    """
    for venue in (origin, destination):
        lat, lon = venue.place.latitude, venue.place.longitude
        if not math.isfinite(lat) or not -90 <= lat <= 90 or not math.isfinite(lon) or not -180 <= lon <= 180:
            raise ValueError('Invalid route coordinates')
    lat1, lat2 = math.radians(origin.place.latitude), math.radians(destination.place.latitude)
    dlat, dlon = lat2 - lat1, math.radians(destination.place.longitude - origin.place.longitude)
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(value)))


def estimated_walking(origin: VerifiedPlace, destination: VerifiedPlace) -> TransportToNextSchema:
    """Estimate minutes only for verified endpoints within one kilometer.

    Returns:
        Existing transport schema with null route distance and explicit caveat.
    Raises:
        ValueError: Coordinates are invalid.
        NoRouteError: The segment exceeds the short-distance fallback bound.

    A 1.5 detour factor, 3 km/h speed and five-minute buffer are conservative
    planning assumptions, not measured route facts. Barriers remain unverified.
    """
    distance = distance_meters(origin, destination)
    if distance > MAX_ESTIMATE_METERS:
        raise NoRouteError()
    minutes = math.ceil(distance / 1000 * 1.5 / 3 * 60 + 5)
    return TransportToNextSchema(type='walking', distance=None, minutes=minutes, cost=0,
        memo=f'{ESTIMATE_MARKER} {destination.place.placeName}까지 직선거리 약 {round(distance)}m를 바탕으로 도보 약 {minutes}분으로 예상했어요. 실제 보행 경로 안내는 포함되지 않으며, 횡단 가능 여부·장애물은 미확인입니다.')


def is_estimated_walking(route: TransportToNextSchema) -> bool:
    """Recognize disclosed estimates; never turn an unknown route into evidence."""
    return route.type == 'walking' and route.distance is None and (route.memo or '').startswith(ESTIMATE_MARKER)


def valid_route(route: object) -> bool:
    """Accept positive provider metrics and reject coordinate-based estimates."""
    if not isinstance(route, TransportToNextSchema) or route.type == 'none' or route.minutes is None or not 0 < route.minutes <= 90:
        return False
    if (route.memo or '').startswith(ESTIMATE_MARKER):
        return False
    return route.distance is not None and math.isfinite(route.distance) and route.distance > 0

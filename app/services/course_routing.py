"""Validate route facts and disclose bounded coordinate-based walking estimates."""

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
        memo=f'{ESTIMATE_MARKER} 직선거리 약 {round(distance)}m에 우회 계수 1.5, 보행 속도 3km/h, 여유 5분을 적용한 약 {minutes}분입니다. 실제 보행 경로·횡단 가능 여부·장애물은 미확인이므로 방문 전 지도에서 확인해 주세요.')


def is_estimated_walking(route: TransportToNextSchema) -> bool:
    """Recognize disclosed estimates; never turn an unknown route into evidence."""
    return route.type == 'walking' and route.distance is None and (route.memo or '').startswith(ESTIMATE_MARKER)


def valid_route(route: object, origin: VerifiedPlace, destination: VerifiedPlace) -> bool:
    """Validate positive provider metrics or recompute a disclosed short estimate."""
    if not isinstance(route, TransportToNextSchema) or route.type == 'none' or route.minutes is None or not 0 < route.minutes <= 90:
        return False
    if (route.memo or '').startswith(ESTIMATE_MARKER):
        if not is_estimated_walking(route) or route.cost != 0:
            return False
        try:
            return route.minutes == estimated_walking(origin, destination).minutes
        except ValueError:
            return False
    return route.distance is not None and math.isfinite(route.distance) and route.distance > 0

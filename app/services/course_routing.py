"""Measure geographic proximity and validate only actual provider route facts."""

import math

from app.agent.tools.verified_maps import VerifiedPlace
from app.schemas.course import TransportToNextSchema

ESTIMATE_MARKER = '[추정 도보]'


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


def valid_route(route: object) -> bool:
    """Accept positive provider metrics and reject coordinate-based estimates."""
    if not isinstance(route, TransportToNextSchema) or route.type == 'none' or route.minutes is None or not 0 < route.minutes <= 90:
        return False
    if (route.memo or '').startswith(ESTIMATE_MARKER):
        return False
    return route.distance is not None and math.isfinite(route.distance) and route.distance > 0

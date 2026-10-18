"""Keep measured transfers distinct from explicitly unavailable route information."""

import math

from app.agent.tools.verified_maps import InternalTransportSchema, VerifiedPlace
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


def has_route_metrics(route: object) -> bool:
    """Recognize positive actual metrics independently of the 90-minute policy."""
    if not isinstance(route, TransportToNextSchema) or route.type in {'none', 'unknown'} or route.minutes is None or route.minutes <= 0:
        return False
    if (route.memo or '').startswith(ESTIMATE_MARKER):
        return False
    return route.distance is not None and math.isfinite(route.distance) and route.distance > 0


def valid_route(route: object) -> bool:
    """Accept measured routes within the duration cap; unknown stays unverified."""
    return has_route_metrics(route) and route.minutes <= 90


def is_unknown_route(route: object) -> bool:
    """Accept only the explicit null-metric shape, with no inferred fare."""
    return (isinstance(route, TransportToNextSchema) and route.type == 'unknown'
            and route.distance is None and route.minutes is None and getattr(route, 'cost', None) is None)


def usable_route(route: object) -> bool:
    """Allow measured or explicitly unavailable transfers in a venue itinerary."""
    return valid_route(route) or is_unknown_route(route)


def unavailable_route() -> InternalTransportSchema:
    """Return unknown transfer data without inventing distance, duration or fare."""
    return InternalTransportSchema(type='unknown', distance=None, minutes=None, cost=None, memo=None)


def minimum_travel_minutes(route: TransportToNextSchema) -> int:
    """Return an internal ordering lower bound, never a guessed travel duration.

    Unknown travel adds no known duration. Subsequent internal arrival clocks
    are provisional lower bounds, excluded from the public API. The regular
    ten-minute visit transition buffer remains separate from travel metrics.
    """
    return 0 if is_unknown_route(route) else (route.minutes or 0)

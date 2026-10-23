"""Resolve optional hotel and airport transfers without inventing route metrics."""

import asyncio
import logging

from app.agent.tools.verified_maps import (
    Destination,
    MapsProviderError,
    VerifiedMapsProvider,
    VerifiedPlace,
)
from app.schemas.course import TransportToNextSchema
from app.services.course_routing import (
    distance_meters,
    has_route_metrics,
    unavailable_route,
)

logger = logging.getLogger(__name__)


async def optional_transfer(
    provider: VerifiedMapsProvider,
    origin: VerifiedPlace,
    target: VerifiedPlace,
    destination: Destination,
    deadline: float,
    *,
    available_minutes: int | None = None,
) -> TransportToNextSchema:
    """Try bounded measured alternatives within one shared deadline.

    Args:
        provider: Existing request-scoped Maps client and cache.
        origin: Verified start location.
        target: Verified arrival location.
        destination: Country context for choosing travel modes.
        deadline: Absolute event-loop deadline shared with other transfers.
        available_minutes: Time actually left in the day's schedule, if known.
            None accepts measured routes without imposing a duration ceiling.
    Returns:
        First acceptable real route, shortest measured overlong alternative, or
        unknown. Straight-line distance only selects modes, never public metrics.
    Raises:
        asyncio.CancelledError: Caller cancellation is propagated.
    """
    separation = distance_meters(origin, target)
    modes = ['transit', 'taxi']
    if destination.country_code != 'KR' and separation <= 1500:
        modes.insert(0, 'walking')
    elif separation <= 3000:
        modes.insert(1, 'walking')
    overlong = None
    try:
        async with asyncio.timeout_at(deadline):
            for mode in modes:
                try:
                    # Internal arrival clocks are planning bounds, not a flight
                    # timetable. Do not assert a scheduled departure time.
                    route = await provider.route(origin, target, mode=mode, departure_time=None)
                except MapsProviderError as error:
                    if error.kind not in {'no_route', 'route_data', 'transient'}:
                        break
                    continue
                except Exception:  # noqa: BLE001 - optional guidance cannot discard the day
                    logger.info('Optional transfer unavailable: mode=%s', mode)
                    continue
                if not has_route_metrics(route):
                    continue
                if available_minutes is None or route.minutes <= available_minutes:
                    return route
                if overlong is None or route.minutes < overlong.minutes:
                    overlong = route
    except TimeoutError:
        pass
    return overlong if overlong is not None else unavailable_route()

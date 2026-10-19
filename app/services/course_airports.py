"""Connect verified arrival/departure airports to an otherwise complete course.

Flight prices already belong to totalCost. Airport endpoints add only measured
ground-transfer fares and do not imply a booked flight or feasible timetable.
"""

import asyncio
import logging

from app.agent.tools.verified_maps import (
    Destination,
    VerifiedMapsProvider,
    VerifiedPlace,
)
from app.schemas.course import (
    CourseRequestSchema,
    CourseSchema,
    DayItinerarySchema,
    StopSchema,
    TransportToNextSchema,
)
from app.services.course_localization import airport_copy
from app.services.course_routing import unavailable_route
from app.services.course_transfers import optional_transfer
from app.services.myrealtrip_quotes import TripQuotes
from app.services.place_categories import map_actual_place_category

logger = logging.getLogger(__name__)
AIRPORT_TIMEOUT_SECONDS = 12.0


def _airport_stop(airport: VerifiedPlace, language: str, *, arrival: bool) -> StopSchema:
    """Create planning allowances, never an invented flight schedule or fare."""
    reason, memo = airport_copy(language, arrival=arrival)
    return StopSchema(
        sequence=1, stayMinutes=60 if arrival else 120, reason=reason, memo=memo,
        cost=0, place=airport.place.model_copy(deep=True),
        transportToNext=TransportToNextSchema(type='none', distance=0, minutes=0, memo=None),
    )


async def attach_airports(
    request: CourseRequestSchema,
    course: CourseSchema,
    destination: Destination,
    quotes: TripQuotes | None,
    provider: VerifiedMapsProvider,
    *,
    deadline: float | None = None,
    verified_airport: VerifiedPlace | None = None,
    lookup: bool = True,
) -> CourseSchema:
    """Insert the flight destination airport at both ends of an air-trip itinerary.

    Args:
        request: Requested duration and display language.
        course: Completed daytime/hotel itinerary including round-trip airfare.
        destination: Verified destination country/city context.
        quotes: Supplier prices or planning estimates already included in the
            course total, with the corresponding supplier airport when known.
        provider: Request-scoped Maps Essentials and routing boundary.
        deadline: Optional absolute event-loop deadline.
        verified_airport: Optional airport already verified during candidate selection.
        lookup: Whether to resolve the supplier identity here. False reuses the
            earlier result and prevents repeated paid verification.
    Returns:
        Copied course with arrival/departure endpoints and actual or unknown
        transfers. No identified airport or lookup failure keeps the original
        course. A verified airport survives an expired transfer budget with
        unknown metrics. Existing visits, lodging and history are preserved.
        Long measured airport transfers remain valid beyond the tourist cap.
    Raises:
        asyncio.CancelledError: Caller cancellation after transfer tasks join.
    """
    if quotes is None or quotes.transport_mode != 'air':
        logger.debug('Airport attachment skipped: reason=non_air_trip')
        return course
    identity = quotes.flight.airport_identity
    days = course.itinerary.days
    if verified_airport is None and (not lookup or identity is None or identity.country_code != destination.country_code):
        logger.info('Airport attachment skipped: reason=no_verified_identity')
        return course
    if (not days or len(days) != request.tripCondition.totalDays
            or not days[0].stops or not days[-1].stops):
        logger.info('Airport attachment skipped: reason=incomplete_itinerary')
        return course
    # Re-entry cannot duplicate endpoints or charge airport transfers twice.
    if any(map_actual_place_category(stop.place.category) == 'AIRPORT'
           for day in days for stop in day.stops):
        logger.debug('Airport attachment skipped: reason=already_attached')
        return course
    loop = asyncio.get_running_loop()
    expires = min(loop.time() + AIRPORT_TIMEOUT_SECONDS, deadline if deadline is not None else float('inf'))
    if verified_airport is None and expires <= loop.time():
        logger.info('Airport attachment skipped: reason=lookup_deadline')
        return course
    try:
        airport = verified_airport
        if airport is None:
            async with asyncio.timeout_at(expires):
                airport = await provider.search_airport(identity, destination)
        if not isinstance(airport, VerifiedPlace) or map_actual_place_category(airport.place.category) != 'AIRPORT':
            logger.info('Airport attachment skipped: reason=unverified_airport')
            return course
        airport_id = airport.place.placeId.removeprefix('places/')
        if not airport_id or any(stop.place.placeId.removeprefix('places/') == airport_id
                                 for day in days for stop in day.stops):
            logger.info('Airport attachment skipped: reason=invalid_or_duplicate_place_id')
            return course
        pairs = (
            (airport, VerifiedPlace(days[0].stops[0].place)),
            (VerifiedPlace(days[-1].stops[-1].place), airport),
        )
        if expires <= loop.time():
            # The airport was already validated. Expiring the optional route
            # allowance cannot erase that independent location evidence.
            logger.info('Airport transfers unavailable: reason=transfer_deadline')
            arrival_route, departure_route = unavailable_route(), unavailable_route()
        else:
            tasks = [asyncio.create_task(optional_transfer(
                provider, origin, target, destination, expires, max_minutes=None,
            )) for origin, target in pairs]
            try:
                arrival_route, departure_route = await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        output = course.model_copy(deep=True)
        # Also works for a same-day round trip: modify the single day once,
        # keeping the same airport at both endpoints as distinct sequence slots.
        for index in sorted({0, len(days) - 1}):
            day = output.itinerary.days[index]
            output.itinerary.days[index] = DayItinerarySchema(day=day.day, date=day.date, stops=day.stops)
        first, last = output.itinerary.days[0], output.itinerary.days[-1]
        arrival = _airport_stop(airport, request.language, arrival=True)
        arrival.transportToNext = arrival_route
        first.stops.insert(0, arrival)
        last.stops[-1].transportToNext = departure_route
        last.stops.append(_airport_stop(airport, request.language, arrival=False))
        for day in (first, last):
            for index, stop in enumerate(day.stops, start=1):
                stop.sequence = index
        output.totalCost += sum(getattr(route, 'cost', None) or 0 for route in (arrival_route, departure_route))
        return output
    except Exception:  # noqa: BLE001 - optional airport guidance must not discard a verified itinerary
        logger.warning('Airport attachment skipped: reason=attachment_error')
        return course

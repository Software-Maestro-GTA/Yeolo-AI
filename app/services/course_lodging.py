"""Append a verified shared overnight stay without changing daytime planning.

Supplier or proposed hotel names are resolved through Maps Essentials. Return transfers may
remain unknown; no hotel, coordinates, fare or room availability is invented.
"""

import asyncio
import logging
from dataclasses import dataclass

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
from app.services.course_localization import lodging_copy
from app.services.course_routing import (
    has_route_metrics,
    valid_route,
)
from app.services.course_transfers import optional_transfer
from app.services.myrealtrip_quotes import TripQuotes
from app.services.place_categories import map_actual_place_category

logger = logging.getLogger(__name__)
LODGING_TIMEOUT_SECONDS = 12.0


@dataclass(frozen=True)
class _LodgingQuery:
    """Use only the supplier's hotel name as retrieval evidence."""

    name: str
    english_name: str = ''
    meal: str = 'none'


async def attach_lodging(
    request: CourseRequestSchema,
    course: CourseSchema,
    destination: Destination,
    quotes: TripQuotes | None,
    provider: VerifiedMapsProvider,
    *,
    deadline: float | None = None,
    verified_lodging: VerifiedPlace | None = None,
    lookup: bool = True,
) -> CourseSchema:
    """Add the same verified hotel after each overnight day's daytime stops.

    Args:
        request: Travel duration and requested display language.
        course: Finalized daytime course, already including whole-stay costs.
        destination: Country and city viewport verified during preparation.
        quotes: Resolved prices with an optional validated supplier hotel identity.
        provider: Existing request-scoped Maps client and cache.
        deadline: Optional shared absolute event-loop deadline.
        verified_lodging: Optional location already verified before daily planning.
        lookup: Whether this call may resolve a supplier hotel. False reuses the
            completed selection, including a missing result, without new lookups.
    Returns:
        A copied course with N-1 hotel stops and return transfers, or the original
        if hotel identity cannot be verified. Existing daytime visits and history
        stay intact. Nightly amounts split the included lodging total exactly;
        only new known transfer fares increase totalCost. A measured return over
        90 minutes prevents attachment rather than being disguised as unknown.
    Raises:
        asyncio.CancelledError: Caller cancellation after child tasks are joined.
    """
    nights = request.tripCondition.totalDays - 1
    if nights <= 0 or quotes is None:
        return course
    if verified_lodging is None and (not lookup or quotes.lodging.lodging_identity is None):
        return course
    amount = quotes.lodging.amount_krw
    if amount is None or len(course.itinerary.days) != nights + 1:
        return course
    # Re-entry cannot append the same stay or charge the return fare twice.
    if any(map_actual_place_category(stop.place.category) == 'LODGING'
           for day in course.itinerary.days for stop in day.stops):
        return course
    loop = asyncio.get_running_loop()
    expires = min(loop.time() + LODGING_TIMEOUT_SECONDS, deadline if deadline is not None else float('inf'))
    if expires <= loop.time():
        return course
    try:
        hotel = verified_lodging
        if hotel is None:
            async with asyncio.timeout_at(expires):
                hotel = await provider.search_lodging(_LodgingQuery(quotes.lodging.lodging_identity.name), destination)
        if not isinstance(hotel, VerifiedPlace) or map_actual_place_category(hotel.place.category) != 'LODGING':
            return course
        hotel_id = hotel.place.placeId.removeprefix('places/')
        if not hotel_id or any(
            stop.place.placeId.removeprefix('places/') == hotel_id
            for day in course.itinerary.days for stop in day.stops
        ):
            return course
        origins = [VerifiedPlace(day.stops[-1].place) for day in course.itinerary.days[:nights]]
        semaphore = asyncio.Semaphore(3)

        async def transfer(origin: VerifiedPlace) -> TransportToNextSchema:
            async with semaphore:
                return await optional_transfer(provider, origin, hotel, destination, expires)

        tasks = [asyncio.create_task(transfer(origin)) for origin in origins]
        try:
            routes = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if any(has_route_metrics(route) and not valid_route(route) for route in routes):
            logger.info('Overnight stay omitted: measured return exceeds duration policy')
            return course
        output = course.model_copy(deep=True)
        nightly, remainder = divmod(amount, nights)
        reason, memo = lodging_copy(request.language)
        for index, route in enumerate(routes):
            original = output.itinerary.days[index]
            day = DayItinerarySchema(day=original.day, date=original.date, stops=original.stops)
            output.itinerary.days[index] = day
            day.stops[-1].transportToNext = route
            # Thirty minutes represents settling in, not overnight sleep or an
            # asserted check-in window. No overnight arrival time is fabricated.
            day.stops.append(StopSchema(
                sequence=len(day.stops) + 1, stayMinutes=30,
                memo=memo, reason=reason, cost=nightly + (index < remainder),
                place=hotel.place.model_copy(deep=True),
                transportToNext=TransportToNextSchema(type='none', distance=0, minutes=0, memo=None),
            ))
            output.totalCost += getattr(route, 'cost', None) or 0
        return output
    except Exception:  # noqa: BLE001 - optional lodging must not discard a verified daytime course
        logger.warning('Overnight stay could not be verified; retaining daytime itinerary')
        return course

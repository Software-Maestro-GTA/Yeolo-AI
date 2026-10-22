"""Append a verified shared overnight stay without changing daytime planning.

Supplier or proposed hotel names are resolved through Maps Essentials. Return transfers may
remain unknown; no hotel, coordinates, fare or room availability is invented.
Measured returns fit the internal day clock, including settling in by 21:00.
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
from app.services.course_routing import (
    has_route_metrics,
)
from app.services.course_transfers import optional_transfer
from app.services.myrealtrip_quotes import TripQuotes
from app.services.place_categories import map_actual_place_category

logger = logging.getLogger(__name__)
LODGING_TIMEOUT_SECONDS = 12.0
LODGING_SETTLING_MINUTES = 30
RETURN_TRANSITION_MINUTES = 10
DAY_END_MINUTES = 21 * 60


@dataclass(frozen=True)
class _LodgingQuery:
    """Use only the supplier's hotel name as retrieval evidence."""

    name: str
    english_name: str = ''
    meal: str = 'none'


def _return_time_allowance(stop: StopSchema) -> int | None:
    """Reserve settling-in and transition time without inventing an internal clock."""
    arrival = getattr(stop, 'arrivalTime', None)
    try:
        hour, minute = (int(value) for value in arrival.split(':'))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError('Invalid internal arrival time')
    except (AttributeError, TypeError, ValueError):
        return None
    return DAY_END_MINUTES - (hour * 60 + minute + stop.stayMinutes
                              + RETURN_TRANSITION_MINUTES + LODGING_SETTLING_MINUTES)


def _return_fits_day(stop: StopSchema, route: TransportToNextSchema) -> bool:
    """Check a measured return against the retained internal visit clock.

    Unknown routes preserve the existing provisional policy. Public courses have
    no arrivalTime: retain measured returns without inventing a departure time
    or a fixed duration ceiling. Internal clocks
    following earlier unknown transfers also remain provisional lower bounds.
    """
    if not has_route_metrics(route):
        return True
    allowance = _return_time_allowance(stop)
    if allowance is None:
        return True
    return route.minutes <= allowance


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
        A copied course with feasible overnight hotel stops and return transfers, or the original
        if hotel identity cannot be verified. Existing daytime visits and history
        stay intact. Nightly amounts split the included lodging total exactly;
        only new known transfer fares increase totalCost. Measured returns
        remain eligible when settling in fits before 21:00. A day that
        cannot fit the return retains its daytime visits without the hotel link.
        Without an internal clock, return timing remains unconfirmed and no
        departure time or fixed route-duration ceiling is invented.
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
        final_stops = [day.stops[-1] for day in course.itinerary.days[:nights]]
        semaphore = asyncio.Semaphore(3)

        async def transfer(stop: StopSchema) -> TransportToNextSchema:
            allowance = _return_time_allowance(stop)
            async with semaphore:
                return await optional_transfer(
                    provider, VerifiedPlace(stop.place), hotel, destination, expires,
                    available_minutes=None if allowance is None else max(0, allowance),
                )

        tasks = [asyncio.create_task(transfer(stop)) for stop in final_stops]
        try:
            routes = await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        output = course.model_copy(deep=True)
        nightly, remainder = divmod(amount, nights)
        for index, route in enumerate(routes):
            original = output.itinerary.days[index]
            if not _return_fits_day(original.stops[-1], route):
                logger.info('Overnight return deferred: day=%s cannot verify a finish by 21:00', original.day)
                continue
            day = DayItinerarySchema(day=original.day, date=original.date, stops=original.stops)
            output.itinerary.days[index] = day
            day.stops[-1].transportToNext = route
            # Thirty minutes represents settling in, not overnight sleep or an
            # asserted check-in window. No overnight arrival time is fabricated.
            day.stops.append(StopSchema(
                sequence=len(day.stops) + 1, stayMinutes=LODGING_SETTLING_MINUTES,
                memo="", reason="", cost=nightly + (index < remainder),
                place=hotel.place.model_copy(deep=True),
                transportToNext=TransportToNextSchema(type='none', distance=0, minutes=0, memo=None),
            ))
            output.totalCost += getattr(route, 'cost', None) or 0
        return output
    except Exception:  # noqa: BLE001 - optional lodging must not discard a verified daytime course
        logger.warning('Overnight stay could not be verified; retaining daytime itinerary')
        return course

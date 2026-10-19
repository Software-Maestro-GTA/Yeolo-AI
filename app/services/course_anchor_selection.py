"""Verify trip-wide airport and hotel proposals independently of reservations.

Supplier identities have priority. Generated alternatives require Maps checks
and cannot inherit the price of an unrelated flight or hotel.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.agent.tools.verified_maps import (
    Destination,
    VerifiedMapsProvider,
    VerifiedPlace,
)
from app.core.config import settings
from app.schemas.course import CourseRequestSchema
from app.schemas.course_anchors import AnchorCandidates, NamedLodgingCandidate
from app.services.myrealtrip_quotes import QuoteItem, TripQuotes
from app.services.place_categories import map_actual_place_category
from app.services.travel_cost_baselines import baseline_trip_quotes

logger = logging.getLogger(__name__)
ANCHOR_LOOKUP_TIMEOUT_SECONDS = 12.0
MAX_ANCHOR_LOOKUPS = 2


@dataclass(frozen=True)
class ResolvedCourseAnchors:
    """Request-local verified locations and prices with matching provenance."""

    airport: VerifiedPlace | None
    lodging: VerifiedPlace | None
    quotes: TripQuotes
    airport_attempts: tuple[str, ...] = ()


async def _optional_lookup(
    lookup: Callable[[], Awaitable[VerifiedPlace | None]], category: str,
) -> VerifiedPlace | None:
    """Isolate optional provider errors; caller cancellation still propagates."""
    try:
        venue = await lookup()
        if isinstance(venue, VerifiedPlace) and map_actual_place_category(venue.place.category) == category:
            return venue
    except Exception:  # noqa: BLE001 - failed optional candidates cannot invalidate tourism
        logger.info('Optional trip location unavailable: category=%s', category)
    return None


async def resolve_course_anchors(
    request: CourseRequestSchema,
    destination: Destination,
    quotes: TripQuotes,
    candidates: AnchorCandidates,
    provider: VerifiedMapsProvider,
    *,
    deadline: float | None = None,
    previous: ResolvedCourseAnchors | None = None,
) -> ResolvedCourseAnchors:
    """Resolve supplier locations or bounded generated alternatives in parallel.

    Args:
        request: Destination, duration, budget and language of the whole trip.
        destination: Previously verified destination country and city boundary.
        quotes: Existing real prices or service planning benchmarks.
        candidates: Names proposed in an existing daily LLM call, not facts.
        provider: Existing request-scoped Maps Essentials client and cache.
        deadline: Optional shared absolute event-loop deadline.
        previous: Completed request-local selection. Keep successful locations
            and try only new airport codes within the original two-attempt cap.
    Returns:
        Verified locations and reconciled quotes. Alternative hotels use policy
        estimates; actual airfare survives only for the same supplier IATA code.
        Failure of one role keeps the other and does not discard daily visits.
    Raises:
        asyncio.CancelledError: Caller cancellation after both tasks are joined.
    """
    baseline = baseline_trip_quotes(request, destination, adults=settings.MYREALTRIP_ADULTS)
    loop = asyncio.get_running_loop()
    expires = min(loop.time() + ANCHOR_LOOKUP_TIMEOUT_SECONDS, deadline if deadline is not None else float('inf'))
    if expires <= loop.time():
        logger.info('Trip anchor verification skipped: reason=deadline')
        return previous or ResolvedCourseAnchors(None, None, quotes)
    prior_codes = set(previous.airport_attempts) if previous is not None else set()
    airport_attempts = list(previous.airport_attempts) if previous is not None else []

    async def airport() -> tuple[VerifiedPlace | None, QuoteItem]:
        if previous is not None and previous.airport is not None:
            return previous.airport, previous.quotes.flight
        if quotes.transport_mode != 'air':
            return None, quotes.flight
        identity = quotes.flight.airport_identity
        try:
            async with asyncio.timeout_at(expires):
                if identity is not None and identity.code not in prior_codes and len(airport_attempts) < MAX_ANCHOR_LOOKUPS:
                    airport_attempts.append(identity.code)
                    venue = await _optional_lookup(lambda: provider.search_airport(identity, destination), 'AIRPORT')
                    if venue is not None:
                        logger.info('Trip airport selected: source=supplier code=%s', identity.code)
                        return venue, quotes.flight
                # A repeat of the supplier airport must not consume the last
                # slot before a different generated airport can be checked.
                ordered = sorted(candidates.airports, key=lambda item: identity is not None and item.iata_code == identity.code)
                for candidate in ordered:
                    if candidate.iata_code in prior_codes:
                        continue
                    if (identity is not None and candidate.iata_code == identity.code
                            and candidate.name.strip().casefold() == identity.name.strip().casefold()
                            and candidate.english_name.strip().casefold() == (identity.english_name or '').strip().casefold()):
                        # Both name and alias have already been searched by the
                        # supplier verifier; this is not a new alternative.
                        continue
                    if len(airport_attempts) >= MAX_ANCHOR_LOOKUPS:
                        break
                    airport_attempts.append(candidate.iata_code)
                    venue = await _optional_lookup(lambda candidate=candidate: provider.search_airport_candidate(candidate, destination), 'AIRPORT')
                    if venue is not None:
                        same_airport = (identity is not None and identity.country_code == destination.country_code
                                        and identity.code == candidate.iata_code)
                        logger.info('Trip airport selected: source=generated code=%s', candidate.iata_code)
                        return venue, quotes.flight if same_airport else baseline.flight
        except TimeoutError:
            logger.info('Optional airport verification deadline reached')
        except Exception:  # noqa: BLE001 - malformed optional metadata cannot invalidate visits
            logger.warning('Optional airport selection unavailable')
        logger.info('Trip airport unavailable: candidate_count=%d attempted_codes=%s',
                    len(candidates.airports), ','.join(airport_attempts) or 'none')
        return None, quotes.flight

    async def lodging() -> tuple[VerifiedPlace | None, QuoteItem]:
        if previous is not None:
            return previous.lodging, previous.quotes.lodging
        if request.tripCondition.totalDays <= 1:
            return None, quotes.lodging
        identity = quotes.lodging.lodging_identity
        seen: set[tuple[str, str]] = set()
        attempts = 0
        try:
            async with asyncio.timeout_at(expires):
                if identity is not None:
                    seen.add((identity.name.strip().casefold(), ''))
                    attempts += 1
                    # Isolate supplier-to-query validation too: an unusably long
                    # supplier name must not prevent valid generated alternatives.
                    venue = await _optional_lookup(
                        lambda: provider.search_lodging(NamedLodgingCandidate(name=identity.name), destination),
                        'LODGING',
                    )
                    if venue is not None:
                        return venue, quotes.lodging
                for candidate in candidates.lodgings:
                    key = (candidate.name.casefold(), candidate.english_name.casefold())
                    if key in seen:
                        continue
                    if attempts >= MAX_ANCHOR_LOOKUPS:
                        break
                    seen.add(key)
                    attempts += 1
                    venue = await _optional_lookup(lambda candidate=candidate: provider.search_lodging(candidate, destination), 'LODGING')
                    if venue is not None:
                        # A name match alone cannot bind this alternative to the
                        # supplier gid whose room price was previously retrieved.
                        return venue, baseline.lodging
        except TimeoutError:
            logger.info('Optional lodging verification deadline reached')
        except Exception:  # noqa: BLE001 - malformed optional metadata cannot invalidate visits
            logger.warning('Optional lodging selection unavailable')
        return None, quotes.lodging

    tasks = [asyncio.create_task(airport()), asyncio.create_task(lodging())]
    try:
        (airport_place, flight_quote), (lodging_place, lodging_quote) = await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return ResolvedCourseAnchors(airport_place, lodging_place, TripQuotes(
        flight_quote, lodging_quote, transport_mode=quotes.transport_mode,
    ), tuple(airport_attempts))

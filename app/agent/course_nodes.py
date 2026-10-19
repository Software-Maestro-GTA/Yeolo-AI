"""Execute course generation nodes using injected Maps and history boundaries.

Normal generation and recovery return partial CourseState updates. Request data
and caches remain in state; this class holds only provider and history handles."""

import asyncio
import copy
import logging
import math
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from langgraph.config import get_stream_writer

from app.agent.course_drafting import (
    MIN_DAILY_DRAFT_BUDGET_SECONDS,
    _recoverable_draft,
    draft_candidates,
)
from app.agent.course_state import (
    CourseGenerationError,
    CourseNode,
    CourseState,
    DraftDay,
    PartialDraftError,
    Visit,
)
from app.agent.course_transitions import (
    _can_refill,
    _max_drafts,
    _needs_fullness,
    _optional_budget,
)
from app.agent.tools.verified_maps import (
    MapsProviderError,
    VerifiedMapsProvider,
    VerifiedPlace,
    individual_place_category_supported,
    meal_category_supported,
)
from app.core.config import settings
from app.schemas.course import (
    CourseRequestSchema,
    CourseSchema,
    DayItinerarySchema,
    ItinerarySchema,
    TransportToNextSchema,
)
from app.services.course_budget import (
    FLIGHT_BUDGET_SHARE,
    LODGING_BUDGET_SHARE,
    budget_guidance,
    course_cost,
    daily_place_budget,
)
from app.services.course_candidate_batches import candidate_batch, candidate_query_key
from app.services.course_diversity import (
    MAX_OVERLAP,
    overlap_scores,
    planning_overlap,
)
from app.services.course_history import CourseHistory, history_key
from app.services.course_localization import (
    LOCALIZATION_TIMEOUT_SECONDS,
    localize_course,
    progress_message,
)
from app.services.course_planning import (
    MAX_DAY_PLANS,
    MEAL_WINDOWS,
    _canonical_id,
    _choose_disjoint_plans,
    _core_visit,
    _day_plans,
    _opening_start,
    _pace_limit,
    _plan_signature,
    _proposal_floor,
    _schedule_day,
    _selection_metadata,
    _shorter_visits,
    _visit_shortage,
)
from app.services.course_reasons import (
    apply_personalized_reasons,
    verified_course_tags,
)
from app.services.course_routing import (
    distance_meters,
    has_route_metrics,
    is_unknown_route,
    minimum_travel_minutes,
    unavailable_route,
    usable_route,
    valid_route,
)
from app.services.myrealtrip_quotes import (
    MyRealTripQuoteProvider,
    QuoteRequest,
)
from app.services.travel_cost_baselines import baseline_trip_quotes, merge_trip_quotes

logger = logging.getLogger(__name__)


def _progress(key: str, *, language: str = "ko") -> None:
    get_stream_writer()(
        {"step": "GENERATING_ROUTE", "message": progress_message(language, key)}
    )


async def _parallel(
    items: list[Any], operation: Callable[[Any], Awaitable[Any]]
) -> list[Any]:
    """Bound work and always cancel/join children when a request is cancelled."""
    semaphore = asyncio.Semaphore(max(1, min(settings.COURSE_MAPS_CONCURRENCY, 8)))

    async def run(item: Any) -> Any:
        async with semaphore:
            return await operation(item)

    tasks = [asyncio.create_task(run(item)) for item in items]
    try:
        return await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class CourseGraphNodes:
    """Implement nodes using injected boundaries and request-local graph state.

    Args:
        provider: Venue and route verification boundary.
        history: Recent course storage boundary.
        quotes: Optional flight and accommodation quote boundary.

    Dependencies live here; drafts, caches, retries and snapshots live only in
    CourseState. Each node returns the same partial state updates as before.
    """

    def __init__(
        self,
        provider: VerifiedMapsProvider,
        history: CourseHistory,
        quotes: MyRealTripQuoteProvider | None = None,
    ) -> None:
        self.provider = provider
        self.history = history
        self.quotes = quotes

    # Normal generation.

    async def prepare(self, state: CourseState) -> dict:
        """Resolve the destination and initialize request-local state."""
        _progress(
            "prepare",
            language=state["request"].language,
        )
        request = state["request"]
        date.fromisoformat(request.tripCondition.startDate)
        destination = await self.provider.resolve_destination(
            request.tripCondition.destinationCountry,
            request.tripCondition.destinationCity,
        )
        unavailable = False
        try:
            async with asyncio.timeout(1.0):
                recent, profiles = await self._read_history(request)
        except OSError, sqlite3.Error, TimeoutError:
            logger.warning("Course history read unavailable")
            recent, profiles, unavailable = [], [], True
        return {
            "destination": destination,
            "recent": recent,
            "recent_profiles": profiles,
            "history_unavailable": unavailable,
            "attempt": 0,
            "feedback": "",
            "validated_days": {},
            "place_cache": {},
            "queried_place_keys": set(),
            "pending_place_days": set(),
            "expanding_places": False,
            "route_cache": {},
            "route_pass_failures": {},
            "candidate_pools": {},
            "rejected_plans": {},
            "repairing": False,
            "repeated": False,
            "restore_fallback": False,
            "verified_fallback": {},
            "refill_attempt": 0,
            "refilling": False,
        }

    async def quote_trip(self, state: CourseState) -> dict:
        """Resolve return travel and all nights before allocating the venue budget.

        Available provider amounts replace explicit planning estimates. Optional
        lookup errors keep the estimates; request cancellation still propagates.
        """
        _progress("quote", language=state["request"].language)
        trip = state["request"].tripCondition
        start = date.fromisoformat(trip.startDate)
        baseline = baseline_trip_quotes(
            state["request"], state["destination"], adults=settings.MYREALTRIP_ADULTS
        )
        # No supplier rail/bus quote exists. A day trip without flying has no
        # flight or hotel lookup to perform, but still includes ground travel.
        if baseline.transport_mode != "air" and trip.totalDays == 1:
            return {"trip_quotes": baseline}
        # Compare real local product prices against each allowance. A universal
        # KRW activity band cannot establish what hotel/cabin the city affords.
        live = None
        quote_request = None
        try:
            if self.quotes is None:
                raise ValueError("Quote provider unavailable")
            configured_timeout = settings.MYREALTRIP_QUOTE_TIMEOUT_SECONDS
            quote_timeout = (
                configured_timeout
                if math.isfinite(configured_timeout) and configured_timeout > 0 else 8.0
            )
            quote_deadline = min(
                asyncio.get_running_loop().time() + quote_timeout,
                state.get("deadline", float("inf")) - 28.0,
            )
            quote_request = QuoteRequest(
                country_code=state["destination"].country_code,
                country=trip.destinationCountry,
                city=trip.destinationCity,
                start_date=start,
                end_date=start + timedelta(days=trip.totalDays - 1),
                flight_required=baseline.transport_mode == "air",
                origin_airport=(
                    settings.MYREALTRIP_DOMESTIC_ORIGIN_AIRPORT
                    if state["destination"].country_code == "KR" and baseline.transport_mode == "air"
                    else None
                ),
                verified_country_name=state["destination"].verified_country_name or None,
                verified_city_name=state["destination"].verified_city_name or None,
                preferred_cabin="BUSINESS" if trip.budget > 0 else "ECONOMY",
                preferred_stay_star_rating="fivestar" if trip.budget > 0 else None,
                flight_budget_krw=trip.budget * FLIGHT_BUDGET_SHARE,
                lodging_budget_krw=trip.budget * LODGING_BUDGET_SHARE,
            )
            async with asyncio.timeout_at(quote_deadline):
                live = await self.quotes.quote(quote_request, deadline=quote_deadline)
        except TimeoutError:
            if isinstance(self.quotes, MyRealTripQuoteProvider) and quote_request is not None:
                live = self.quotes.completed_quotes(quote_request)
            logger.warning("Travel quote deadline reached; keeping completed prices and planning benchmarks")
        except Exception:  # noqa: BLE001 - optional quotes must not discard a verified itinerary; cancellation propagates
            logger.warning("Travel quote lookup unavailable; using planning benchmarks")
        return {"trip_quotes": merge_trip_quotes(baseline, live)}

    async def expand_places(self, state: CourseState) -> dict:
        """Query deferred real candidates before spending another model request."""
        return {"expanding_places": True, "feedback": ""}

    async def draft(self, state: CourseState) -> dict:
        """Propose missing days while retaining successful day drafts."""
        _progress(
            "draft",
            language=state["request"].language,
        )
        existing = dict(state.get("partial_drafts", {}))
        existing.update(
            {
                index: DraftDay(
                    candidates=[candidate for candidate, _ in saved["selected"]]
                )
                for index, saved in state.get("validated_days", {}).items()
            }
        )
        pending = [
            index
            for index in range(state["request"].tripCondition.totalDays)
            if index not in existing
        ]
        try:
            draft_deadline = state["deadline"] - 28.0 if "deadline" in state else None
            result = await draft_candidates(
                state["request"],
                sorted(set().union(*state["recent"])) if state["recent"] else [],
                self._history_feedback(state) + "\n" + state.get("feedback", ""),
                pending_days=pending,
                existing_days=existing,
                deadline=draft_deadline,
            )
        except PartialDraftError as error:
            return {
                "partial_drafts": error.days,
                "attempt": state["attempt"] + 1,
                "feedback": "일부 일자 후보 응답이 지연되었습니다. 완료한 일자는 유지하고 누락한 일자만 보충하세요.",
            }
        except Exception as error:
            if not _recoverable_draft(error):
                raise
            if state.get("verified_fallback") and not _needs_fullness(state):
                return {
                    "attempt": state["attempt"] + 1,
                    "restore_fallback": True,
                    "feedback": "",
                }
            return {
                "attempt": state["attempt"] + 1,
                "feedback": "모델의 일시적 응답 오류입니다. 기존에 확인한 장소를 유지하고 필요한 대안을 다시 제안하세요.",
            }
        if len(result.days) != state["request"].tripCondition.totalDays:
            if state.get("verified_fallback") and not _needs_fullness(state):
                return {
                    "attempt": state["attempt"] + 1,
                    "restore_fallback": True,
                    "feedback": "",
                }
            return {
                "attempt": state["attempt"] + 1,
                "feedback": "요청한 여행 일수와 초안의 days 개수를 일치시키세요.",
            }
        result = result.model_copy(deep=True)
        for index, saved in state.get("validated_days", {}).items():
            result.days[index] = DraftDay(
                candidates=[candidate for candidate, _ in saved["selected"]]
            )
        return {
            "draft_data": result,
            "partial_drafts": {},
            "attempt": state["attempt"] + 1,
            "feedback": "",
            "queried_place_keys": set(state["place_cache"]),
            "route_pass_failures": {},
            "expanding_places": False,
        }

    async def verify_places(self, state: CourseState) -> dict:
        """Verify venue facts, supplement nearby candidates and form day plans."""
        _progress(
            "verify_places",
            language=state["request"].language,
        )
        saved = state["validated_days"]
        reserved_ids = {
            venue.place.placeId
            for value in saved.values()
            for _, venue in value["selected"]
        }
        cache = state["place_cache"]
        pools = {index: dict(pool) for index, pool in state["candidate_pools"].items()}

        key = candidate_query_key
        queried = set(state.get("queried_place_keys", set(cache)))
        unique = {}
        for index, day in enumerate(state["draft_data"].days):
            if index in saved:
                continue
            if state.get("expanding_places") and index not in state.get(
                "pending_place_days", set()
            ):
                continue
            cached_count = sum(key(candidate) in cache for candidate in day.candidates)
            limit = (
                2
                if state.get("expanding_places")
                else max(0, _pace_limit(state["request"]) - cached_count)
            )
            for candidate in candidate_batch(
                day.candidates,
                queried,
                limit,
                prefer_meal_balance=not state.get("expanding_places", False),
                daily_budget=daily_place_budget(
                    state["request"], state.get("trip_quotes")
                ),
            ):
                unique.setdefault(key(candidate), candidate)
        queried.update(unique)
        results = await _parallel(
            list(unique.values()),
            lambda candidate: self.provider.search(candidate, state["destination"]),
        )
        temporary_names = {
            candidate.name
            for candidate, result in zip(unique.values(), results)
            if isinstance(result, MapsProviderError) and result.transient
        }
        for candidate_key, venue in zip(unique, results):
            if isinstance(venue, asyncio.CancelledError):
                raise venue
            if isinstance(venue, MapsProviderError) and venue.kind in {
                "unauthorized",
                "invalid",
            }:
                raise venue
            if isinstance(venue, MapsProviderError) and venue.kind in {
                "not_found",
                "place_data",
            }:
                # One stale or malformed place must not discard verified peers.
                # Keep it out of the success cache; deferred venues can replace it.
                logger.info("Course candidate unavailable: kind=%s", venue.kind)
                continue
            if isinstance(venue, VerifiedPlace):
                venue.place.placeId = (
                    ("places/" + _canonical_id(venue.place.placeId))
                    if _canonical_id(venue.place.placeId)
                    else ""
                )
                cache[candidate_key] = venue
        recent_ids = set().union(*state["recent"]) if state["recent"] else set()
        max_stops = _pace_limit(state["request"])
        minimum_visits = 3
        all_plans, failures = [], []
        for index, day in enumerate(state["draft_data"].days):
            await asyncio.sleep(0)
            if index in saved:
                all_plans.append([saved[index]["selected"]])
                continue
            day_date = date.fromisoformat(
                state["request"].tripCondition.startDate
            ) + timedelta(days=index)
            pool = pools.setdefault(index, {})
            current_keys = []
            rejected = []
            for candidate in day.candidates:
                venue = cache.get(key(candidate))
                if isinstance(venue, BaseException) or not isinstance(
                    venue, VerifiedPlace
                ):
                    if key(candidate) in queried:
                        rejected.append(candidate.name)
                    continue
                place = venue.place
                if (
                    not individual_place_category_supported(place.category)
                    or place.placeId in reserved_ids
                    or not place.placeId
                    or not place.address
                    or not math.isfinite(place.latitude)
                    or not math.isfinite(place.longitude)
                    or not state["destination"].contains(
                        place.latitude, place.longitude
                    )
                ):
                    rejected.append(candidate.name)
                    continue
                if not meal_category_supported(place.category, candidate.meal):
                    rejected.append(candidate.name)
                    continue
                pool[(place.placeId, candidate.meal)] = (candidate, venue)
                current_keys.append((place.placeId, candidate.meal))
            # Recheck retained facts against other days: a later successful day can
            # reserve a venue that was available when this day's pool was created.
            pool = {
                key: item
                for key, item in pool.items()
                if item[1].place.placeId not in reserved_ids
                and individual_place_category_supported(item[1].place.category)
                and meal_category_supported(item[1].place.category, item[0].meal)
                and item[1].place.address
                and math.isfinite(item[1].place.latitude)
                and math.isfinite(item[1].place.longitude)
                and state["destination"].contains(
                    item[1].place.latitude, item[1].place.longitude
                )
            }
            # Three drafts of at most ten candidates is the request-wide hard cap.
            ordered_keys = dict.fromkeys([*current_keys, *pool])
            pools[index] = {key: pool[key] for key in ordered_keys if key in pool}
            pools[index] = dict(list(pools[index].items())[:30])
            available = list(pools[index].values())
            plans = _day_plans(
                available,
                day_date,
                max_stops,
                minimum_visits,
                recent_ids,
                state["rejected_plans"].get(index),
                recent_profiles=state.get("recent_profiles"),
                daily_budget=daily_place_budget(
                    state["request"], state.get("trip_quotes")
                ),
            )
            if state.get("refilling"):
                # Compare different real attraction subsets as well as ordering:
                # the four nearest optimistic orders may share one unusable venue.
                for _, excluded in [item for item in available if _core_visit(item)][
                    :5
                ]:
                    alternatives = [
                        item
                        for item in available
                        if item[1].place.placeId != excluded.place.placeId
                    ]
                    for plan in _day_plans(
                        alternatives,
                        day_date,
                        max_stops,
                        3,
                        recent_ids,
                        state["rejected_plans"].get(index),
                        plan_limit=1,
                        recent_profiles=state.get("recent_profiles"),
                        daily_budget=daily_place_budget(
                            state["request"], state.get("trip_quotes")
                        ),
                    ):
                        if _plan_signature(plan) not in {
                            _plan_signature(value) for value in plans
                        }:
                            plans.append(plan)
                shorter = _shorter_visits(available, state["request"])
                for plan in _day_plans(
                    shorter,
                    day_date,
                    5,
                    3,
                    recent_ids,
                    state["rejected_plans"].get(index),
                    recent_profiles=state.get("recent_profiles"),
                    daily_budget=daily_place_budget(
                        state["request"], state.get("trip_quotes")
                    ),
                ):
                    if _plan_signature(plan) not in {
                        _plan_signature(value) for value in plans
                    }:
                        plans.append(plan)
                plans = plans[:12]
            if not plans:
                # This is an internal safety snapshot, never a completed result
                # until independent fullness supplementation is exhausted.
                for core_count in (2, 1):
                    plans = _day_plans(
                        available,
                        day_date,
                        max_stops,
                        core_count,
                        recent_ids,
                        state["rejected_plans"].get(index),
                        recent_profiles=state.get("recent_profiles"),
                        daily_budget=daily_place_budget(
                            state["request"], state.get("trip_quotes")
                        ),
                    )
                    if plans:
                        break
            all_plans.append(plans)
            if not plans:
                failures.append(
                    f"{index + 1}일차 {day_date}: 점심·저녁과 명소 {minimum_visits}곳의 방문/식사 시간 조합 부족. 확인된 후보: {', '.join(candidate.name for candidate, _ in available)}. 일시 조회 실패(재조회 가능): {', '.join(name for name in rejected if name in temporary_names)}. 위치/유형/주소 검증 미충족: {', '.join(name for name in rejected if name not in temporary_names)}. 다른 검색 가능한 장소명과 식사 대안을 제안하세요."
                )
        return {
            "day_plans": all_plans,
            "failures": failures,
            "feedback": "",
            "place_cache": cache,
            "candidate_pools": pools,
            "queried_place_keys": queried,
            "pending_place_days": {
                index
                for index, day in enumerate(state["draft_data"].days)
                if index not in saved
                and any(key(candidate) not in queried for candidate in day.candidates)
            },
            "expanding_places": False,
        }

    async def verify_routes(self, state: CourseState) -> dict:
        """Check actual transfers or preserve venues with explicit unknown travel."""
        _progress(
            "verify_routes",
            language=state["request"].language,
        )
        validated = dict(state["validated_days"])
        cache = state["route_cache"]
        failures = list(state["failures"])
        rejected_plans = {
            index: set(signatures)
            for index, signatures in state["rejected_plans"].items()
        }
        used = {
            _canonical_id(venue.place.placeId)
            for value in validated.values()
            for _, venue in value["selected"]
        }
        # Deferred venues can produce several validation passes for one draft.
        # Share failures across those passes; a fresh draft/refill can retry.
        # Failures never enter the successful or persistent provider cache.
        pass_failures: dict[tuple, MapsProviderError] = dict(
            state.get("route_pass_failures", {})
        )
        mode_service_errors = {
            key[2]: error for key, error in pass_failures.items() if error.transient
        }
        route_service_error: MapsProviderError | None = None

        def route_key(
            left: VerifiedPlace,
            right: VerifiedPlace,
            departure: datetime | None,
            mode: str | None = None,
        ) -> tuple:
            mode = mode or (
                "transit"
                if state["destination"].country_code == "KR"
                or distance_meters(left, right) > 1500
                else "walking"
            )
            return (
                left.place.placeId,
                right.place.placeId,
                mode,
                departure.isoformat() if departure and mode == "transit" else None,
            )

        async def actual_route(
            left: VerifiedPlace,
            right: VerifiedPlace,
            departure: datetime | None,
            mode: str,
        ) -> TransportToNextSchema:
            """Share successful and failed actual mode lookups within their bounds."""
            nonlocal route_service_error
            key = route_key(left, right, departure, mode)
            if key in cache:
                return cache[key]
            if route_service_error is not None:
                raise route_service_error
            if mode in mode_service_errors:
                raise mode_service_errors[mode]
            if key in pass_failures:
                raise pass_failures[key]
            try:
                route = await self.provider.route(
                    left,
                    right,
                    mode=mode,
                    departure_time=departure if mode == "transit" else None,
                )
            except MapsProviderError as error:
                pass_failures[key] = error
                if error.kind == 'unauthorized':
                    route_service_error = error
                elif error.transient:
                    mode_service_errors[mode] = error
                raise
            except (ValueError, TimeoutError, httpx.HTTPError) as error:
                failure = MapsProviderError('route_data' if isinstance(error, ValueError) else 'transient')
                pass_failures[key] = failure
                if failure.transient:
                    mode_service_errors[mode] = failure
                raise failure from None
            if not has_route_metrics(route):
                failure = MapsProviderError('route_data')
                pass_failures[key] = failure
                raise failure
            if valid_route(route):
                cache[key] = route
            return route

        async def lookup(
            left: VerifiedPlace, right: VerifiedPlace, departure: datetime | None
        ) -> TransportToNextSchema:
            """Use real mode alternatives before returning unavailable travel.

            The preferred mode still needs one request on success. Walking is
            a fallback only within 3 km; a measured taxi route does not assume
            the traveler has a car. Exhausted outages are isolated by mode so
            one unavailable service cannot suppress another usable mode.
            """
            key = route_key(left, right, departure)
            preferred = key[2]
            if key in cache:
                return cache[key]
            modes = ["transit", "taxi"]
            if preferred == "walking":
                modes.insert(0, "walking")
            elif distance_meters(left, right) <= 3000:
                modes.insert(1, "walking")
            last_error = None
            for mode in modes:
                try:
                    # Keep actual overlong metrics for the existing plan check;
                    # never replace them with unknown or an estimated duration.
                    return await actual_route(left, right, departure, mode)
                except MapsProviderError as error:
                    last_error = error
                    if error.kind not in {"no_route", "route_data", "transient"}:
                        break
            if last_error is not None:
                logger.warning("Course transfer unavailable: kind=%s status=%s", last_error.kind, last_error.status_code)
            return unavailable_route()

        async def verify_one(item: tuple[int, list[Visit]]) -> tuple:
            index, selected = item
            day_date = date.fromisoformat(
                state["request"].tripCondition.startDate
            ) + timedelta(days=index)
            routes = []
            current = 540
            timing_confirmed = True
            temporary_failure = False
            try:
                for left, right in pairwise(selected):
                    candidate, venue = left
                    opening, latest = MEAL_WINDOWS[candidate.meal]
                    arrival = _opening_start(
                        venue.periods,
                        day_date,
                        max(current, opening),
                        candidate.stay_minutes,
                        latest,
                    )
                    if arrival is None:
                        raise CourseGenerationError(
                            f"{candidate.name}: 방문/식사 시간 안에 방문할 수 없습니다."
                        )
                    departure = None
                    if timing_confirmed and state["destination"].country_code == "KR":
                        departure = datetime.combine(
                            day_date, time(), tzinfo=ZoneInfo("Asia/Seoul")
                        ) + timedelta(minutes=arrival + candidate.stay_minutes)
                    route = await lookup(venue, right[1], departure)
                    if not usable_route(route):
                        raise CourseGenerationError(
                            f"{candidate.name} → {right[0].name}: 경로 정보가 불완전하거나 이동 시간이 90분을 초과합니다."
                        )
                    routes.append(route)
                    timing_confirmed = timing_confirmed and not is_unknown_route(route)
                    current = arrival + candidate.stay_minutes + minimum_travel_minutes(route) + 10
                day = _schedule_day(
                    selected,
                    routes,
                    day_date,
                    index + 1,
                )
            except MapsProviderError as error:
                if error.kind in {"unauthorized", "invalid"}:
                    raise
                temporary_failure = error.transient
                last_error = (
                    ("일시적인 경로 조회 장애" if temporary_failure else "경로 미확인")
                    + f": {left[0].name} → {right[0].name}. 서로 가까운 다른 장소가 필요합니다."
                )
            except (CourseGenerationError, ValueError) as error:
                last_error = str(error)
            else:
                return {"selected": selected, "routes": routes, "day": day}, "", False
            return None, last_error, temporary_failure

        plan_limit = (
            16
            if state.get("repairing")
            else 12
            if state.get("refilling")
            else MAX_DAY_PLANS
        )
        remaining = {
            index: list(plans[:plan_limit])
            for index, plans in enumerate(state["day_plans"])
            if index not in validated and plans
        }
        last_errors: dict[int, str] = {}
        unknown_days: dict[int, list[dict]] = {}
        while remaining:
            assignments = _choose_disjoint_plans(remaining, used)
            if not assignments:
                break
            results = await _parallel(list(assignments.items()), verify_one)
            for (index, selected), outcome in zip(assignments.items(), results):
                if isinstance(outcome, BaseException):
                    raise outcome
                record, error, temporary = outcome
                if record is not None and not any(is_unknown_route(route) for route in record['routes']):
                    validated[index] = record
                    used.update(
                        _canonical_id(venue.place.placeId) for _, venue in selected
                    )
                    remaining.pop(index, None)
                else:
                    if record is not None:
                        unknown_days.setdefault(index, []).append(record)
                    else:
                        last_errors[index] = error
                    remaining[index].remove(selected)
                    if record is None and not temporary:
                        rejected_plans.setdefault(index, set()).add(
                            _plan_signature(selected)
                        )
                    if not remaining[index]:
                        remaining.pop(index)
        # Preserve the model's verified five-stop order when a small optimistic
        # shortlist misses it. It still needs route-state and full-stay checks;
        # this bounded local attempt precedes further venue/model requests.
        if not state.get("repairing"):
            local = {}
            for index, plans in enumerate(state["day_plans"]):
                if index in validated or not plans:
                    continue
                day_date = date.fromisoformat(
                    state["request"].tripCondition.startDate
                ) + timedelta(days=index)
                floor = _proposal_floor(
                    list(state["candidate_pools"].get(index, {}).values()),
                    day_date,
                )
                if (
                    floor
                    and not {_canonical_id(venue.place.placeId) for _, venue in floor}
                    & used
                    and _plan_signature(floor) not in rejected_plans.get(index, set())
                    and _plan_signature(floor)
                    not in {_plan_signature(plan) for plan in plans[:plan_limit]}
                ):
                    local[index] = [floor]
            assignments = _choose_disjoint_plans(local, used) if local else {}
            results = await _parallel(list(assignments.items()), verify_one)
            for (index, selected), outcome in zip(assignments.items(), results):
                if isinstance(outcome, BaseException):
                    raise outcome
                record, error, temporary = outcome
                if record is not None and not any(is_unknown_route(route) for route in record['routes']):
                    validated[index] = record
                    used.update(
                        _canonical_id(venue.place.placeId) for _, venue in selected
                    )
                else:
                    if record is not None:
                        unknown_days.setdefault(index, []).append(record)
                    else:
                        last_errors[index] = error
                    if record is None and not temporary:
                        rejected_plans.setdefault(index, set()).add(
                            _plan_signature(selected)
                        )
        # Prefer fully measured alternatives, then retain the best disjoint
        # venue itineraries instead of redrafting solely for missing travel.
        pending = {
            index: [record['selected'] for record in sorted(records, key=lambda record: sum(is_unknown_route(route) for route in record['routes']))]
            for index, records in unknown_days.items() if index not in validated
        }
        unknown_counts = {
            (index, _plan_signature(record['selected'])): sum(is_unknown_route(route) for route in record['routes'])
            for index, records in unknown_days.items() for record in records
        }
        for index, selected in _choose_disjoint_plans(
            pending, used, plan_cost=lambda index, plan: unknown_counts[index, _plan_signature(plan)]
        ).items():
            record = next(record for record in unknown_days[index] if record['selected'] == selected)
            validated[index] = record
            used.update(_canonical_id(venue.place.placeId) for _, venue in selected)
        for index in range(len(state["day_plans"])):
            if index not in validated and state["day_plans"][index]:
                failures.append(
                    f"{index + 1}일차: "
                    + last_errors.get(
                        index, "다른 일차와 겹치지 않는 실제 장소 조합이 부족합니다."
                    )
                )
        if failures:
            if (
                state.get("verified_fallback")
                and not _needs_fullness(state)
                and not state.get("refilling")
            ):
                return {
                    "route_cache": cache,
                    "route_pass_failures": pass_failures,
                    "restore_fallback": True,
                    "feedback": "",
                }
            preserved = "; ".join(
                f"{index + 1}일차 유지: {', '.join(candidate.name for candidate, _ in value['selected'])}"
                for index, value in sorted(validated.items())
            )
            return {
                "validated_days": validated,
                "route_cache": cache,
                "rejected_plans": rejected_plans,
                "route_pass_failures": pass_failures,
                "feedback": " / ".join(failures) + " / " + preserved,
            }
        selected = [
            validated[index]["selected"] for index in range(len(state["day_plans"]))
        ]
        routes = [
            validated[index]["routes"] for index in range(len(state["day_plans"]))
        ]
        metadata = _selection_metadata(selected)
        scores = overlap_scores(
            {venue.place.placeId for day in selected for _, venue in day},
            metadata["attraction_ids"],
            state.get("recent_profiles", []),
        )
        area_overlap = planning_overlap(
            metadata["areas"], state.get("recent_profiles", []), "areas"
        )
        repeated = max(scores.values()) > MAX_OVERLAP or area_overlap > MAX_OVERLAP
        shortages = [_visit_shortage(day) for day in selected]
        rank = (
            sum(value[0] for value in shortages),
            sum(value[1] for value in shortages),
            sum(is_unknown_route(route) for day in routes for route in day),
            max(scores.values()),
            area_overlap,
        )
        snapshot = copy.deepcopy(
            {
                "draft_data": state["draft_data"],
                "validated_days": validated,
                "selected": selected,
                "routes": routes,
                "diversity_scores": {**scores, "planned_area_overlap": area_overlap},
                "repeated": repeated,
                "rank": rank,
            }
        )
        previous = state.get("verified_fallback")
        if previous and previous["rank"] <= rank:
            snapshot = previous
        logger.info(
            "Course diversity: place_overlap=%.2f attraction_overlap=%.2f planned_area_overlap=%.2f attempt=%d",
            scores["place_overlap"],
            scores["attraction_overlap"],
            area_overlap,
            state["attempt"],
        )
        updates = {
            "validated_days": validated,
            "route_cache": cache,
            "route_pass_failures": pass_failures,
            "selected": selected,
            "routes": routes,
            "repeated": repeated,
            "feedback": "",
            "verified_fallback": snapshot,
            "diversity_scores": {**scores, "planned_area_overlap": area_overlap},
        }
        underfull = any(any(value) for value in shortages)
        if (
            underfull
            and not state.get("repairing")
            and (state["attempt"] < _max_drafts(state) or _can_refill(state))
            and (
                state.get("deadline", float("inf"))
                - asyncio.get_running_loop().time()
                - 1.0
                >= MIN_DAILY_DRAFT_BUDGET_SECONDS
            )
        ):
            retained = {
                index: value
                for index, value in validated.items()
                if not any(_visit_shortage(value["selected"]))
            }
            missing = "; ".join(
                f"{index + 1}일차: 총 장소 {count}곳, 관광·체험 명소 {core}곳 부족. 검증된 가까운 후보 유지: "
                + ", ".join(
                    candidate.name for candidate, _ in validated[index]["selected"]
                )
                for index, (count, core) in enumerate(shortages)
                if count or core
            )
            updates.update(
                {
                    "validated_days": retained,
                    "feedback": missing
                    + ". 부족한 관광·체험 슬롯을 가까운 실제 명소로 보충하고 성공한 일차는 유지하세요.",
                }
            )
        elif underfull and not state.get("repairing"):
            if (
                state.get("deadline", float("inf"))
                - asyncio.get_running_loop().time()
                - 1.0
                < MIN_DAILY_DRAFT_BUDGET_SECONDS
            ):
                updates["restore_fallback"] = True
            else:
                retained = {
                    index: value
                    for index, value in validated.items()
                    if not any(_visit_shortage(value["selected"]))
                }
                updates.update(
                    {
                        "validated_days": retained,
                        "feedback": "부족한 일차의 기존 후보로 최소 5곳의 실제 이동과 체류 시간을 마지막으로 비교합니다.",
                    }
                )
        elif underfull and previous and previous["rank"] <= rank:
            updates["restore_fallback"] = True
        elif (
            repeated
            and not state.get("refilling")
            and state["attempt"] < _max_drafts(state)
            and _optional_budget(state) > 0.1
        ):
            updates.update(
                {
                    "validated_days": {},
                    "feedback": "최근 코스의 명소 또는 계획 권역이 반복됩니다. 취향을 유지하고 다른 권역의 실제 장소를 제안하세요. 이미 확인한 후보: "
                    + ", ".join(
                        candidate.name
                        for value in validated.values()
                        for candidate, _ in value["selected"]
                    ),
                }
            )
        elif (
            repeated
            and not state.get("refilling")
            and previous
            and previous["rank"] <= rank
        ):
            updates["restore_fallback"] = True
        return updates

    async def schedule(self, state: CourseState) -> dict:
        """Confirm final consistency and reuse the already validated day schedules."""
        _progress(
            "schedule",
            language=state["request"].language,
        )
        expected = state["request"].tripCondition.totalDays
        ids = [
            _canonical_id(venue.place.placeId)
            for selected in state["selected"]
            for _, venue in selected
        ]
        if (
            len(state["selected"]) != expected
            or len(state["routes"]) != expected
            or not all(ids)
            or len(ids) != len(set(ids))
        ):
            raise CourseGenerationError(
                "전체 날짜의 장소 중복 또는 일정 누락을 확인했습니다."
            )
        start_date = date.fromisoformat(state["request"].tripCondition.startDate)
        days = []
        for index, (selected, routes) in enumerate(
            zip(state["selected"], state["routes"])
        ):
            if len(routes) != len(selected) - 1 or any(
                not usable_route(route) for route in routes
            ):
                raise CourseGenerationError(
                    "이동 정보의 확인·미확보 상태가 올바르지 않습니다."
                )
            record = state["validated_days"].get(index, {})
            day = record.get("day")
            if (
                not isinstance(day, DayItinerarySchema)
                or record.get("selected") != selected
                or record.get("routes") != routes
                or day.day != index + 1
                or day.date != (start_date + timedelta(days=index)).isoformat()
                or [stop.place.placeId for stop in day.stops]
                != [venue.place.placeId for _, venue in selected]
            ):
                raise CourseGenerationError(
                    "검증된 일정과 최종 장소·경로가 일치하지 않습니다."
                )
            # Venue windows and full stays were checked by verify_one. Unknown
            # transfers retain provisional internal clocks, not verified timing.
            days.append(day.model_copy(deep=True))
        return {"days": days, "feedback": ""}

    async def finalize(self, state: CourseState) -> dict:
        """Compose grounded course copy and atomically record recent history."""
        _progress(
            "finalize",
            language=state["request"].language,
        )
        request = state["request"]
        trip = request.tripCondition
        apply_personalized_reasons(request, state["days"])
        course = CourseSchema(
            title=f"{trip.destinationCity} {trip.totalDays}일 여행",
            destinationCountry=trip.destinationCountry,
            destinationCity=trip.destinationCity,
            startDate=trip.startDate,
            totalDays=trip.totalDays,
            totalCost=course_cost(state["days"])
            + (state["trip_quotes"].total_krw if state.get("trip_quotes") else 0),
            tags=verified_course_tags(state["days"]),
            itinerary=ItinerarySchema(days=state["days"]),
        )
        ids = {
            stop.place.placeId for day in course.itinerary.days for stop in day.stops
        }
        unavailable = state.get("history_unavailable", False)
        novel = False
        try:
            budget = min(2.0, _optional_budget(state))
            if budget <= 0.1:
                raise TimeoutError(
                    "History storage skipped to preserve completion margin"
                )
            async with asyncio.timeout(budget):
                metadata = _selection_metadata(state["selected"])
                novel = await self.history.record_if_novel(
                    history_key(request),
                    ids,
                    metadata=metadata,
                    max_overlap=None if state.get("repeated") else MAX_OVERLAP,
                )
                if (
                    not novel
                    and state["attempt"] < _max_drafts(state)
                    and _optional_budget(state) > 0.1
                ):
                    recent, profiles = await self._read_history(request)
                    return {
                        "validated_days": {},
                        "feedback": "동시에 생성된 최근 코스와 유사합니다. 취향과 검증된 후보를 유지하며 다른 권역·명소를 제안하세요.",
                        "recent": recent,
                        "recent_profiles": profiles,
                    }
                if not novel and not state.get("repeated"):
                    # Explicit soft acceptance after bounded concurrency retries.
                    novel = await self.history.record_if_novel(
                        history_key(request),
                        ids,
                        metadata=metadata,
                    )
                    state = {**state, "repeated": True}
        except OSError, sqlite3.Error, TimeoutError:
            logger.warning("Course history write unavailable")
            unavailable = True
        if unavailable:
            _progress("history_unavailable", language=request.language)
        elif not novel or state.get("repeated"):
            _progress("repeated_supplemental", language=request.language)
        return {"course": course, "feedback": ""}

    async def localize_display(self, state: CourseState) -> dict:
        """Translate display text within the existing optional request budget.

        The service preserves verified facts and original guidance on failure.
        No new provider, model call or retry budget is introduced by this node.
        """
        request = state["request"]
        _progress("localize", language=request.language)
        course = await localize_course(
            request, state["course"],
            timeout_seconds=min(LOCALIZATION_TIMEOUT_SECONDS, max(0.0, state.get('deadline', float('inf')) - asyncio.get_running_loop().time() - 1.0)),
        )
        return {"course": course}

    async def finalize_course(self, state: CourseState) -> dict:
        """Disclose benchmark amounts and return the localized course."""
        request, course = state["request"], state["course"]
        quotes = state.get("trip_quotes")
        if quotes and quotes.estimated_items:
            get_stream_writer()(
                {
                    "step": "GENERATING_ROUTE",
                    "message": progress_message(
                        request.language, "quote_estimated",
                        items=tuple(
                            "transport" if item == "flight" else item
                            for item in quotes.estimated_items
                        ),
                    ),
                }
            )
        if course.totalCost > request.tripCondition.budget:
            get_stream_writer()(
                {
                    "step": "GENERATING_ROUTE",
                    "message": progress_message(
                        request.language,
                        "budget_over",
                        total=course.totalCost,
                        budget=request.tripCondition.budget,
                    ),
                }
            )
        logger.info(
            "Travel cost summary: mode=%s transport=%s lodging=%s total_cost_krw=%d budget_krw=%s",
            quotes.transport_mode if quotes else "unknown",
            quotes.flight if quotes else None,
            quotes.lodging if quotes else None,
            course.totalCost,
            request.tripCondition.budget,
        )
        return {"course": course}

    # Recovery with verified candidates and snapshots.

    async def refill(self, state: CourseState) -> dict:
        """Supplement only unfinished dates with an independent bounded budget.

        Successful dates and verified candidate/route caches remain unchanged.
        Named model proposals are verified before local recombination. Temporary
        model failures retain candidates for recovery instead of losing them.
        """
        _progress(
            "refill",
            language=state["request"].language,
        )
        saved = {
            index: value
            for index, value in state["validated_days"].items()
            if not any(_visit_shortage(value["selected"]))
        }
        existing = (
            dict(state.get("partial_drafts", {})) if not state.get("draft_data") else {}
        )
        existing.update(
            {
                index: DraftDay(
                    candidates=[candidate for candidate, _ in value["selected"]]
                )
                for index, value in saved.items()
            }
        )
        pending = [
            index
            for index in range(state["request"].tripCondition.totalDays)
            if index not in existing
        ]
        feedback = (
            state.get("feedback", "")
            + "\n최소 5곳과 관광·체험 3곳을 채우는 보충입니다. 기존 취향을 유지하고 아래 확인된 명소 주변의 가까운 공식 장소를 우선하세요. slow_stay/long_stay의 긴 체류와 테마파크·하이킹 같은 긴 활동은 유지하세요. 그 밖의 일정에서 꼭 필요한 경우에만 박물관·미술관 60분, 근거리 공원·정원 45분, 식사 45분을 계획 대안으로 제안하세요.\n"
        )
        for index in pending:
            values = list(state["candidate_pools"].get(index, {}).values())
            feedback += (
                f"{index + 1}일차 확인된 후보 유지: "
                + ", ".join(
                    f"{candidate.name}({venue.place.category}, {candidate.meal})"
                    for candidate, venue in values
                )
                + "\n"
            )
        updates = {
            "refill_attempt": state.get("refill_attempt", 0) + 1,
            "route_pass_failures": {},
            "refilling": True,
            "repairing": False,
            "validated_days": saved,
            "restore_fallback": False,
            "feedback": "",
        }
        try:
            deadline = state["deadline"] - 10.0 if "deadline" in state else None
            result = await draft_candidates(
                state["request"],
                sorted(set().union(*state["recent"])) if state["recent"] else [],
                self._history_feedback(state) + "\n" + feedback,
                pending_days=pending,
                existing_days=existing,
                deadline=deadline,
            )
        except PartialDraftError as error:
            if state.get("draft_data"):
                result = state["draft_data"].model_copy(deep=True)
                for index, day in error.days.items():
                    result.days[index] = day
            else:
                updates["partial_drafts"] = error.days
                updates["feedback"] = "부족한 일차의 후보 응답을 다시 확인합니다."
                return updates
        except Exception as error:
            if not _recoverable_draft(error):
                raise
            if not state.get("draft_data"):
                updates["feedback"] = "부족한 일차의 후보 응답을 다시 확인합니다."
                return updates
            result = state["draft_data"].model_copy(deep=True)
        if len(result.days) != state["request"].tripCondition.totalDays:
            if not state.get("draft_data"):
                updates["feedback"] = (
                    "요청한 여행 일수와 후보 개수가 일치하지 않습니다."
                )
                return updates
            result = state["draft_data"].model_copy(deep=True)
        else:
            result = result.model_copy(deep=True)
        for index, value in existing.items():
            result.days[index] = value
        updates.update(
            {
                "draft_data": result,
                "partial_drafts": {},
                "queried_place_keys": set(state["place_cache"]),
                "expanding_places": False,
            }
        )
        return updates

    async def repair(self, state: CourseState) -> dict:
        """Use retained real venues for one final bounded, less dense schedule."""
        _progress(
            "repair",
            language=state["request"].language,
        )
        saved = {
            index: value
            for index, value in state["validated_days"].items()
            if not any(_visit_shortage(value["selected"]))
        }
        reserved = {
            venue.place.placeId
            for value in saved.values()
            for _, venue in value["selected"]
        }
        recent_ids = set().union(*state["recent"]) if state["recent"] else set()
        start = date.fromisoformat(state["request"].tripCondition.startDate)
        plans, failures = [], []
        for index in range(state["request"].tripCondition.totalDays):
            if index in saved:
                plans.append([saved[index]["selected"]])
                continue
            available = [
                item
                for item in state["candidate_pools"].get(index, {}).values()
                if item[1].place.placeId not in reserved
            ]
            alternatives = _day_plans(
                available,
                start + timedelta(days=index),
                5,
                3,
                recent_ids,
                state["rejected_plans"].get(index),
                plan_limit=4,
                compact=True,
                recent_profiles=state.get("recent_profiles"),
                daily_budget=daily_place_budget(
                    state["request"], state.get("trip_quotes")
                ),
            )
            shorter = _shorter_visits(available, state["request"])
            alternatives.extend(
                _day_plans(
                    shorter,
                    start + timedelta(days=index),
                    5,
                    3,
                    recent_ids,
                    state["rejected_plans"].get(index),
                    plan_limit=4,
                    compact=True,
                    recent_profiles=state.get("recent_profiles"),
                    daily_budget=daily_place_budget(
                        state["request"], state.get("trip_quotes")
                    ),
                )
            )
            for stop_count, core_count in ((4, 2), (3, 1)):
                alternatives.extend(
                    _day_plans(
                        available,
                        start + timedelta(days=index),
                        stop_count,
                        core_count,
                        recent_ids,
                        state["rejected_plans"].get(index),
                        plan_limit=4,
                        compact=True,
                        recent_profiles=state.get("recent_profiles"),
                        daily_budget=daily_place_budget(
                            state["request"], state.get("trip_quotes")
                        ),
                    )
                )
            alternatives = list(
                {_plan_signature(plan): plan for plan in alternatives}.values()
            )
            plans.append(alternatives)
            if not alternatives:
                failures.append(
                    f"{index + 1}일차: 점심·저녁과 명소를 포함한 검증 가능한 대안이 없습니다."
                )
        return {
            "validated_days": saved,
            "day_plans": plans,
            "failures": failures,
            "feedback": "",
            "repairing": True,
            "route_pass_failures": {},
        }

    async def restore(self, state: CourseState) -> dict:
        """Restore a complete feasible snapshot, including its own draft metadata."""
        _progress(
            "restore",
            language=state["request"].language,
        )
        logger.info("Course diversity fallback: retained verified itinerary")
        snapshot = state["verified_fallback"]
        return {
            **{
                key: copy.deepcopy(value)
                for key, value in snapshot.items()
                if key != "rank"
            },
            "feedback": "",
            "restore_fallback": False,
            "attempt": _max_drafts(state),
        }

    # Shared history, time budgets and conditional routing.

    async def _read_history(
        self, request: CourseRequestSchema
    ) -> tuple[list[set[str]], list[dict]]:
        # One transaction supplies both views of the same recent history.
        profiles = await self.history.recent_profiles(history_key(request))
        return [set(item["place_ids"]) for item in profiles], profiles

    def _history_feedback(self, state: CourseState) -> str:
        profiles = state.get("recent_profiles", [])
        areas = sorted({value for item in profiles for value in item.get("areas", [])})
        experiences = sorted(
            {value for item in profiles for value in item.get("experiences", [])}
        )
        return (
            budget_guidance(state["request"], state.get("trip_quotes")) + "\n"
            + (
                (
                    "최근 계획 권역: "
                    + ", ".join(areas[:30])
                    + ". 최근 경험 구성: "
                    + ", ".join(experiences)
                    + ". "
                    "사용자의 높은 취향을 유지하며 최근 계획과 다른 권역을 우선하세요. 권역은 하루 안에서 가까운 장소로 묶으세요. 낮은 취향을 다양성 때문에 강제하지 마세요. "
                    "각 후보에 모델 계획 planned_area와 experiences를 작성하세요. 권역 이름만 바꿔 동일 장소를 다시 제안하지 마세요."
                )
                if profiles
                else ""
            )
        )

    async def _run_with_fallback(self, node: CourseNode, state: CourseState) -> dict:
        """Bound novelty-only work while preserving time for safe completion."""
        if not state.get("verified_fallback"):
            return await node(state)
        compulsory = _needs_fullness(state) or state.get("refilling")
        budget = (
            max(
                0.0,
                state.get("deadline", float("inf"))
                - asyncio.get_running_loop().time()
                - 1.0,
            )
            if compulsory
            else _optional_budget(state)
        )
        if budget <= 0.1:
            return {"restore_fallback": True, "feedback": ""}
        try:
            async with asyncio.timeout(None if math.isinf(budget) else budget):
                return await node(state)
        except TimeoutError:
            logger.info(
                "Course verified fallback: %s validation timeout",
                "required" if compulsory else "optional",
            )
            return {"restore_fallback": True, "feedback": ""}

    def bounded(self, node: CourseNode) -> CourseNode:
        """Apply the existing fallback budget to candidate and verification nodes."""

        async def run(state: CourseState) -> dict:
            return await self._run_with_fallback(node, state)

        return run

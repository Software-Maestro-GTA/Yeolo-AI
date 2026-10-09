"""Execute course generation nodes using injected Maps and history boundaries.

Normal generation and recovery return partial CourseState updates. Request data
and caches remain in state; this class holds only provider and history handles."""

import asyncio
import copy
import inspect
import logging
import math
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

from langgraph.config import get_stream_writer

from app.agent.course_drafting import (
    MIN_DAILY_DRAFT_BUDGET_SECONDS,
    _recoverable_draft,
    draft_candidates,
)
from app.agent.course_state import (
    Candidate,
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
    tourism_category_supported,
)
from app.core.config import settings
from app.schemas.course import (
    CourseRequestSchema,
    CourseSchema,
    DayItinerarySchema,
    ItinerarySchema,
    TransportToNextSchema,
)
from app.services.course_diversity import (
    MAX_OVERLAP,
    overlap_scores,
    planning_overlap,
)
from app.services.course_history import CourseHistory, history_key
from app.services.course_images import enrich_course_images
from app.services.course_planning import (
    MAX_DAY_PLANS,
    MEAL_WINDOWS,
    _canonical_id,
    _choose_disjoint_plans,
    _core_visit,
    _day_plans,
    _meal_capable,
    _opening_start,
    _pace_limit,
    _plan_signature,
    _schedule_day,
    _selection_metadata,
    _shorter_visits,
    _visit_shortage,
)
from app.services.course_reasons import (
    RULES,
    apply_personalized_reasons,
    verified_course_tags,
)
from app.services.course_routing import (
    distance_meters,
    valid_route,
)

logger = logging.getLogger(__name__)


def _progress(message: str) -> None:
    get_stream_writer()({"step": "GENERATING_ROUTE", "message": message})


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
        provider: Venue, route and photo verification boundary.
        history: Recent course storage boundary.

    Dependencies live here; drafts, caches, retries and snapshots live only in
    CourseState. Each node returns the same partial state updates as before.
    """

    def __init__(self, provider: VerifiedMapsProvider, history: CourseHistory) -> None:
        self.provider = provider
        self.history = history

    # Normal generation.

    async def prepare(self, state: CourseState) -> dict:
        """Resolve the destination and initialize request-local state."""
        _progress("여행 조건과 최근 코스 이력을 확인하고 있습니다.")
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
            "route_cache": {},
            "candidate_pools": {},
            "rejected_plans": {},
            "repairing": False,
            "repeated": False,
            "discovered_days": set(),
            "restore_fallback": False,
            "verified_fallback": {},
            "refill_attempt": 0,
            "refilling": False,
            "attraction_discovered_days": set(),
        }

    async def draft(self, state: CourseState) -> dict:
        """Propose missing days while retaining successful day drafts."""
        _progress("취향에 맞는 장소 후보와 식사 대안을 구성하고 있습니다.")
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
        }

    async def verify_places(self, state: CourseState) -> dict:
        """Verify venue facts, supplement nearby candidates and form day plans."""
        _progress("장소의 실제 위치·도시·영업 정보를 병렬로 확인하고 있습니다.")
        saved = state["validated_days"]
        reserved_ids = {
            venue.place.placeId
            for value in saved.values()
            for _, venue in value["selected"]
        }
        cache = state["place_cache"]
        pools = {index: dict(pool) for index, pool in state["candidate_pools"].items()}
        discovered_days = set(state.get("discovered_days", set()))
        attraction_discovered_days = set(state.get("attraction_discovered_days", set()))

        def key(candidate: Candidate) -> tuple:
            return (
                candidate.name.strip().casefold(),
                candidate.english_name.strip().casefold(),
                candidate.meal,
            )

        unique = {
            key(candidate): candidate
            for index, day in enumerate(state["draft_data"].days)
            if index not in saved
            for candidate in day.candidates
            if key(candidate) not in cache
        }
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
            meal_ids = {
                meal: {
                    venue.place.placeId
                    for candidate, venue in available
                    if meal_category_supported(venue.place.category, meal)
                    and _opening_start(
                        venue.periods,
                        day_date,
                        MEAL_WINDOWS[meal][0],
                        candidate.stay_minutes,
                        MEAL_WINDOWS[meal][1],
                    )
                    is not None
                }
                for meal in ("lunch", "dinner")
            }
            enough_meals = any(
                lunch != dinner
                for lunch in meal_ids["lunch"]
                for dinner in meal_ids["dinner"]
            )
            anchor = next(
                (
                    venue
                    for candidate, venue in available
                    if candidate.meal == "none" and not _meal_capable(venue)
                ),
                None,
            )
            discover = getattr(self.provider, "discover_meals", None)
            if (
                not enough_meals
                and anchor
                and index not in discovered_days
                and inspect.iscoroutinefunction(discover)
            ):
                _progress(
                    f"{index + 1}일차의 확인된 명소 주변에서 실제 식당 대안을 보충하고 있습니다."
                )
                try:
                    meals = await discover(state["destination"], anchor)
                    discovered_days.add(index)
                except MapsProviderError as error:
                    if error.kind in {"unauthorized", "invalid"}:
                        raise
                    if not error.transient:
                        discovered_days.add(index)
                    meals = []
                meal_costs = [
                    candidate.cost
                    for candidate in day.candidates
                    if candidate.meal in {"lunch", "dinner"} and candidate.cost > 0
                ]
                planned_cost = (
                    round(sum(meal_costs) / len(meal_costs)) if meal_costs else 18000
                )
                additions = {}
                for venue in meals[:5]:
                    if (
                        not isinstance(venue, VerifiedPlace)
                        or venue.place.placeId in reserved_ids
                        or not _meal_capable(venue)
                    ):
                        continue
                    if (
                        not venue.place.address
                        or not individual_place_category_supported(venue.place.category)
                        or not state["destination"].contains(
                            venue.place.latitude, venue.place.longitude
                        )
                        or distance_meters(anchor, venue) > 1000
                    ):
                        continue
                    candidate = Candidate(
                        name=venue.place.placeName,
                        english_name=venue.place.placeEngName,
                        meal="lunch",
                        stay_minutes=45,
                        cost=planned_cost,
                    )
                    venue.place.placeId = (
                        ("places/" + _canonical_id(venue.place.placeId))
                        if _canonical_id(venue.place.placeId)
                        else ""
                    )
                    additions[(venue.place.placeId, candidate.meal)] = (
                        candidate,
                        venue,
                    )
                pools[index] = dict(list({**additions, **pools[index]}.items())[:30])
                available = list(pools[index].values())
            plans = _day_plans(
                available,
                day_date,
                max_stops,
                minimum_visits,
                recent_ids,
                state["rejected_plans"].get(index),
                recent_profiles=state.get("recent_profiles"),
            )
            discover_attractions = getattr(self.provider, "discover_attractions", None)
            if (
                state.get("refilling")
                and index not in attraction_discovered_days
                and inspect.iscoroutinefunction(discover_attractions)
            ):
                profile = state["request"].tasteProfile
                preferred_categories = set().union(
                    *(
                        rule.categories
                        for rule in RULES
                        if profile
                        and getattr(getattr(profile, rule.section), rule.field) >= 4
                    )
                )
                anchors = [
                    item
                    for item in available
                    if _core_visit(item)
                    and tourism_category_supported(item[1].place.category)
                ]
                anchors.sort(
                    key=lambda item: item[1].place.category not in preferred_categories
                )
                if anchors:
                    anchor_candidate, anchor = anchors[0]
                    try:
                        attractions = await discover_attractions(
                            state["destination"], anchor
                        )
                        attraction_discovered_days.add(index)
                    except MapsProviderError as error:
                        if error.kind in {"unauthorized", "invalid"}:
                            raise
                        if not error.transient:
                            attraction_discovered_days.add(index)
                        attractions = []
                    existing_ids = {
                        _canonical_id(venue.place.placeId) for _, venue in available
                    } | {_canonical_id(value) for value in reserved_ids}
                    additions = {}
                    for venue in attractions[:5]:
                        if not isinstance(venue, VerifiedPlace):
                            continue
                        place = venue.place
                        identifier = _canonical_id(place.placeId)
                        if (
                            not identifier
                            or identifier in existing_ids
                            or not tourism_category_supported(place.category)
                            or not place.address
                            or not all(
                                math.isfinite(value)
                                for value in (place.latitude, place.longitude)
                            )
                            or not state["destination"].contains(
                                place.latitude, place.longitude
                            )
                            or distance_meters(anchor, venue) > 2000
                        ):
                            continue
                        if (
                            preferred_categories
                            & {item[1].place.category for item in anchors}
                            and place.category not in preferred_categories
                        ):
                            continue
                        place.placeId = "places/" + identifier
                        pace = profile.travelPaceDensity if profile else "balanced"
                        duration = (
                            anchor_candidate.stay_minutes
                            if place.category == anchor.place.category
                            or pace in {"slow_stay", "long_stay"}
                            else {
                                "museum": 60,
                                "art_gallery": 60,
                                "park": 45,
                                "garden": 45,
                            }.get(place.category, anchor_candidate.stay_minutes)
                        )
                        candidate = Candidate(
                            name=place.placeName,
                            english_name=place.placeEngName,
                            category=place.category,
                            stay_minutes=duration,
                            cost=anchor_candidate.cost,
                            planned_area=anchor_candidate.planned_area,
                            experiences=anchor_candidate.experiences,
                        )
                        cache[key(candidate)] = venue
                        additions[(place.placeId, "none")] = (candidate, venue)
                        existing_ids.add(identifier)
                    pools[index] = dict(
                        list({**additions, **pools[index]}.items())[:30]
                    )
                    available = list(pools[index].values())
                    plans = _day_plans(
                        available,
                        day_date,
                        max_stops,
                        minimum_visits,
                        recent_ids,
                        state["rejected_plans"].get(index),
                        recent_profiles=state.get("recent_profiles"),
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
                    )
                    if plans:
                        break
            all_plans.append(plans)
            if not plans:
                failures.append(
                    f"{index + 1}일차 {day_date}: 점심·저녁과 명소 {minimum_visits}곳의 영업/식사 시간 조합 부족. 확인된 후보: {', '.join(candidate.name for candidate, _ in available)}. 일시 조회 실패(재조회 가능): {', '.join(name for name in rejected if name in temporary_names)}. 이름/위치 검증 미충족: {', '.join(name for name in rejected if name not in temporary_names)}. 다른 공식 장소명과 식사 대안을 제안하세요."
                )
        return {
            "day_plans": all_plans,
            "failures": failures,
            "feedback": "",
            "place_cache": cache,
            "candidate_pools": pools,
            "discovered_days": discovered_days,
            "attraction_discovered_days": attraction_discovered_days,
        }

    async def verify_routes(self, state: CourseState) -> dict:
        """Check real travel and visit windows; retain feasible complete snapshots."""
        _progress(
            "실제 이동 시간으로 일정을 확인하고, 맞지 않으면 검증된 대안을 비교하고 있습니다."
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
        # Share one failed lookup within this validation pass only. A fresh draft
        # can retry a transient edge; failures never enter the successful cache.
        pass_failures: dict[tuple, MapsProviderError] = {}

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
            key = route_key(left, right, departure, mode)
            if key in cache:
                return cache[key]
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
                raise
            if valid_route(route):
                cache[key] = route
            return route

        async def lookup(
            left: VerifiedPlace, right: VerifiedPlace, departure: datetime | None
        ) -> TransportToNextSchema:
            key = route_key(left, right, departure)
            mode = key[2]
            if key in cache:
                return cache[key]
            try:
                return await actual_route(left, right, departure, mode)
            except MapsProviderError as error:
                if error.kind not in {"no_route", "route_data", "transient"}:
                    raise
                alternative = None
                if mode == "transit" and distance_meters(left, right) <= 3000:
                    alternative = "walking"
                elif mode == "walking" and error.kind in {"no_route", "route_data"}:
                    alternative = "transit"
                if alternative:
                    try:
                        # A valid but overlong route must remain a failed plan;
                        # its successful response cannot become a short estimate.
                        return await actual_route(left, right, departure, alternative)
                    except MapsProviderError as alternative_error:
                        if alternative_error.kind not in {
                            "no_route",
                            "route_data",
                            "transient",
                        }:
                            raise
                raise

        async def verify_one(item: tuple[int, list[Visit]]) -> tuple:
            index, selected = item
            day_date = date.fromisoformat(
                state["request"].tripCondition.startDate
            ) + timedelta(days=index)
            routes = []
            current = 540
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
                            f"{candidate.name}: 영업/식사 시간 안에 방문할 수 없습니다."
                        )
                    departure = None
                    if state["destination"].country_code == "KR":
                        departure = datetime.combine(
                            day_date, time(), tzinfo=ZoneInfo("Asia/Seoul")
                        ) + timedelta(minutes=arrival + candidate.stay_minutes)
                    route = await lookup(venue, right[1], departure)
                    if not valid_route(route):
                        raise CourseGenerationError(
                            f"{candidate.name} → {right[0].name}: 경로 정보가 불완전하거나 이동 시간이 90분을 초과합니다."
                        )
                    routes.append(route)
                    current = arrival + candidate.stay_minutes + route.minutes + 10
                day = _schedule_day(
                    selected,
                    routes,
                    day_date,
                    index + 1,
                    compact=any(_visit_shortage(selected)),
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
        while remaining:
            assignments = _choose_disjoint_plans(remaining, used)
            if not assignments:
                break
            results = await _parallel(list(assignments.items()), verify_one)
            for (index, selected), outcome in zip(assignments.items(), results):
                if isinstance(outcome, BaseException):
                    raise outcome
                record, error, temporary = outcome
                if record is not None:
                    validated[index] = record
                    used.update(
                        _canonical_id(venue.place.placeId) for _, venue in selected
                    )
                    remaining.pop(index, None)
                else:
                    last_errors[index] = error
                    remaining[index].remove(selected)
                    if not temporary:
                        rejected_plans.setdefault(index, set()).add(
                            _plan_signature(selected)
                        )
                    if not remaining[index]:
                        remaining.pop(index)
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
                return {"route_cache": cache, "restore_fallback": True, "feedback": ""}
            preserved = "; ".join(
                f"{index + 1}일차 유지: {', '.join(candidate.name for candidate, _ in value['selected'])}"
                for index, value in sorted(validated.items())
            )
            return {
                "validated_days": validated,
                "route_cache": cache,
                "rejected_plans": rejected_plans,
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
        _progress("검증을 마친 일자별 일정과 방문 팁을 최종 확인하고 있습니다.")
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
                not valid_route(route) for route in routes
            ):
                raise CourseGenerationError(
                    "실제로 확인되지 않은 이동 경로가 있습니다."
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
            # Real routes, opening/meal windows and full stays were checked by
            # verify_one. Copy that accepted day instead of formatting it twice.
            days.append(day.model_copy(deep=True))
        return {"days": days, "feedback": ""}

    async def finalize(self, state: CourseState) -> dict:
        """Compose grounded course copy and atomically record recent history."""
        _progress(
            "확정한 일정의 추천 이유를 작성하고 최근 코스의 중복 여부를 확인하고 있습니다."
        )
        request = state["request"]
        trip = request.tripCondition
        course = CourseSchema(
            title=f"{trip.destinationCity} {trip.totalDays}일 여행",
            destinationCountry=trip.destinationCountry,
            destinationCity=trip.destinationCity,
            startDate=trip.startDate,
            totalDays=trip.totalDays,
            tags=verified_course_tags(state["days"]),
            recommendationReason=apply_personalized_reasons(request, state["days"]),
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
                record = self.history.record_if_novel
                parameters = inspect.signature(record).parameters
                supports_metadata = "metadata" in parameters or any(
                    value.kind == inspect.Parameter.VAR_KEYWORD
                    for value in parameters.values()
                )
                kwargs = (
                    {
                        "metadata": _selection_metadata(state["selected"]),
                        "max_overlap": None if state.get("repeated") else MAX_OVERLAP,
                    }
                    if supports_metadata
                    else {}
                )
                novel = await record(history_key(request), ids, **kwargs)
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
                if not novel and supports_metadata and not state.get("repeated"):
                    # Explicit soft acceptance after bounded concurrency retries.
                    novel = await record(
                        history_key(request),
                        ids,
                        metadata=_selection_metadata(state["selected"]),
                    )
                    state = {**state, "repeated": True}
        except OSError, sqlite3.Error, TimeoutError:
            logger.warning("Course history write unavailable")
            unavailable = True
        if unavailable:
            course.recommendationReason += (
                " 생성 이력 확인·저장이 일시적으로 제한되어 최근 코스와 겹칠 수 있어요."
            )
        elif not novel or state.get("repeated"):
            course.recommendationReason += " 확인 가능한 장소와 일정 조건이 제한되어 이전 코스와 일부 방문 장소 또는 계획 권역이 겹칠 수 있어요."
        return {"course": course, "feedback": ""}

    async def enrich_images(self, state: CourseState) -> dict:
        """Use only residual optional time for final-place photos and attribution."""
        _progress("확정한 장소의 실제 사진과 대표 이미지를 준비하고 있습니다.")
        budget = 5.0
        if "deadline" in state:
            budget = min(
                budget, state["deadline"] - asyncio.get_running_loop().time() - 1
            )
        try:
            course = await enrich_course_images(
                state["course"], self.provider, timeout_seconds=max(0, budget)
            )
        except Exception:  # noqa: BLE001 - optional images cannot invalidate a verified course
            logger.warning("Optional course image enrichment unavailable")
            course = state["course"]
        return {"course": course}

    # Recovery with verified candidates and snapshots.

    async def refill(self, state: CourseState) -> dict:
        """Supplement only unfinished dates with an independent bounded budget.

        Successful dates and verified candidate/route caches remain unchanged.
        Temporary model failures still reach Maps supplementation and local
        recombination, rather than restoring a short snapshot immediately.
        """
        _progress("장소가 부족한 일차에 가까운 명소를 보충하고 있습니다.")
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
        updates.update({"draft_data": result, "partial_drafts": {}})
        return updates

    async def repair(self, state: CourseState) -> dict:
        """Use retained real venues for one final bounded, less dense schedule."""
        _progress(
            "확인한 장소로 이동과 식사에 여유가 있는 대안 일정을 구성하고 있습니다."
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
        }

    async def restore(self, state: CourseState) -> dict:
        """Restore a complete feasible snapshot, including its own draft metadata."""
        _progress("확인된 장소와 이동 경로를 유지하여 일정을 마무리하고 있습니다.")
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
        key = history_key(request)
        read_profiles = getattr(self.history, "recent_profiles", None)
        if inspect.iscoroutinefunction(read_profiles):
            profiles = await read_profiles(key)
            if isinstance(profiles, list):
                return [set(item["place_ids"]) for item in profiles], profiles
        recent = await self.history.recent(key)
        return recent, [{"place_ids": sorted(ids)} for ids in recent]

    def _history_feedback(self, state: CourseState) -> str:
        profiles = state.get("recent_profiles", [])
        areas = sorted({value for item in profiles for value in item.get("areas", [])})
        experiences = sorted(
            {value for item in profiles for value in item.get("experiences", [])}
        )
        return (
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

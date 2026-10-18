"""Explore venue combinations and construct schedules from verified facts.

Geographic distance ranks candidates; unknown transfers leave timing provisional."""

from collections.abc import Callable
from datetime import date

from app.agent.course_state import (
    CourseGenerationError,
    ScheduledDaySchema,
    ScheduledStopSchema,
    Visit,
)
from app.agent.tools.verified_maps import (
    VerifiedPlace,
    meal_category_supported,
    tourism_category_supported,
)
from app.schemas.course import (
    CourseRequestSchema,
    TransportToNextSchema,
)
from app.services.course_budget import budget_rank
from app.services.course_diversity import (
    MAX_OVERLAP,
    normalize_area,
    overlap_scores,
    planning_overlap,
)
from app.services.course_reasons import (
    CAFE,
    visit_tip,
)
from app.services.course_routing import (
    distance_meters,
    minimum_travel_minutes,
)


def _opening_start(
    periods: list[dict] | None, day: date, earliest: int, duration: int, latest: int
) -> int | None:
    """Fit a visit to regular weekly hours, supporting overnight and 24h periods.

    Unknown hours do not establish opening-time verification. Malformed supplied
    periods fail closed; meal windows and complete stay duration still apply.
    """
    if periods is None:
        return earliest if earliest + duration <= latest else None
    weekday = (day.weekday() + 1) % 7  # Google: Sunday=0.
    intervals: list[tuple[int, int]] = []
    for period in periods:
        try:
            opening = period["open"]
            opening_day = int(opening["day"])
            hour, minute = int(opening.get("hour", 0)), int(opening.get("minute", 0))
            if not 0 <= opening_day <= 6 or not 0 <= hour < 24 or not 0 <= minute < 60:
                return None
            if "close" not in period:
                if opening_day == 0 and hour == minute == 0:
                    return earliest if earliest + duration <= latest else None
                return None
            closing = period["close"]
            closing_day = int(closing["day"])
            close_hour, close_minute = (
                int(closing.get("hour", 0)),
                int(closing.get("minute", 0)),
            )
            if (
                not 0 <= closing_day <= 6
                or not 0 <= close_hour < 24
                or not 0 <= close_minute < 60
            ):
                return None
            start = opening_day * 1440 + hour * 60 + minute
            end = closing_day * 1440 + close_hour * 60 + close_minute
            if end <= start:
                end += 7 * 1440
            for offset in (-7 * 1440, 0, 7 * 1440):
                intervals.append(
                    (start + offset - weekday * 1440, end + offset - weekday * 1440)
                )
        except KeyError, TypeError, ValueError:
            return None
    for start, end in sorted(intervals):
        arrival = max(earliest, start)
        if arrival + duration <= min(end, latest):
            return arrival
    return None


MAX_DAY_PLANS = 4


MEAL_WINDOWS = {
    "none": (540, 1260),
    "breakfast": (540, 660),
    "lunch": (690, 840),
    "dinner": (1050, 1200),
}


def _schedule_day(
    selected: list[Visit],
    routes: list[TransportToNextSchema],
    day_date: date,
    day_number: int,
) -> ScheduledDaySchema:
    """Build visit order; clocks after unknown transfers are provisional bounds.

    Full visits must fit known windows. Unknown travel is not measured as zero:
    only its lower bound is used internally, and no clock is publicly returned.
    """
    if len(routes) != len(selected) - 1:
        raise CourseGenerationError(f"{day_number}일차: 이동 경로가 누락되었습니다.")
    current, stops = 540, []
    for index, (candidate, venue) in enumerate(selected):
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
                f"{day_number}일차 {day_date} {candidate.meal} {candidate.name}: 방문/식사 시간 안에 {candidate.stay_minutes}분 체류할 수 없습니다."
            )
        last = index == len(selected) - 1
        transport = (
            TransportToNextSchema(
                type="none",
                distance=0,
                minutes=0,
                memo=None,
            )
            if last
            else routes[index]
        )
        note = visit_tip(venue.place.category)
        stops.append(
            ScheduledStopSchema(
                sequence=index + 1,
                arrivalTime=f"{arrival // 60:02d}:{arrival % 60:02d}",
                stayMinutes=candidate.stay_minutes,
                memo=note,
                reason=candidate.reason,
                cost=candidate.cost,
                place=venue.place,
                transportToNext=transport,
            )
        )
        current = (
            arrival
            + candidate.stay_minutes
            + minimum_travel_minutes(transport)
            + (0 if last else 10)
        )
    return ScheduledDaySchema(day=day_number, date=day_date.isoformat(), stops=stops)


def _meal_capable(venue: VerifiedPlace) -> bool:
    return any(
        meal_category_supported(venue.place.category, meal)
        for meal in ("lunch", "dinner")
    )


def _core_visit(item: Visit) -> bool:
    """Count verified sightseeing venues; food and cafe stops never replace them."""
    candidate, venue = item
    return (
        candidate.meal == "none"
        and tourism_category_supported(venue.place.category)
        and not _meal_capable(venue)
        and venue.place.category not in CAFE
    )


def _visit_shortage(selected: list[Visit]) -> tuple[int, int]:
    """Return common normal floor deficits, independent of optional pace targets."""
    return max(0, 5 - len(selected)), max(
        0, 3 - sum(_core_visit(item) for item in selected)
    )


def _proposal_floor(
    available: list[Visit], day_date: date
) -> list[Visit]:
    """Retain one original-order five-visit alternative before new searches.

    Args:
        available: Retained proposals with already verified venue facts.
        day_date: Date used for known opening periods and common meal windows.
    Returns:
        First three verified core proposals plus distinct proposed lunch/dinner,
        in the model order, or an empty list when identity/order/windows fail.
        Transfer time is not invented: this optimistic check uses zero travel
        and the existing ten-minute buffer only to reject impossible proposals.
        The caller must verify all four actual routes before accepting the day.
    """
    core_indices = [index for index, item in enumerate(available) if _core_visit(item)][:3]
    if len(core_indices) != 3:
        return []
    selected_indices = list(core_indices)
    for meal in ('lunch', 'dinner'):
        index = next((index for index, (candidate, venue) in enumerate(available)
                      if candidate.meal == meal and meal_category_supported(venue.place.category, meal)), None)
        if index is None:
            return []
        selected_indices.append(index)
    selected = [available[index] for index in sorted(selected_indices)]
    ids = [_canonical_id(venue.place.placeId) for _, venue in selected]
    if not all(ids) or len(set(ids)) != 5:
        return []
    if [candidate.meal for candidate, _ in selected if candidate.meal != 'none'] != ['lunch', 'dinner']:
        return []
    current = 540
    for candidate, venue in selected:
        opening, latest = MEAL_WINDOWS[candidate.meal]
        arrival = _opening_start(venue.periods, day_date, max(current, opening), candidate.stay_minutes, latest)
        if arrival is None:
            return []
        current = arrival + candidate.stay_minutes + 10
    return selected


def _shorter_visits(
    available: list[Visit], request: CourseRequestSchema
) -> list[Visit]:
    """Adjust only short local visit types, preserving deliberate slow stays."""
    pace = (
        request.tasteProfile.travelPaceDensity if request.tasteProfile else "balanced"
    )
    if pace in {"slow_stay", "long_stay"}:
        return available
    limits = {
        "museum": 60,
        "art_gallery": 60,
        "park": 45,
        "garden": 45,
        "botanical_garden": 60,
        "historical_landmark": 45,
        "monument": 45,
        "book_store": 45,
        "gift_shop": 45,
        "souvenir_store": 45,
    }
    return [
        (
            candidate.model_copy(
                update={
                    "stay_minutes": min(
                        candidate.stay_minutes,
                        45
                        if candidate.meal in {"lunch", "dinner"}
                        else limits.get(venue.place.category, candidate.stay_minutes),
                    )
                }
            ),
            venue,
        )
        for candidate, venue in available
    ]


def _pace_limit(request: CourseRequestSchema) -> int:
    pace = (
        request.tasteProfile.travelPaceDensity if request.tasteProfile else "balanced"
    )
    return {"slow_stay": 5, "long_stay": 5, "dense_schedule": 7}.get(pace, 6)


def _plan_signature(selected: list[Visit]) -> tuple:
    """Identify the exact ordered visits whose real travel feasibility was checked."""
    return tuple(
        (venue.place.placeId, candidate.meal, candidate.stay_minutes)
        for candidate, venue in selected
    )


def _selection_metadata(selected: list[list[Visit]]) -> dict:
    """Remember only selected attraction IDs and their original model choices."""
    attractions = [
        (candidate, venue)
        for day in selected
        for candidate, venue in day
        if _core_visit((candidate, venue))
    ]
    return {
        "attraction_ids": sorted({venue.place.placeId for _, venue in attractions}),
        "areas": sorted(
            {
                normalize_area(candidate.planned_area)
                for candidate, _ in attractions
                if candidate.planned_area.strip()
            }
        ),
        "experiences": sorted(
            {value for candidate, _ in attractions for value in candidate.experiences}
        ),
    }


def _diversity_rank(path: list[Visit], profiles: list[dict]) -> tuple:
    metadata = _selection_metadata([path])
    scores = overlap_scores(
        [venue.place.placeId for _, venue in path], metadata["attraction_ids"], profiles
    )
    # Rank diversity after the common visit floor; extra stops are optional.
    areas = {
        normalize_area(candidate.planned_area)
        for candidate, _ in path
        if candidate.planned_area.strip()
    }
    area_overlap = planning_overlap(metadata["areas"], profiles, "areas")
    misses_target = (
        max(scores["attraction_overlap"], scores["place_overlap"], area_overlap)
        > MAX_OVERLAP
    )
    return (
        misses_target,
        scores["attraction_overlap"],
        scores["place_overlap"],
        area_overlap,
        max(0, len(areas) - 1),
        planning_overlap(metadata["experiences"], profiles, "experiences"),
    )


def _day_plans(
    available: list[Visit],
    day_date: date,
    max_stops: int,
    minimum_visits: int,
    recent_ids: set[str],
    rejected_plans: set[tuple] | None = None,
    *,
    plan_limit: int = MAX_DAY_PLANS,
    compact: bool = False,
    recent_profiles: list[dict] | None = None,
    daily_budget: float | None = None,
) -> list[list[Visit]]:
    """Find up to four optimistic plans; only real routes can confirm feasibility.

    The beam is capped at 48 states per depth, with at most 30 retained proposals.
    Zero travel is used only to discard impossible orderings, never as a route.
    Budget ranks feasible alternatives after the common visit floor; it is never
    a hard rejection and unknown transport fares are not estimated here.
    """
    options = [
        item
        for item in available
        if meal_category_supported(item[1].place.category, item[0].meal)
    ]
    # A verified restaurant may serve another meal if that full visit fits its hours.
    for candidate, venue in available:
        if not _meal_capable(venue):
            continue
        for meal in ("lunch", "dinner"):
            if candidate.meal != meal and meal_category_supported(
                venue.place.category, meal
            ):
                options.append((candidate.model_copy(update={"meal": meal}), venue))
    options = [
        item
        for item in options
        if _opening_start(
            item[1].periods,
            day_date,
            MEAL_WINDOWS[item[0].meal][0],
            item[0].stay_minutes,
            MEAL_WINDOWS[item[0].meal][1],
        )
        is not None
    ]
    unique = {
        (venue.place.placeId, candidate.meal): (candidate, venue)
        for candidate, venue in reversed(options)
    }
    options = list(reversed(unique.values()))
    completed: list[tuple[list[Visit], int, float]] = []
    beam: list[tuple[list[Visit], int, float]] = [([], 540, 0.0)]
    for depth in range(max_stops):
        following = []
        for path, current, distance in beam:
            ids = {venue.place.placeId for _, venue in path}
            meals = {
                candidate.meal for candidate, _ in path if candidate.meal != "none"
            }
            for item in options:
                candidate, venue = item
                if venue.place.placeId in ids or (
                    candidate.meal != "none" and candidate.meal in meals
                ):
                    continue
                if candidate.meal == "breakfast" and (
                    "lunch" in meals or "dinner" in meals
                ):
                    continue
                if candidate.meal == "dinner" and "lunch" not in meals:
                    continue
                earliest, latest = MEAL_WINDOWS[candidate.meal]
                arrival = _opening_start(
                    venue.periods,
                    day_date,
                    max(current, earliest),
                    candidate.stay_minutes,
                    latest,
                )
                if arrival is None:
                    continue
                new_path = [*path, item]
                new_meals = meals | {candidate.meal}
                visits = sum(_core_visit(entry) for entry in new_path)
                required = len({"lunch", "dinner"} - new_meals) + max(
                    0, minimum_visits - visits
                )
                if max_stops - depth - 1 < required:
                    continue
                new_distance = distance + (
                    distance_meters(path[-1][1], venue) if path else 0
                )
                # Ten minutes of transfer buffer is known even before route lookup.
                new_state = (
                    new_path,
                    arrival + candidate.stay_minutes + 10,
                    new_distance,
                )
                following.append(new_state)
                if not required:
                    completed.append(new_state)
        following.sort(
            key=lambda state: (
                budget_rank(state[0], daily_budget),
                (_diversity_rank(state[0], recent_profiles) if recent_profiles else ()),
                state[1],
                sum(venue.place.placeId in recent_ids for _, venue in state[0]),
                state[2],
            )
        )
        beam = following[:48]
        if not beam:
            break
    completed.sort(
        key=lambda state: (
            _visit_shortage(state[0]),
            budget_rank(state[0], daily_budget),
            (_diversity_rank(state[0], recent_profiles) if recent_profiles else ()),
            len(state[0]) if compact else -len(state[0]),
            not any(candidate.meal == "breakfast" for candidate, _ in state[0]),
            sum(venue.place.placeId in recent_ids for _, venue in state[0]),
            state[2],
            state[1],
        )
    )
    # Keep an already feasible original proposal first; this avoids gratuitous reorder.
    original = []
    used: set[str] = set()
    ordered = sorted(available, key=lambda item: item[1].place.placeId in recent_ids)
    for meal in ("breakfast", "lunch", "dinner"):
        matches = [
            item
            for item in ordered
            if item[0].meal == meal and item[1].place.placeId not in used
        ]
        if matches:
            original.append(matches[0])
            used.add(matches[0][1].place.placeId)
    for item in ordered:
        if (
            len(original) < max_stops
            and item[0].meal == "none"
            and item[1].place.placeId not in used
        ):
            original.append(item)
            used.add(item[1].place.placeId)
    order = {id(candidate): index for index, (candidate, _) in enumerate(available)}
    original.sort(key=lambda item: order[id(item[0])])
    valid_meals = [
        candidate.meal for candidate, _ in original if candidate.meal != "none"
    ]
    original_key = tuple(
        (venue.place.placeId, candidate.meal) for candidate, venue in original
    )
    preferred = next(
        (
            state
            for state in completed
            if tuple(
                (venue.place.placeId, candidate.meal) for candidate, venue in state[0]
            )
            == original_key
        ),
        None,
    )
    if (
        not recent_profiles
        and not compact
        and preferred
        and len(preferred[0]) == len(completed[0][0])
        and _visit_shortage(preferred[0]) == _visit_shortage(completed[0][0])
        and budget_rank(preferred[0], daily_budget)
        == budget_rank(completed[0][0], daily_budget)
        and valid_meals in (["lunch", "dinner"], ["breakfast", "lunch", "dinner"])
    ):
        completed.remove(preferred)
        completed.insert(0, preferred)
    plans: list[list[Visit]] = []
    signatures: set[tuple] = set()
    meal_sets: set[tuple] = set()
    for diverse in (False,) if recent_profiles else (True, False):
        for path, _, _ in completed:
            signature = _plan_signature(path)
            meal_set = tuple(
                (candidate.meal, venue.place.placeId)
                for candidate, venue in path
                if candidate.meal != "none"
            )
            if (
                signature in signatures
                or signature in (rejected_plans or set())
                or (diverse and meal_set in meal_sets)
            ):
                continue
            plans.append(path)
            signatures.add(signature)
            meal_sets.add(meal_set)
            if len(plans) == plan_limit:
                return plans
    return plans


def _canonical_id(value: str) -> str:
    return value.strip().removeprefix("places/")


def _choose_disjoint_plans(
    day_plans: dict[int, list[list[Visit]]],
    reserved_ids: set[str],
    search_budget: int = 5000,
    *,
    plan_cost: Callable[[int, list[Visit]], int] | None = None,
) -> dict[int, list[Visit]]:
    """Cover as many disjoint days as possible, then minimize optional cost.

    Args:
        day_plans: Already ranked visit alternatives per day.
        reserved_ids: Verified venues already committed to other days.
        search_budget: Existing bound on explored assignments.
        plan_cost: Optional nonnegative count, such as unknown transfers. With
            no cost the original first-complete search and ordering are kept.
    Returns:
        Best assignment found within the bound, retaining all possible days
        before preferring a lower total cost. No external requests are made.
    """
    reserved = {_canonical_id(value) for value in reserved_ids}
    options = {
        index: [
            (plan, {_canonical_id(venue.place.placeId) for _, venue in plan},
             plan_cost(index, plan) if plan_cost else 0)
            for plan in plans
        ]
        for index, plans in day_plans.items()
    }
    order = sorted(options, key=lambda index: (len(options[index]), index))
    best: dict[int, list[Visit]] = {}
    best_cost = float('inf')
    visited = 0

    def search(offset: int, used: set[str], chosen: dict[int, list[Visit]], cost: int) -> bool:
        nonlocal best, best_cost, visited
        if visited >= search_budget:
            return False
        visited += 1
        if len(chosen) > len(best) or len(chosen) == len(best) and cost < best_cost:
            best = dict(chosen)
            best_cost = cost
        if offset == len(order):
            return len(chosen) == len(order) and (plan_cost is None or cost == 0)
        possible_days = len(chosen) + len(order) - offset
        if possible_days < len(best) or possible_days == len(best) and cost >= best_cost:
            return False
        index = order[offset]
        for plan, ids, weight in options[index]:
            if len(ids) != len(plan) or used & ids:
                continue
            chosen[index] = plan
            if search(offset + 1, used | ids, chosen, cost + weight):
                return True
            chosen.pop(index)
        return search(offset + 1, used, chosen, cost)

    search(0, reserved, {}, 0)
    return dict(sorted(best.items()))

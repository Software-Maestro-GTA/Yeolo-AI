"""Explore venue combinations and construct schedules from verified facts.

Geographic distance ranks candidates; only actual provider routes confirm travel."""

from datetime import date

from app.agent.course_state import CourseGenerationError, Visit
from app.agent.tools.verified_maps import (
    VerifiedPlace,
    meal_category_supported,
)
from app.schemas.course import (
    CourseRequestSchema,
    DayItinerarySchema,
    StopSchema,
    TransportToNextSchema,
)
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
)
from app.services.day_summary import MAX_DAY_SUMMARY_LENGTH, compose_day_summary


def _opening_start(
    periods: list[dict] | None, day: date, earliest: int, duration: int, latest: int
) -> int | None:
    """Fit a visit to regular weekly hours, supporting overnight and 24h periods.

    Unknown hours are allowed but disclosed. Malformed supplied periods fail closed.
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
    *,
    compact: bool = False,
) -> DayItinerarySchema:
    """Build one day using actual routes; reject any full-visit window violation."""
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
                f"{day_number}일차 {day_date} {candidate.meal} {candidate.name}: 영업/식사 시간 안에 {candidate.stay_minutes}분 체류할 수 없습니다."
            )
        last = index == len(selected) - 1
        transport = (
            TransportToNextSchema(
                type="none",
                distance=0,
                minutes=0,
                cost=0,
                memo="이 장소에서 오늘의 계획된 일정을 종료합니다. 숙소 이동은 포함되지 않습니다.",
            )
            if last
            else routes[index]
        )
        note = visit_tip(venue.place.category)
        if venue.periods is None:
            note += " 영업시간 미확인: 방문 전 확인이 필요합니다."
        stops.append(
            StopSchema(
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
            + (transport.minutes or 0)
            + (0 if last else 10)
        )
    try:
        summary = compose_day_summary(stops, day_number, compact=compact)
        if (
            not isinstance(summary, str)
            or not summary.strip()
            or len(summary) > MAX_DAY_SUMMARY_LENGTH
        ):
            raise ValueError("Invalid optional day summary")
    except Exception:  # noqa: BLE001 - optional prose cannot invalidate a verified schedule; cancellation propagates
        summary = "새로운 장소를 만나며 나만의 여행 이야기를 만들어보세요."
        if compact:
            summary += f" 오늘은 이동과 식사 시간을 고려해 {len(stops)}곳을 둘러보세요."
    return DayItinerarySchema(
        day=day_number, date=day_date.isoformat(), memo=summary, stops=stops
    )


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
        and not _meal_capable(venue)
        and venue.place.category not in CAFE
    )


def _visit_shortage(selected: list[Visit]) -> tuple[int, int]:
    """Return common normal floor deficits, independent of optional pace targets."""
    return max(0, 5 - len(selected)), max(
        0, 3 - sum(_core_visit(item) for item in selected)
    )


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
) -> list[list[Visit]]:
    """Find up to four optimistic plans; only real routes can confirm feasibility.

    The beam is capped at 48 states per depth, with at most 30 retained proposals.
    Zero travel is used only to discard impossible orderings, never as a route.
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
) -> dict[int, list[Visit]]:
    """Find a bounded global assignment, protecting days with few alternatives."""
    reserved = {_canonical_id(value) for value in reserved_ids}
    options = {
        index: [
            (plan, {_canonical_id(venue.place.placeId) for _, venue in plan})
            for plan in plans
        ]
        for index, plans in day_plans.items()
    }
    order = sorted(options, key=lambda index: (len(options[index]), index))
    best: dict[int, list[Visit]] = {}
    visited = 0

    def search(offset: int, used: set[str], chosen: dict[int, list[Visit]]) -> bool:
        nonlocal best, visited
        visited += 1
        if len(chosen) > len(best):
            best = dict(chosen)
        if offset == len(order):
            return len(chosen) == len(order)
        if visited >= search_budget:
            return False
        index = order[offset]
        for plan, ids in options[index]:
            if len(ids) != len(plan) or used & ids:
                continue
            chosen[index] = plan
            if search(offset + 1, used | ids, chosen):
                return True
            chosen.pop(index)
        return search(offset + 1, used, chosen)

    search(0, reserved, {})
    return dict(sorted(best.items()))

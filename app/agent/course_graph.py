"""Explicit, bounded LangGraph pipeline for personal, verified travel courses."""

import asyncio
import logging
import math
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from datetime import date, timedelta
from itertools import pairwise
from typing import Any, Literal, TypedDict

from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from app.agent.prompts import COURSE_CANDIDATE_PROMPT
from app.agent.tools.verified_maps import (
    Destination,
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
    StopSchema,
    TransportToNextSchema,
)
from app.services.course_history import CourseHistory, history_key
from app.services.course_reasons import apply_personalized_reasons
from app.services.maps_cost import MapsCostMetrics

logger = logging.getLogger(__name__)


class CourseGenerationError(ValueError):
    """No sufficiently verified, feasible, novel course could be produced."""


class Candidate(BaseModel):
    """Internal proposal; contains no invented map facts or arrival times."""

    name: str = Field(min_length=1, max_length=160)
    english_name: str = Field(default='', max_length=160)
    category: str = 'attraction'
    stay_minutes: int = Field(default=60, ge=15, le=240)
    cost: int = Field(default=0, ge=0)
    reason: str = ''
    meal: Literal['none', 'breakfast', 'lunch', 'dinner'] = 'none'


class DraftDay(BaseModel):
    candidates: list[Candidate] = Field(min_length=2, max_length=10)


class CourseDraft(BaseModel):
    title: str
    reason: str
    tags: list[str] = Field(default_factory=list)
    days: list[DraftDay]


class CourseState(TypedDict, total=False):
    request: CourseRequestSchema
    attempt: int
    destination: Destination
    recent: list[set[str]]
    draft_data: CourseDraft
    selected: list[list[tuple[Candidate, VerifiedPlace]]]
    routes: list[list[TransportToNextSchema]]
    days: list[DayItinerarySchema]
    feedback: str
    course: CourseSchema
    day_plans: list[list[list[tuple[Candidate, VerifiedPlace]]]]
    validated_days: dict[int, dict]
    place_cache: dict[tuple, Any]
    route_cache: dict[tuple, Any]
    failures: list[str]
    candidate_pools: dict[int, dict[tuple[str, str], tuple[Candidate, VerifiedPlace]]]
    rejected_plans: dict[int, set[tuple]]


async def draft_candidates(request: CourseRequestSchema, recent_ids: list[str], feedback: str = '') -> CourseDraft:
    """Ask Gemini once for compact, preference-aware candidate names and reasons."""
    model = ChatGoogleGenerativeAI(model=settings.GEMINI_MODEL_NAME, google_api_key=settings.GEMINI_API_KEY, temperature=0.7, thinking_level=settings.GEMINI_THINKING_LEVEL, max_retries=0, timeout=50)
    chain = COURSE_CANDIDATE_PROMPT | model.with_structured_output(CourseDraft)
    return await chain.ainvoke({
        'mbti': request.mbti or '미제공',
        'taste_profile': request.tasteProfile.model_dump_json() if request.tasteProfile else '미제공',
        'trip_condition': request.tripCondition.model_dump_json(),
        'recent_ids': ','.join(recent_ids[:120]),
        'feedback': feedback,
        'seed': uuid.uuid4().hex[:12],
    })


def _progress(message: str) -> None:
    get_stream_writer()({'step': 'GENERATING_ROUTE', 'message': message})


async def _parallel(items: list[Any], operation: Callable[[Any], Awaitable[Any]]) -> list[Any]:
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


def _opening_start(periods: list[dict] | None, day: date, earliest: int, duration: int, latest: int) -> int | None:
    """Fit a visit to regular weekly hours, supporting overnight and 24h periods.

    Unknown hours are allowed but disclosed. Malformed supplied periods fail closed.
    """
    if periods is None:
        return earliest if earliest + duration <= latest else None
    weekday = (day.weekday() + 1) % 7  # Google: Sunday=0.
    intervals: list[tuple[int, int]] = []
    for period in periods:
        try:
            opening = period['open']
            opening_day = int(opening['day'])
            hour, minute = int(opening.get('hour', 0)), int(opening.get('minute', 0))
            if not 0 <= opening_day <= 6 or not 0 <= hour < 24 or not 0 <= minute < 60:
                return None
            if 'close' not in period:
                if opening_day == 0 and hour == minute == 0:
                    return earliest if earliest + duration <= latest else None
                return None
            closing = period['close']
            closing_day = int(closing['day'])
            close_hour, close_minute = int(closing.get('hour', 0)), int(closing.get('minute', 0))
            if not 0 <= closing_day <= 6 or not 0 <= close_hour < 24 or not 0 <= close_minute < 60:
                return None
            start = opening_day * 1440 + hour * 60 + minute
            end = closing_day * 1440 + close_hour * 60 + close_minute
            if end <= start:
                end += 7 * 1440
            for offset in (-7 * 1440, 0, 7 * 1440):
                intervals.append((start + offset - weekday * 1440, end + offset - weekday * 1440))
        except (KeyError, TypeError, ValueError):
            return None
    for start, end in sorted(intervals):
        arrival = max(earliest, start)
        if arrival + duration <= min(end, latest):
            return arrival
    return None


def _distance(left: VerifiedPlace, right: VerifiedPlace) -> float:
    lat1, lat2 = math.radians(left.place.latitude), math.radians(right.place.latitude)
    dlat, dlon = lat2 - lat1, math.radians(right.place.longitude - left.place.longitude)
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(value)))


# A small local search explores verified alternatives before asking the LLM again.
MAX_DAY_PLANS = 4
MEAL_WINDOWS = {'none': (540, 1260), 'breakfast': (540, 660), 'lunch': (690, 840), 'dinner': (1050, 1200)}
type Visit = tuple[Candidate, VerifiedPlace]


def _schedule_day(selected: list[Visit], routes: list[TransportToNextSchema], day_date: date, day_number: int) -> DayItinerarySchema:
    """Build one day using actual routes; reject any full-visit window violation."""
    if len(routes) != len(selected) - 1:
        raise CourseGenerationError(f'{day_number}일차: 이동 경로가 누락되었습니다.')
    current, stops = 540, []
    for index, (candidate, venue) in enumerate(selected):
        opening, latest = MEAL_WINDOWS[candidate.meal]
        arrival = _opening_start(venue.periods, day_date, max(current, opening), candidate.stay_minutes, latest)
        if arrival is None:
            raise CourseGenerationError(f'{day_number}일차 {day_date} {candidate.meal} {candidate.name}: 영업/식사 시간 안에 {candidate.stay_minutes}분 체류할 수 없습니다.')
        last = index == len(selected) - 1
        transport = TransportToNextSchema(type='none', distance=0, minutes=0, cost=0, memo='이 장소에서 오늘의 계획된 일정을 종료합니다. 숙소 이동은 포함되지 않습니다.') if last else routes[index]
        note = '영업시간 미확인: 방문 전 확인이 필요합니다.' if venue.periods is None else '정규 영업시간 기준이며 공휴일·임시휴무·예약 가능 여부는 방문 전 확인이 필요합니다.'
        stops.append(StopSchema(sequence=index + 1, arrivalTime=f'{arrival // 60:02d}:{arrival % 60:02d}', stayMinutes=candidate.stay_minutes, memo=f'{note} 비용은 AI 추정치이며 실제 가격은 다를 수 있습니다.', reason=candidate.reason, cost=candidate.cost, place=venue.place, transportToNext=transport))
        current = arrival + candidate.stay_minutes + (transport.minutes or 0) + (0 if last else 10)
    breakfast_note = '조식이 포함되어 있습니다.' if any(candidate.meal == 'breakfast' for candidate, _ in selected) else '조식은 별도입니다.'
    return DayItinerarySchema(day=day_number, date=day_date.isoformat(), memo=f'09:00~21:00 기준 일정. 식사와 이동 후 10분 여유를 반영했습니다. {breakfast_note} 숙소 왕복은 별도이며 교통은 조회 시점 예상치입니다.', stops=stops)


def _meal_capable(venue: VerifiedPlace) -> bool:
    return any(meal_category_supported(venue.place.category, meal) for meal in ('lunch', 'dinner'))


def _plan_signature(selected: list[Visit]) -> tuple:
    """Identify the exact ordered visits whose real travel feasibility was checked."""
    return tuple((venue.place.placeId, candidate.meal, candidate.stay_minutes) for candidate, venue in selected)


def _day_plans(available: list[Visit], day_date: date, max_stops: int, minimum_visits: int, recent_ids: set[str], rejected_plans: set[tuple] | None = None) -> list[list[Visit]]:
    """Find up to four optimistic plans; only real routes can confirm feasibility.

    The beam is capped at 48 states per depth, with at most 30 retained proposals.
    Zero travel is used only to discard impossible orderings, never as a route.
    """
    options = [item for item in available if meal_category_supported(item[1].place.category, item[0].meal)]
    # A verified restaurant may serve another meal if that full visit fits its hours.
    for candidate, venue in available:
        if not _meal_capable(venue):
            continue
        for meal in ('lunch', 'dinner'):
            if candidate.meal != meal and meal_category_supported(venue.place.category, meal):
                options.append((candidate.model_copy(update={'meal': meal}), venue))
    options = [item for item in options if _opening_start(item[1].periods, day_date, MEAL_WINDOWS[item[0].meal][0], item[0].stay_minutes, MEAL_WINDOWS[item[0].meal][1]) is not None]
    unique = {(venue.place.placeId, candidate.meal): (candidate, venue) for candidate, venue in reversed(options)}
    options = list(reversed(unique.values()))
    completed: list[tuple[list[Visit], int, float]] = []
    beam: list[tuple[list[Visit], int, float]] = [([], 540, 0.0)]
    for depth in range(max_stops):
        following = []
        for path, current, distance in beam:
            ids = {venue.place.placeId for _, venue in path}
            meals = {candidate.meal for candidate, _ in path if candidate.meal != 'none'}
            for item in options:
                candidate, venue = item
                if venue.place.placeId in ids or (candidate.meal != 'none' and candidate.meal in meals):
                    continue
                if candidate.meal == 'breakfast' and ('lunch' in meals or 'dinner' in meals):
                    continue
                if candidate.meal == 'dinner' and 'lunch' not in meals:
                    continue
                earliest, latest = MEAL_WINDOWS[candidate.meal]
                arrival = _opening_start(venue.periods, day_date, max(current, earliest), candidate.stay_minutes, latest)
                if arrival is None:
                    continue
                new_path = [*path, item]
                new_meals = meals | {candidate.meal}
                visits = sum(entry[0].meal == 'none' for entry in new_path)
                required = len({'lunch', 'dinner'} - new_meals) + max(0, minimum_visits - visits)
                if max_stops - depth - 1 < required:
                    continue
                new_distance = distance + (_distance(path[-1][1], venue) if path else 0)
                # Ten minutes of transfer buffer is known even before route lookup.
                new_state = (new_path, arrival + candidate.stay_minutes + 10, new_distance)
                following.append(new_state)
                if not required:
                    completed.append(new_state)
        following.sort(key=lambda state: (state[1], sum(venue.place.placeId in recent_ids for _, venue in state[0]), state[2]))
        beam = following[:48]
        if not beam:
            break
    completed.sort(key=lambda state: (-len(state[0]), not any(candidate.meal == 'breakfast' for candidate, _ in state[0]), sum(venue.place.placeId in recent_ids for _, venue in state[0]), state[2], state[1]))
    # Keep an already feasible original proposal first; this avoids gratuitous reorder.
    original = []
    used: set[str] = set()
    ordered = sorted(available, key=lambda item: item[1].place.placeId in recent_ids)
    for meal in ('breakfast', 'lunch', 'dinner'):
        matches = [item for item in ordered if item[0].meal == meal and item[1].place.placeId not in used]
        if matches:
            original.append(matches[0])
            used.add(matches[0][1].place.placeId)
    for item in ordered:
        if len(original) < max_stops and item[0].meal == 'none' and item[1].place.placeId not in used:
            original.append(item)
            used.add(item[1].place.placeId)
    order = {id(candidate): index for index, (candidate, _) in enumerate(available)}
    original.sort(key=lambda item: order[id(item[0])])
    valid_meals = [candidate.meal for candidate, _ in original if candidate.meal != 'none']
    original_key = tuple((venue.place.placeId, candidate.meal) for candidate, venue in original)
    preferred = next((state for state in completed if tuple((venue.place.placeId, candidate.meal) for candidate, venue in state[0]) == original_key), None)
    if preferred and valid_meals in (['lunch', 'dinner'], ['breakfast', 'lunch', 'dinner']):
        completed.remove(preferred)
        completed.insert(0, preferred)
    plans: list[list[Visit]] = []
    signatures: set[tuple] = set()
    meal_sets: set[tuple] = set()
    for diverse in (True, False):
        for path, _, _ in completed:
            signature = _plan_signature(path)
            meal_set = tuple((candidate.meal, venue.place.placeId) for candidate, venue in path if candidate.meal != 'none')
            if signature in signatures or signature in (rejected_plans or set()) or (diverse and meal_set in meal_sets):
                continue
            plans.append(path)
            signatures.add(signature)
            meal_sets.add(meal_set)
            if len(plans) == MAX_DAY_PLANS:
                return plans
    return plans


def build_course_graph(provider: VerifiedMapsProvider, history: CourseHistory) -> Any:
    """Compile explicit generation nodes using injectable provider/history boundaries.

    Returns a compiled LangGraph; validation failures receive one repair, or two for multi-day trips.
    """
    async def prepare(state: CourseState) -> dict:
        _progress('여행 조건과 최근 코스 이력을 확인하고 있습니다.')
        request = state['request']
        date.fromisoformat(request.tripCondition.startDate)
        destination = await provider.resolve_destination(request.tripCondition.destinationCountry, request.tripCondition.destinationCity)
        return {'destination': destination, 'recent': await history.recent(history_key(request)), 'attempt': 0, 'feedback': '', 'validated_days': {}, 'place_cache': {}, 'route_cache': {}, 'candidate_pools': {}, 'rejected_plans': {}}

    async def draft(state: CourseState) -> dict:
        _progress('취향에 맞는 장소 후보와 식사 대안을 구성하고 있습니다.')
        result = await draft_candidates(state['request'], sorted(set().union(*state['recent'])) if state['recent'] else [], state.get('feedback', ''))
        if len(result.days) != state['request'].tripCondition.totalDays:
            return {'attempt': state['attempt'] + 1, 'feedback': '요청한 여행 일수와 초안의 days 개수를 일치시키세요.'}
        result = result.model_copy(deep=True)
        for index, saved in state.get('validated_days', {}).items():
            result.days[index] = DraftDay(candidates=[candidate for candidate, _ in saved['selected']])
        return {'draft_data': result, 'attempt': state['attempt'] + 1, 'feedback': ''}

    async def verify_places(state: CourseState) -> dict:
        _progress('장소의 실제 위치·도시·영업 정보를 병렬로 확인하고 있습니다.')
        saved = state['validated_days']
        reserved_ids = {venue.place.placeId for value in saved.values() for _, venue in value['selected']}
        cache = state['place_cache']
        pools = {index: dict(pool) for index, pool in state['candidate_pools'].items()}
        def key(candidate: Candidate) -> tuple:
            return (candidate.name.strip().casefold(), candidate.english_name.strip().casefold(), candidate.meal)
        unique = {key(candidate): candidate for index, day in enumerate(state['draft_data'].days) if index not in saved for candidate in day.candidates if key(candidate) not in cache}
        results = await _parallel(list(unique.values()), lambda candidate: provider.search(candidate, state['destination']))
        cache.update(zip(unique, results))
        recent_ids = set().union(*state['recent']) if state['recent'] else set()
        pace = state['request'].tasteProfile.travelPaceDensity if state['request'].tasteProfile else 'balanced'
        max_stops = {'slow_stay': 5, 'long_stay': 5, 'dense_schedule': 7}.get(pace, 6)
        minimum_visits = {'slow_stay': 1, 'long_stay': 1, 'dense_schedule': 3}.get(pace, 2)
        all_plans, failures = [], []
        for index, day in enumerate(state['draft_data'].days):
            if index in saved:
                all_plans.append([saved[index]['selected']])
                continue
            day_date = date.fromisoformat(state['request'].tripCondition.startDate) + timedelta(days=index)
            pool = pools.setdefault(index, {})
            current_keys = []
            rejected = []
            for candidate in day.candidates:
                venue = cache[key(candidate)]
                if isinstance(venue, BaseException) or not isinstance(venue, VerifiedPlace):
                    rejected.append(candidate.name)
                    continue
                place = venue.place
                if not individual_place_category_supported(place.category) or place.placeId in reserved_ids or not place.placeId or not place.address or not math.isfinite(place.latitude) or not math.isfinite(place.longitude) or not state['destination'].contains(place.latitude, place.longitude):
                    rejected.append(candidate.name)
                    continue
                if not meal_category_supported(place.category, candidate.meal):
                    rejected.append(candidate.name)
                    continue
                pool[(place.placeId, candidate.meal)] = (candidate, venue)
                current_keys.append((place.placeId, candidate.meal))
            # Recheck retained facts against other days: a later successful day can
            # reserve a venue that was available when this day's pool was created.
            pool = {key: item for key, item in pool.items() if item[1].place.placeId not in reserved_ids and individual_place_category_supported(item[1].place.category) and meal_category_supported(item[1].place.category, item[0].meal) and item[1].place.address and math.isfinite(item[1].place.latitude) and math.isfinite(item[1].place.longitude) and state['destination'].contains(item[1].place.latitude, item[1].place.longitude)}
            # Three drafts of at most ten candidates is the request-wide hard cap.
            ordered_keys = dict.fromkeys([*current_keys, *pool])
            pools[index] = {key: pool[key] for key in ordered_keys if key in pool}
            pools[index] = dict(list(pools[index].items())[:30])
            available = list(pools[index].values())
            plans = _day_plans(available, day_date, max_stops, minimum_visits, recent_ids, state['rejected_plans'].get(index))
            all_plans.append(plans)
            if not plans:
                failures.append(f'{index + 1}일차 {day_date}: 점심·저녁과 명소 {minimum_visits}곳의 영업/식사 시간 조합 부족. 확인된 후보: {", ".join(candidate.name for candidate, _ in available)}. 이름/위치 검증 실패(재사용 금지): {", ".join(rejected)}. 다른 공식 장소명과 식사 대안을 제안하세요.')
        return {'day_plans': all_plans, 'failures': failures, 'feedback': '', 'place_cache': cache, 'candidate_pools': pools}

    async def verify_routes(state: CourseState) -> dict:
        _progress('실제 이동 시간으로 일정을 확인하고, 맞지 않으면 검증된 대안을 비교하고 있습니다.')
        validated = dict(state['validated_days'])
        cache = state['route_cache']
        failures = list(state['failures'])
        rejected_plans = {index: set(signatures) for index, signatures in state['rejected_plans'].items()}
        used = {venue.place.placeId for value in validated.values() for _, venue in value['selected']}
        def route_key(pair: tuple[Visit, Visit]) -> tuple:
            left, right = pair[0][1], pair[1][1]
            return (left.place.placeId, right.place.placeId, 'walking' if _distance(left, right) <= 1500 else 'transit')
        async def lookup(pair: tuple[Visit, Visit]) -> TransportToNextSchema:
            return await provider.route(pair[0][1], pair[1][1], mode=route_key(pair)[2])
        for index, plans in enumerate(state['day_plans']):
            if index in validated or not plans:
                continue
            last_error = '다른 일자와 장소가 겹칩니다.'
            day_date = date.fromisoformat(state['request'].tripCondition.startDate) + timedelta(days=index)
            for selected in plans[:MAX_DAY_PLANS]:
                if any(venue.place.placeId in used for _, venue in selected):
                    continue
                pairs = list(pairwise(selected))
                pending = {route_key(pair): pair for pair in pairs if route_key(pair) not in cache}
                results = await _parallel(list(pending.values()), lookup)
                cache.update(zip(pending, results))
                routes = [cache[route_key(pair)] for pair in pairs]
                if any(isinstance(result, BaseException) or not isinstance(result, TransportToNextSchema) or result.type == 'none' or result.minutes is None or not 0 < result.minutes <= 90 or result.distance is None or not math.isfinite(result.distance) or result.distance <= 0 for result in routes):
                    last_error = '경로 미확인 또는 90분 초과. 서로 가까운 다른 장소가 필요합니다.'
                    rejected_plans.setdefault(index, set()).add(_plan_signature(selected))
                    continue
                try:
                    day = _schedule_day(selected, routes, day_date, index + 1)
                except CourseGenerationError as error:
                    last_error = str(error)
                    rejected_plans.setdefault(index, set()).add(_plan_signature(selected))
                    continue
                validated[index] = {'selected': selected, 'routes': routes, 'day': day}
                used.update(venue.place.placeId for _, venue in selected)
                break
            if index not in validated:
                failures.append(f'{index + 1}일차 {day_date} 대안 {min(len(plans), MAX_DAY_PLANS)}개 소진: {last_error}')
        if failures:
            preserved = '; '.join(f'{index + 1}일차 유지: {", ".join(candidate.name for candidate, _ in value["selected"])}' for index, value in sorted(validated.items()))
            return {'validated_days': validated, 'route_cache': cache, 'rejected_plans': rejected_plans, 'feedback': ' / '.join(failures) + ' / ' + preserved}
        if used in state['recent']:
            return {'validated_days': {}, 'route_cache': cache, 'feedback': '최근 코스와 장소 집합이 동일합니다. 다른 장소를 제안하세요: ' + ', '.join(candidate.name for value in validated.values() for candidate, _ in value['selected'])}
        return {'validated_days': validated, 'route_cache': cache, 'selected': [validated[index]['selected'] for index in range(len(state['day_plans']))], 'routes': [validated[index]['routes'] for index in range(len(state['day_plans']))], 'feedback': ''}

    async def schedule(state: CourseState) -> dict:
        _progress('확정한 일자별 장소·이동·영업·식사 시간을 최종 검증하고 있습니다.')
        start_date = date.fromisoformat(state['request'].tripCondition.startDate)
        days = [_schedule_day(selected, state['routes'][index], start_date + timedelta(days=index), index + 1) for index, selected in enumerate(state['selected'])]
        return {'days': days, 'feedback': ''}

    async def finalize(state: CourseState) -> dict:
        _progress('최종 일정과 최근 코스의 중복 여부를 확인하고 있습니다.')
        request, proposal = state['request'], state['draft_data']
        trip = request.tripCondition
        course = CourseSchema(title=proposal.title, destinationCountry=trip.destinationCountry, destinationCity=trip.destinationCity, startDate=trip.startDate, totalDays=trip.totalDays, tags=proposal.tags, recommendationReason=apply_personalized_reasons(request, state['days']), itinerary=ItinerarySchema(days=state['days']))
        ids = {stop.place.placeId for day in course.itinerary.days for stop in day.stops}
        if not await history.record_if_novel(history_key(request), ids):
            return {'validated_days': {}, 'feedback': '동시에 생성된 최근 코스와 중복됩니다. 다른 장소를 제안하세요.', 'recent': await history.recent(history_key(request))}
        return {'course': course, 'feedback': ''}

    def next_node(success: str) -> Callable[[CourseState], str]:
        def decide(state: CourseState) -> str:
            if not state.get('feedback'):
                return success
            max_drafts = 3 if state['request'].tripCondition.totalDays > 1 else 2
            if state['attempt'] >= max_drafts:
                raise CourseGenerationError(state['feedback'])
            return 'draft'
        return decide

    graph = StateGraph(CourseState)
    for name, node in [('prepare', prepare), ('draft', draft), ('verify_places', verify_places), ('verify_routes', verify_routes), ('schedule', schedule), ('finalize', finalize)]:
        graph.add_node(name, node)
    graph.add_edge(START, 'prepare')
    graph.add_edge('prepare', 'draft')
    graph.add_conditional_edges('draft', next_node('verify_places'), ['verify_places', 'draft'])
    for node, successor in [('verify_places', 'verify_routes'), ('verify_routes', 'schedule'), ('schedule', 'finalize'), ('finalize', END)]:
        graph.add_conditional_edges(node, next_node(successor), [successor, 'draft'])
    return graph.compile()


async def stream_course_generation(request: CourseRequestSchema) -> AsyncGenerator[tuple[str, dict]]:
    """Stream existing progress/complete payloads while the graph actually executes.

    A request-wide timeout and generator closure cancel active graph work.
    Completion is published only after the graph and provider close successfully.
    """
    completed: dict | None = None
    provider = VerifiedMapsProvider(concurrency=settings.COURSE_MAPS_CONCURRENCY)
    try:
        async with asyncio.timeout(settings.COURSE_TIMEOUT_SECONDS):
            async with provider:
                graph = build_course_graph(provider, CourseHistory())
                async with aclosing(graph.astream({'request': request, 'attempt': 0}, stream_mode=['custom', 'updates'])) as events:
                    async for mode, value in events:
                        if mode == 'custom':
                            yield 'progress', value
                        elif 'finalize' in value and value['finalize'].get('course'):
                            completed = {'course': value['finalize']['course'].model_dump()}
    finally:
        metrics = getattr(provider, 'metrics', None)
        if isinstance(metrics, MapsCostMetrics):
            logger.info('Maps request cost summary: %s', metrics.snapshot())
    if completed is not None:
        yield 'complete', completed

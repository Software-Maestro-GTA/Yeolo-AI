"""Explicit, bounded LangGraph pipeline for personal, verified travel courses."""

import asyncio
import copy
import inspect
import logging
import math
import sqlite3
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from typing import Any, Literal, TypedDict
from zoneinfo import ZoneInfo

import httpx
from google.genai.errors import APIError
from langchain_core.exceptions import OutputParserException
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, ValidationError

from app.agent.prompts import COURSE_CANDIDATE_PROMPT
from app.agent.tools.verified_maps import (
    Destination,
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
    StopSchema,
    TransportToNextSchema,
)
from app.services.course_copy import enrich_course_place_copy
from app.services.course_diversity import (
    MAX_OVERLAP,
    Experience,
    normalize_area,
    overlap_scores,
    planning_overlap,
)
from app.services.course_history import CourseHistory, history_key
from app.services.course_images import enrich_course_images
from app.services.course_reasons import CAFE, apply_personalized_reasons, visit_tip
from app.services.course_routing import (
    distance_meters,
    estimated_walking,
    valid_route,
)
from app.services.day_summary import MAX_DAY_SUMMARY_LENGTH, compose_day_summary
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
    planned_area: str = Field(default='', max_length=100)
    experiences: list[Experience] = Field(default_factory=list, max_length=5)


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
    repairing: bool
    history_unavailable: bool
    repeated: bool
    discovered_days: set[int]
    deadline: float
    recent_profiles: list[dict]
    verified_fallback: dict
    restore_fallback: bool
    diversity_scores: dict


async def draft_candidates(request: CourseRequestSchema, recent_ids: list[str], feedback: str = '') -> CourseDraft:
    """Ask Gemini once for compact, preference-aware candidate names and reasons."""
    model = ChatGoogleGenerativeAI(model=settings.GEMINI_MODEL_NAME, google_api_key=settings.GEMINI_API_KEY, temperature=0.7, thinking_level=settings.GEMINI_THINKING_LEVEL, max_retries=0, timeout=25)
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
    return distance_meters(left, right)


# A small local search explores verified alternatives before asking the LLM again.
MAX_DAY_PLANS = 4
MEAL_WINDOWS = {'none': (540, 1260), 'breakfast': (540, 660), 'lunch': (690, 840), 'dinner': (1050, 1200)}
type Visit = tuple[Candidate, VerifiedPlace]


def _schedule_day(selected: list[Visit], routes: list[TransportToNextSchema], day_date: date, day_number: int, *, compact: bool = False) -> DayItinerarySchema:
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
        note = visit_tip(venue.place.category)
        if venue.periods is None:
            note += ' 영업시간 미확인: 방문 전 확인이 필요합니다.'
        stops.append(StopSchema(sequence=index + 1, arrivalTime=f'{arrival // 60:02d}:{arrival % 60:02d}', stayMinutes=candidate.stay_minutes, memo=note, reason=candidate.reason, cost=candidate.cost, place=venue.place, transportToNext=transport))
        current = arrival + candidate.stay_minutes + (transport.minutes or 0) + (0 if last else 10)
    try:
        summary = compose_day_summary(stops, day_number, compact=compact)
        if not isinstance(summary, str) or not summary.strip() or len(summary) > MAX_DAY_SUMMARY_LENGTH:
            raise ValueError('Invalid optional day summary')
    except Exception:  # noqa: BLE001 - optional prose cannot invalidate a verified schedule; cancellation propagates
        summary = '새로운 장소를 만나며 나만의 여행 이야기를 만들어보세요.'
        if compact:
            summary += f' 오늘은 이동과 식사 시간을 고려해 {len(stops)}곳을 둘러보세요.'
    return DayItinerarySchema(day=day_number, date=day_date.isoformat(), memo=summary, stops=stops)


def _meal_capable(venue: VerifiedPlace) -> bool:
    return any(meal_category_supported(venue.place.category, meal) for meal in ('lunch', 'dinner'))


def _core_visit(item: Visit) -> bool:
    """Count verified sightseeing venues; food and cafe stops never replace them."""
    candidate, venue = item
    return candidate.meal == 'none' and not _meal_capable(venue) and venue.place.category not in CAFE


def _visit_shortage(selected: list[Visit]) -> tuple[int, int]:
    """Return common normal floor deficits, independent of optional pace targets."""
    return max(0, 5 - len(selected)), max(0, 3 - sum(_core_visit(item) for item in selected))


def _pace_limit(request: CourseRequestSchema) -> int:
    pace = request.tasteProfile.travelPaceDensity if request.tasteProfile else 'balanced'
    return {'slow_stay': 5, 'long_stay': 5, 'dense_schedule': 7}.get(pace, 6)


def _plan_signature(selected: list[Visit]) -> tuple:
    """Identify the exact ordered visits whose real travel feasibility was checked."""
    return tuple((venue.place.placeId, candidate.meal, candidate.stay_minutes) for candidate, venue in selected)


def _max_drafts(state: CourseState) -> int:
    return 3 if state['request'].tripCondition.totalDays > 1 else 2


def _recoverable_draft(error: Exception) -> bool:
    """Retry temporary model and structured-output failures, never configuration."""
    if isinstance(error, APIError):
        return error.code == 429 or 500 <= error.code < 600
    return isinstance(error, (TimeoutError, httpx.TimeoutException, httpx.NetworkError, OutputParserException, ValidationError))


def _selection_metadata(selected: list[list[Visit]]) -> dict:
    """Remember only selected attraction IDs and their original model choices."""
    attractions = [(candidate, venue) for day in selected for candidate, venue in day if _core_visit((candidate, venue))]
    return {
        'attraction_ids': sorted({venue.place.placeId for _, venue in attractions}),
        'areas': sorted({normalize_area(candidate.planned_area) for candidate, _ in attractions if candidate.planned_area.strip()}),
        'experiences': sorted({value for candidate, _ in attractions for value in candidate.experiences}),
    }


def _diversity_rank(path: list[Visit], profiles: list[dict]) -> tuple:
    metadata = _selection_metadata([path])
    scores = overlap_scores([venue.place.placeId for _, venue in path], metadata['attraction_ids'], profiles)
    # Rank diversity after the common visit floor; extra stops are optional.
    areas = {normalize_area(candidate.planned_area) for candidate, _ in path if candidate.planned_area.strip()}
    area_overlap = planning_overlap(metadata['areas'], profiles, 'areas')
    misses_target = max(scores['attraction_overlap'], scores['place_overlap'], area_overlap) > MAX_OVERLAP
    return (misses_target, scores['attraction_overlap'], scores['place_overlap'], area_overlap, max(0, len(areas) - 1), planning_overlap(metadata['experiences'], profiles, 'experiences'))


def _day_plans(available: list[Visit], day_date: date, max_stops: int, minimum_visits: int, recent_ids: set[str], rejected_plans: set[tuple] | None = None, *, plan_limit: int = MAX_DAY_PLANS, compact: bool = False, recent_profiles: list[dict] | None = None) -> list[list[Visit]]:
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
                visits = sum(_core_visit(entry) for entry in new_path)
                required = len({'lunch', 'dinner'} - new_meals) + max(0, minimum_visits - visits)
                if max_stops - depth - 1 < required:
                    continue
                new_distance = distance + (_distance(path[-1][1], venue) if path else 0)
                # Ten minutes of transfer buffer is known even before route lookup.
                new_state = (new_path, arrival + candidate.stay_minutes + 10, new_distance)
                following.append(new_state)
                if not required:
                    completed.append(new_state)
        following.sort(key=lambda state: ((_diversity_rank(state[0], recent_profiles) if recent_profiles else ()), state[1], sum(venue.place.placeId in recent_ids for _, venue in state[0]), state[2]))
        beam = following[:48]
        if not beam:
            break
    completed.sort(key=lambda state: (_visit_shortage(state[0]), (_diversity_rank(state[0], recent_profiles) if recent_profiles else ()), len(state[0]) if compact else -len(state[0]), not any(candidate.meal == 'breakfast' for candidate, _ in state[0]), sum(venue.place.placeId in recent_ids for _, venue in state[0]), state[2], state[1]))
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
    if not recent_profiles and not compact and preferred and len(preferred[0]) == len(completed[0][0]) and valid_meals in (['lunch', 'dinner'], ['breakfast', 'lunch', 'dinner']):
        completed.remove(preferred)
        completed.insert(0, preferred)
    plans: list[list[Visit]] = []
    signatures: set[tuple] = set()
    meal_sets: set[tuple] = set()
    for diverse in ((False,) if recent_profiles else (True, False)):
        for path, _, _ in completed:
            signature = _plan_signature(path)
            meal_set = tuple((candidate.meal, venue.place.placeId) for candidate, venue in path if candidate.meal != 'none')
            if signature in signatures or signature in (rejected_plans or set()) or (diverse and meal_set in meal_sets):
                continue
            plans.append(path)
            signatures.add(signature)
            meal_sets.add(meal_set)
            if len(plans) == plan_limit:
                return plans
    return plans


def build_course_graph(provider: VerifiedMapsProvider, history: CourseHistory) -> Any:
    """Compile explicit generation nodes using injectable provider/history boundaries.

    Returns a compiled LangGraph; validation failures receive one repair, or two for multi-day trips.
    """
    async def read_history(request: CourseRequestSchema) -> tuple[list[set[str]], list[dict]]:
        recent = await history.recent(history_key(request))
        read_profiles = getattr(history, 'recent_profiles', None)
        if inspect.iscoroutinefunction(read_profiles):
            profiles = await read_profiles(history_key(request))
            if isinstance(profiles, list):
                return recent, profiles
        return recent, [{'place_ids': sorted(ids)} for ids in recent]

    def optional_budget(state: CourseState) -> float:
        remaining = state.get('deadline', float('inf')) - asyncio.get_running_loop().time() - 1.
        return min(25., max(0., remaining))

    async def optional_node(node: Callable[[CourseState], Awaitable[dict]], state: CourseState) -> dict:
        """Bound novelty-only work while preserving time for safe completion."""
        if not state.get('verified_fallback'):
            return await node(state)
        budget = optional_budget(state)
        if budget <= .1:
            return {'restore_fallback': True, 'feedback': ''}
        try:
            async with asyncio.timeout(budget):
                return await node(state)
        except TimeoutError:
            logger.info('Course diversity fallback: optional validation timeout')
            return {'restore_fallback': True, 'feedback': ''}

    def history_feedback(state: CourseState) -> str:
        profiles = state.get('recent_profiles', [])
        areas = sorted({value for item in profiles for value in item.get('areas', [])})
        experiences = sorted({value for item in profiles for value in item.get('experiences', [])})
        return ('최근 계획 권역: ' + ', '.join(areas[:30]) + '. 최근 경험 구성: ' + ', '.join(experiences) + '. '
                '사용자의 높은 취향을 유지하며 최근 계획과 다른 권역을 우선하세요. 권역은 하루 안에서 가까운 장소로 묶으세요. 낮은 취향을 다양성 때문에 강제하지 마세요. '
                '각 후보에 모델 계획 planned_area와 experiences를 작성하세요. 권역 이름만 바꿔 동일 장소를 다시 제안하지 마세요.') if profiles else ''

    async def prepare(state: CourseState) -> dict:
        _progress('여행 조건과 최근 코스 이력을 확인하고 있습니다.')
        request = state['request']
        date.fromisoformat(request.tripCondition.startDate)
        destination = await provider.resolve_destination(request.tripCondition.destinationCountry, request.tripCondition.destinationCity)
        unavailable = False
        try:
            async with asyncio.timeout(1.):
                recent, profiles = await read_history(request)
        except (OSError, sqlite3.Error, TimeoutError):
            logger.warning('Course history read unavailable')
            recent, profiles, unavailable = [], [], True
        return {'destination': destination, 'recent': recent, 'recent_profiles': profiles, 'history_unavailable': unavailable, 'attempt': 0, 'feedback': '', 'validated_days': {}, 'place_cache': {}, 'route_cache': {}, 'candidate_pools': {}, 'rejected_plans': {}, 'repairing': False, 'repeated': False, 'discovered_days': set(), 'restore_fallback': False, 'verified_fallback': {}}

    async def draft(state: CourseState) -> dict:
        _progress('취향에 맞는 장소 후보와 식사 대안을 구성하고 있습니다.')
        try:
            result = await draft_candidates(state['request'], sorted(set().union(*state['recent'])) if state['recent'] else [], history_feedback(state) + '\n' + state.get('feedback', ''))
        except Exception as error:
            if not _recoverable_draft(error):
                raise
            if state.get('verified_fallback'):
                return {'attempt': state['attempt'] + 1, 'restore_fallback': True, 'feedback': ''}
            return {'attempt': state['attempt'] + 1, 'feedback': '모델의 일시적 응답 오류입니다. 기존에 확인한 장소를 유지하고 필요한 대안을 다시 제안하세요.'}
        if len(result.days) != state['request'].tripCondition.totalDays:
            if state.get('verified_fallback'):
                return {'attempt': state['attempt'] + 1, 'restore_fallback': True, 'feedback': ''}
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
        discovered_days = set(state.get('discovered_days', set()))
        def key(candidate: Candidate) -> tuple:
            return (candidate.name.strip().casefold(), candidate.english_name.strip().casefold(), candidate.meal)
        unique = {key(candidate): candidate for index, day in enumerate(state['draft_data'].days) if index not in saved for candidate in day.candidates if key(candidate) not in cache}
        results = await _parallel(list(unique.values()), lambda candidate: provider.search(candidate, state['destination']))
        temporary_names = {candidate.name for candidate, result in zip(unique.values(), results) if isinstance(result, MapsProviderError) and result.transient}
        for candidate_key, venue in zip(unique, results):
            if isinstance(venue, asyncio.CancelledError):
                raise venue
            if isinstance(venue, MapsProviderError) and venue.kind in {'unauthorized', 'invalid'}:
                raise venue
            if isinstance(venue, VerifiedPlace):
                cache[candidate_key] = venue
        recent_ids = set().union(*state['recent']) if state['recent'] else set()
        max_stops = _pace_limit(state['request'])
        minimum_visits = 3
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
                venue = cache.get(key(candidate))
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
            meal_ids = {
                meal: {venue.place.placeId for candidate, venue in available if meal_category_supported(venue.place.category, meal) and _opening_start(venue.periods, day_date, MEAL_WINDOWS[meal][0], candidate.stay_minutes, MEAL_WINDOWS[meal][1]) is not None}
                for meal in ('lunch', 'dinner')
            }
            enough_meals = any(lunch != dinner for lunch in meal_ids['lunch'] for dinner in meal_ids['dinner'])
            anchor = next((venue for candidate, venue in available if candidate.meal == 'none' and not _meal_capable(venue)), None)
            discover = getattr(provider, 'discover_meals', None)
            if not enough_meals and anchor and index not in discovered_days and inspect.iscoroutinefunction(discover):
                _progress(f'{index + 1}일차의 확인된 명소 주변에서 실제 식당 대안을 보충하고 있습니다.')
                discovered_days.add(index)
                try:
                    meals = await discover(state['destination'], anchor)
                except MapsProviderError as error:
                    if error.kind in {'unauthorized', 'invalid'}:
                        raise
                    meals = []
                meal_costs = [candidate.cost for candidate in day.candidates if candidate.meal in {'lunch', 'dinner'} and candidate.cost > 0]
                planned_cost = round(sum(meal_costs) / len(meal_costs)) if meal_costs else 18000
                additions = {}
                for venue in meals[:5]:
                    if not isinstance(venue, VerifiedPlace) or venue.place.placeId in reserved_ids or not _meal_capable(venue):
                        continue
                    if not venue.place.address or not individual_place_category_supported(venue.place.category) or not state['destination'].contains(venue.place.latitude, venue.place.longitude) or _distance(anchor, venue) > 1000:
                        continue
                    candidate = Candidate(name=venue.place.placeName, english_name=venue.place.placeEngName, meal='lunch', stay_minutes=45, cost=planned_cost)
                    additions[(venue.place.placeId, candidate.meal)] = (candidate, venue)
                pools[index] = dict(list({**additions, **pools[index]}.items())[:30])
                available = list(pools[index].values())
            plans = _day_plans(available, day_date, max_stops, minimum_visits, recent_ids, state['rejected_plans'].get(index), recent_profiles=state.get('recent_profiles'))
            if not plans:
                # Retain a safe short course before optional missing-slot refill.
                for core_count in (2, 1):
                    plans = _day_plans(available, day_date, max_stops, core_count, recent_ids, state['rejected_plans'].get(index), recent_profiles=state.get('recent_profiles'))
                    if plans:
                        break
            all_plans.append(plans)
            if not plans:
                failures.append(f'{index + 1}일차 {day_date}: 점심·저녁과 명소 {minimum_visits}곳의 영업/식사 시간 조합 부족. 확인된 후보: {", ".join(candidate.name for candidate, _ in available)}. 일시 조회 실패(재조회 가능): {", ".join(name for name in rejected if name in temporary_names)}. 이름/위치 검증 미충족: {", ".join(name for name in rejected if name not in temporary_names)}. 다른 공식 장소명과 식사 대안을 제안하세요.')
        return {'day_plans': all_plans, 'failures': failures, 'feedback': '', 'place_cache': cache, 'candidate_pools': pools, 'discovered_days': discovered_days}

    async def verify_routes(state: CourseState) -> dict:
        _progress('실제 이동 시간으로 일정을 확인하고, 맞지 않으면 검증된 대안을 비교하고 있습니다.')
        validated = dict(state['validated_days'])
        cache = state['route_cache']
        failures = list(state['failures'])
        rejected_plans = {index: set(signatures) for index, signatures in state['rejected_plans'].items()}
        used = {venue.place.placeId for value in validated.values() for _, venue in value['selected']}
        # Share one failed lookup within this validation pass only. A fresh draft
        # can retry a transient edge; failures never enter the successful cache.
        pass_failures: dict[tuple, MapsProviderError] = {}
        def route_key(left: VerifiedPlace, right: VerifiedPlace, departure: datetime | None, mode: str | None = None) -> tuple:
            mode = mode or ('transit' if state['destination'].country_code == 'KR' or _distance(left, right) > 1500 else 'walking')
            return (left.place.placeId, right.place.placeId, mode, departure.isoformat() if departure and mode == 'transit' else None)

        async def actual_route(left: VerifiedPlace, right: VerifiedPlace, departure: datetime | None, mode: str) -> TransportToNextSchema:
            """Share successful and failed actual mode lookups within their bounds."""
            key = route_key(left, right, departure, mode)
            if key in cache:
                return cache[key]
            if key in pass_failures:
                raise pass_failures[key]
            try:
                route = await provider.route(left, right, mode=mode, departure_time=departure if mode == 'transit' else None)
            except MapsProviderError as error:
                pass_failures[key] = error
                raise
            if valid_route(route, left, right):
                cache[key] = route
            return route

        async def lookup(left: VerifiedPlace, right: VerifiedPlace, departure: datetime | None) -> TransportToNextSchema:
            key = route_key(left, right, departure)
            mode = key[2]
            if key in cache:
                return cache[key]
            try:
                return await actual_route(left, right, departure, mode)
            except MapsProviderError as error:
                if error.kind not in {'no_route', 'route_data', 'transient'}:
                    raise
                alternative = None
                if mode == 'transit' and _distance(left, right) <= 3000:
                    alternative = 'walking'
                elif mode == 'walking' and error.kind in {'no_route', 'route_data'}:
                    alternative = 'transit'
                if alternative:
                    try:
                        # A valid but overlong route must remain a failed plan;
                        # its successful response cannot become a short estimate.
                        return await actual_route(left, right, departure, alternative)
                    except MapsProviderError as alternative_error:
                        if alternative_error.kind not in {'no_route', 'route_data', 'transient'}:
                            raise
                        if _distance(left, right) > 1000:
                            raise
                if _distance(left, right) > 1000:
                    raise
                route = estimated_walking(left, right)
                cache[key] = route
                return route

        for index, plans in enumerate(state['day_plans']):
            if index in validated or not plans:
                continue
            last_error = '다른 일자와 장소가 겹칩니다.'
            day_date = date.fromisoformat(state['request'].tripCondition.startDate) + timedelta(days=index)
            limit = 12 if state.get('repairing') else MAX_DAY_PLANS
            for selected in plans[:limit]:
                if any(venue.place.placeId in used for _, venue in selected):
                    continue
                routes = []
                current = 540
                temporary_failure = False
                try:
                    for left, right in pairwise(selected):
                        candidate, venue = left
                        opening, latest = MEAL_WINDOWS[candidate.meal]
                        arrival = _opening_start(venue.periods, day_date, max(current, opening), candidate.stay_minutes, latest)
                        if arrival is None:
                            raise CourseGenerationError(f'{candidate.name}: 영업/식사 시간 안에 방문할 수 없습니다.')
                        departure = None
                        if state['destination'].country_code == 'KR':
                            departure = datetime.combine(day_date, time(), tzinfo=ZoneInfo('Asia/Seoul')) + timedelta(minutes=arrival + candidate.stay_minutes)
                        route = await lookup(venue, right[1], departure)
                        if not valid_route(route, venue, right[1]):
                            raise CourseGenerationError(f'{candidate.name} → {right[0].name}: 경로 정보가 불완전하거나 이동 시간이 90분을 초과합니다.')
                        routes.append(route)
                        current = arrival + candidate.stay_minutes + route.minutes + 10
                    day = _schedule_day(selected, routes, day_date, index + 1, compact=any(_visit_shortage(selected)))
                except MapsProviderError as error:
                    if error.kind in {'unauthorized', 'invalid'}:
                        raise
                    temporary_failure = error.transient
                    last_error = ('일시적인 경로 조회 장애' if temporary_failure else '경로 미확인') + f': {left[0].name} → {right[0].name}. 서로 가까운 다른 장소가 필요합니다.'
                except (CourseGenerationError, ValueError) as error:
                    last_error = str(error)
                else:
                    validated[index] = {'selected': selected, 'routes': routes, 'day': day, 'compact': any(_visit_shortage(selected))}
                    used.update(venue.place.placeId for _, venue in selected)
                    break
                if not temporary_failure:
                    rejected_plans.setdefault(index, set()).add(_plan_signature(selected))
            if index not in validated:
                failures.append(f'{index + 1}일차 {day_date} 대안 {min(len(plans), limit)}개 소진: {last_error}')
        if failures:
            if state.get('verified_fallback'):
                return {'route_cache': cache, 'restore_fallback': True, 'feedback': ''}
            preserved = '; '.join(f'{index + 1}일차 유지: {", ".join(candidate.name for candidate, _ in value["selected"])}' for index, value in sorted(validated.items()))
            return {'validated_days': validated, 'route_cache': cache, 'rejected_plans': rejected_plans, 'feedback': ' / '.join(failures) + ' / ' + preserved}
        selected = [validated[index]['selected'] for index in range(len(state['day_plans']))]
        routes = [validated[index]['routes'] for index in range(len(state['day_plans']))]
        metadata = _selection_metadata(selected)
        scores = overlap_scores(used, metadata['attraction_ids'], state.get('recent_profiles', []))
        area_overlap = planning_overlap(metadata['areas'], state.get('recent_profiles', []), 'areas')
        repeated = max(scores.values()) > MAX_OVERLAP or area_overlap > MAX_OVERLAP
        shortages = [_visit_shortage(day) for day in selected]
        rank = (sum(value[0] for value in shortages), sum(value[1] for value in shortages), max(scores.values()), area_overlap)
        snapshot = copy.deepcopy({'draft_data': state['draft_data'], 'validated_days': validated, 'selected': selected, 'routes': routes, 'diversity_scores': {**scores, 'planned_area_overlap': area_overlap}, 'repeated': repeated, 'rank': rank})
        previous = state.get('verified_fallback')
        if previous and previous['rank'] <= rank:
            snapshot = previous
        logger.info('Course diversity: place_overlap=%.2f attraction_overlap=%.2f planned_area_overlap=%.2f attempt=%d', scores['place_overlap'], scores['attraction_overlap'], area_overlap, state['attempt'])
        updates = {'validated_days': validated, 'route_cache': cache, 'selected': selected, 'routes': routes, 'repeated': repeated, 'feedback': '', 'verified_fallback': snapshot, 'diversity_scores': {**scores, 'planned_area_overlap': area_overlap}}
        underfull = any(any(value) for value in shortages)
        if underfull and state['attempt'] < _max_drafts(state) and not state.get('repairing') and optional_budget(state) > .1:
            retained = {index: value for index, value in validated.items() if not any(_visit_shortage(value['selected']))}
            missing = '; '.join(f'{index + 1}일차: 총 장소 {count}곳, 관광·체험 명소 {core}곳 부족. 검증된 가까운 후보 유지: ' + ', '.join(candidate.name for candidate, _ in validated[index]['selected']) for index, (count, core) in enumerate(shortages) if count or core)
            updates.update({'validated_days': retained, 'feedback': missing + '. 부족한 관광·체험 슬롯을 가까운 실제 명소로 보충하고 성공한 일차는 유지하세요.'})
        elif underfull and previous and previous['rank'] <= rank:
            updates['restore_fallback'] = True
        elif repeated and state['attempt'] < _max_drafts(state) and optional_budget(state) > .1:
            updates.update({'validated_days': {}, 'feedback': '최근 코스의 명소 또는 계획 권역이 반복됩니다. 취향을 유지하고 다른 권역의 실제 장소를 제안하세요. 이미 확인한 후보: ' + ', '.join(candidate.name for value in validated.values() for candidate, _ in value['selected'])})
        elif repeated and previous and previous['rank'] <= rank:
            updates['restore_fallback'] = True
        return updates

    async def restore(state: CourseState) -> dict:
        """Restore a complete feasible snapshot, including its own draft metadata."""
        _progress('확인된 장소와 이동 경로를 유지하여 일정을 마무리하고 있습니다.')
        logger.info('Course diversity fallback: retained verified itinerary')
        snapshot = state['verified_fallback']
        return {**{key: copy.deepcopy(value) for key, value in snapshot.items() if key != 'rank'}, 'feedback': '', 'restore_fallback': False, 'attempt': _max_drafts(state)}

    async def schedule(state: CourseState) -> dict:
        _progress('확정한 일자별 장소·이동·영업·식사 시간을 최종 검증하고 있습니다.')
        start_date = date.fromisoformat(state['request'].tripCondition.startDate)
        days = [_schedule_day(selected, state['routes'][index], start_date + timedelta(days=index), index + 1, compact=bool(state['validated_days'][index].get('compact'))) for index, selected in enumerate(state['selected'])]
        return {'days': days, 'feedback': ''}

    async def finalize(state: CourseState) -> dict:
        _progress('최종 일정과 최근 코스의 중복 여부를 확인하고 있습니다.')
        request, proposal = state['request'], state['draft_data']
        trip = request.tripCondition
        course = CourseSchema(title=proposal.title, destinationCountry=trip.destinationCountry, destinationCity=trip.destinationCity, startDate=trip.startDate, totalDays=trip.totalDays, tags=proposal.tags, recommendationReason=apply_personalized_reasons(request, state['days']), itinerary=ItinerarySchema(days=state['days']))
        ids = {stop.place.placeId for day in course.itinerary.days for stop in day.stops}
        unavailable = state.get('history_unavailable', False)
        novel = False
        try:
            budget = min(2., optional_budget(state))
            if budget <= .1:
                raise TimeoutError('History storage skipped to preserve completion margin')
            async with asyncio.timeout(budget):
                record = history.record_if_novel
                parameters = inspect.signature(record).parameters
                supports_metadata = 'metadata' in parameters or any(value.kind == inspect.Parameter.VAR_KEYWORD for value in parameters.values())
                kwargs = {'metadata': _selection_metadata(state['selected']), 'max_overlap': None if state.get('repeated') else MAX_OVERLAP} if supports_metadata else {}
                novel = await record(history_key(request), ids, **kwargs)
                if not novel and state['attempt'] < _max_drafts(state) and optional_budget(state) > .1:
                    recent, profiles = await read_history(request)
                    return {'validated_days': {}, 'feedback': '동시에 생성된 최근 코스와 유사합니다. 취향과 검증된 후보를 유지하며 다른 권역·명소를 제안하세요.', 'recent': recent, 'recent_profiles': profiles}
                if not novel and supports_metadata and not state.get('repeated'):
                    # Explicit soft acceptance after bounded concurrency retries.
                    novel = await record(history_key(request), ids, metadata=_selection_metadata(state['selected']))
                    state = {**state, 'repeated': True}
        except (OSError, sqlite3.Error, TimeoutError):
            logger.warning('Course history write unavailable')
            unavailable = True
        if unavailable:
            course.recommendationReason += ' 생성 이력 확인·저장이 일시적으로 제한되어 최근 코스와 겹칠 수 있어요.'
        elif not novel or state.get('repeated'):
            course.recommendationReason += ' 확인 가능한 장소와 일정 조건이 제한되어 이전 코스와 일부 방문 장소 또는 계획 권역이 겹칠 수 있어요.'
        return {'course': course, 'feedback': ''}

    async def enrich_place_copy(state: CourseState) -> dict:
        """Write final-place prose once, reserving photo and completion budgets."""
        _progress('확정한 장소의 추천 이유와 방문 팁을 작성하고 있습니다.')
        budget = 25.0
        if 'deadline' in state:
            budget = min(budget, state['deadline'] - asyncio.get_running_loop().time() - 6.0)
        if budget <= .1:
            return {'course': state['course']}
        context = {
            (index + 1, sequence + 1, venue.place.placeId): {'experiences': candidate.experiences}
            for index, selected in enumerate(state['selected'])
            for sequence, (candidate, venue) in enumerate(selected)
        }
        course = await enrich_course_place_copy(state['course'], state['request'], selection_context=context, timeout_seconds=budget)
        return {'course': course}

    async def enrich_images(state: CourseState) -> dict:
        """Use only residual optional time for final-place photos and attribution."""
        _progress('확정한 장소의 실제 사진과 대표 이미지를 준비하고 있습니다.')
        budget = 5.0
        if 'deadline' in state:
            budget = min(budget, state['deadline'] - asyncio.get_running_loop().time() - 1)
        course = await enrich_course_images(state['course'], provider, timeout_seconds=max(0, budget))
        return {'course': course}

    async def repair(state: CourseState) -> dict:
        """Use retained real venues for one final bounded, less dense schedule."""
        _progress('확인한 장소로 이동과 식사에 여유가 있는 대안 일정을 구성하고 있습니다.')
        saved = state['validated_days']
        reserved = {venue.place.placeId for value in saved.values() for _, venue in value['selected']}
        recent_ids = set().union(*state['recent']) if state['recent'] else set()
        start = date.fromisoformat(state['request'].tripCondition.startDate)
        plans, failures = [], []
        for index in range(state['request'].tripCondition.totalDays):
            if index in saved:
                plans.append([saved[index]['selected']])
                continue
            available = [item for item in state['candidate_pools'].get(index, {}).values() if item[1].place.placeId not in reserved]
            alternatives = []
            for stop_count, core_count in ((5, 3), (4, 2), (3, 1)):
                alternatives.extend(_day_plans(available, start + timedelta(days=index), stop_count, core_count, recent_ids, state['rejected_plans'].get(index), plan_limit=4, compact=True, recent_profiles=state.get('recent_profiles')))
            plans.append(alternatives)
            if not alternatives:
                failures.append(f'{index + 1}일차: 점심·저녁과 명소를 포함한 검증 가능한 대안이 없습니다.')
        return {'day_plans': plans, 'failures': failures, 'feedback': '', 'repairing': True}

    def next_node(success: str) -> Callable[[CourseState], str]:
        def decide(state: CourseState) -> str:
            if state.get('restore_fallback'):
                return 'restore'
            if not state.get('feedback'):
                return success
            if state['attempt'] >= _max_drafts(state):
                if state.get('verified_fallback'):
                    return 'restore'
                if not state.get('repairing') and state.get('draft_data'):
                    return 'repair'
                raise CourseGenerationError(state['feedback'])
            return 'draft'
        return decide

    graph = StateGraph(CourseState)
    for name, node in [('prepare', prepare), ('draft', draft), ('verify_places', verify_places), ('verify_routes', verify_routes), ('schedule', schedule), ('finalize', finalize), ('repair', repair), ('enrich_place_copy', enrich_place_copy), ('enrich_images', enrich_images)]:
        if name in {'draft', 'verify_places', 'verify_routes'}:
            async def bounded(state: CourseState, operation: Callable = node) -> dict:
                return await optional_node(operation, state)
            graph.add_node(name, bounded)
        else:
            graph.add_node(name, node)
    graph.add_node('restore', restore)
    graph.add_edge('restore', 'schedule')
    graph.add_edge(START, 'prepare')
    graph.add_edge('prepare', 'draft')
    graph.add_conditional_edges('draft', next_node('verify_places'), ['verify_places', 'draft', 'repair', 'restore'])
    graph.add_edge('repair', 'verify_routes')
    for node, successor in [('verify_places', 'verify_routes'), ('verify_routes', 'schedule'), ('schedule', 'finalize'), ('finalize', 'enrich_place_copy')]:
        graph.add_conditional_edges(node, next_node(successor), [successor, 'draft', 'repair', 'restore'])
    graph.add_edge('enrich_place_copy', 'enrich_images')
    graph.add_edge('enrich_images', END)
    return graph.compile()


async def stream_course_generation(request: CourseRequestSchema, *, deadline: float | None = None) -> AsyncGenerator[tuple[str, dict]]:
    """Stream existing progress/complete payloads while the graph actually executes.

    A request-wide timeout and generator closure cancel active graph work.
    Completion is published only after the graph and provider close successfully.
    """
    completed: dict | None = None
    provider = VerifiedMapsProvider(concurrency=settings.COURSE_MAPS_CONCURRENCY)
    now = asyncio.get_running_loop().time()
    request_deadline = min(deadline if deadline is not None else float('inf'), now + min(90., settings.COURSE_TIMEOUT_SECONDS))
    remaining = max(0., request_deadline - now)
    # Leave up to one second for cancelled provider/graph work to close cleanly.
    processing_deadline = request_deadline - min(1., remaining * .1)
    try:
        async with asyncio.timeout_at(processing_deadline) as timeout:
            async with provider:
                graph = build_course_graph(provider, CourseHistory())
                async with aclosing(graph.astream({'request': request, 'attempt': 0, 'deadline': timeout.when()}, stream_mode=['custom', 'updates'])) as events:
                    async for mode, value in events:
                        if mode == 'custom':
                            yield 'progress', value
                        elif 'enrich_images' in value and value['enrich_images'].get('course'):
                            completed = {'course': value['enrich_images']['course'].model_dump()}
    finally:
        metrics = getattr(provider, 'metrics', None)
        if isinstance(metrics, MapsCostMetrics):
            logger.info('Maps request cost summary: %s', metrics.snapshot())
    if completed is not None:
        yield 'complete', completed

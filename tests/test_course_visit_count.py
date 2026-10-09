"""Exercise minimum visit completeness and bounded safety fallback without paid APIs."""

import asyncio
import copy
from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.schemas.course import CourseRequestSchema, PlaceSchema, TransportToNextSchema


@pytest.fixture
def visit_dependencies(mocker, tmp_path):
    from app.agent.course_state import Candidate, CourseDraft, DraftDay
    from app.agent.tools.verified_maps import Destination, VerifiedPlace
    from app.services.course_history import CourseHistory

    request = CourseRequestSchema.model_validate({'userId': '550e8400-e29b-41d4-a716-446655440000', 'mbti': 'INTJ', 'tripCondition': {'destinationCountry': '대한민국', 'destinationCity': '서울', 'startDate': '2026-10-05', 'totalDays': 1, 'budgetType': 'moderate'}})
    places = {}

    def make_draft(prefix='base', cores=3, extras=(), days=1):
        result = []
        for day in range(days):
            candidates = []
            roles = [('none', 'museum'), ('lunch', 'restaurant'), ('none', 'park'), ('dinner', 'restaurant')]
            if cores < 2:
                roles = [roles[0], roles[1], roles[3]]
            roles += [('none', 'art_gallery')] * max(0, cores - 2)
            roles += list(extras)
            for number, (meal, category) in enumerate(roles):
                name = f'{prefix}-{day}-{number}'
                candidate = Candidate(name=name, meal=meal, stay_minutes=40, planned_area=prefix, experiences=['food' if category == 'restaurant' else 'culture'])
                candidates.append(candidate)
                places[name] = VerifiedPlace(PlaceSchema(placeId=f'places/{name}', placeName=name, category=category, address='대한민국 서울', latitude=37.5 + day * .01 + number * .0001, longitude=127.))
            result.append(DraftDay(candidates=candidates))
        return CourseDraft(title=f'{prefix} 계획', reason='문화와 휴식', days=result)

    provider = mocker.Mock()
    provider.resolve_destination = AsyncMock(return_value=Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3))
    provider.search = AsyncMock(side_effect=lambda candidate, destination: places[candidate.name])
    provider.route = AsyncMock(return_value=TransportToNextSchema(type='walking', distance=400, minutes=10, cost=0))
    provider.discover_meals = AsyncMock(return_value=[])
    provider.discover_attractions = AsyncMock(return_value=[])
    provider.photo = AsyncMock(return_value=None)
    llm = mocker.patch('app.agent.course_nodes.draft_candidates', new_callable=AsyncMock)
    history = CourseHistory(tmp_path / 'history.sqlite3')
    return request, provider, history, llm, places, make_draft


def profile_for_pace(pace):
    from app.schemas.taste_profile import (
        ActivityPreferenceSchema,
        FoodPreferenceSchema,
        PreferredLocationTypeSchema,
        TasteProfileSchema,
        TravelPurposeSchema,
    )

    data = {field: dict.fromkeys(schema.model_fields, 1) for field, schema in {'travelPurpose': TravelPurposeSchema, 'foodPreference': FoodPreferenceSchema, 'activityPreference': ActivityPreferenceSchema, 'preferredLocationType': PreferredLocationTypeSchema}.items()}
    return TasteProfileSchema.model_validate({**data, 'travelPaceDensity': pace, 'spendingTendency': 'moderate', 'companionType': 'solo'})


def core_ids(day):
    from app.agent.tools.verified_maps import meal_category_supported
    from app.services.course_reasons import CAFE

    return {stop.place.placeId for stop in day.stops if stop.place.category not in CAFE and not meal_category_supported(stop.place.category, 'lunch')}


@pytest.mark.parametrize(('pace', 'expected'), [('slow_stay', 5), ('long_stay', 5), ('balanced', 6), ('dense_schedule', 7)])
@pytest.mark.asyncio
async def test_pace_targets_require_three_verified_core_visits(visit_dependencies, pace, expected):
    from app.agent.course_graph import build_course_graph

    request, provider, history, llm, _, make_draft = visit_dependencies
    request.tasteProfile = profile_for_pace(pace)
    llm.return_value = make_draft(cores=3, extras=[('breakfast', 'restaurant'), ('none', 'coffee_shop')])
    day = (await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0}))['course'].itinerary.days[0]
    assert len(day.stops) == expected
    assert len(core_ids(day)) >= 3
    llm.assert_awaited_once()
    provider.discover_attractions.assert_not_awaited()
    assert '줄' not in day.memo


@pytest.mark.asyncio
async def test_five_complete_visits_beat_four_more_novel_visits_in_same_verified_pool(visit_dependencies):
    from app.agent.course_graph import build_course_graph
    from app.services.course_history import history_key

    request, provider, history, llm, places, make_draft = visit_dependencies
    rich = make_draft('rich', cores=3)
    novel = make_draft('novel', cores=2)
    old = {places[c.name].place.placeId for c in rich.days[0].candidates[:1]}
    await history.record_if_novel(history_key(request), old, metadata={'attraction_ids': list(old)})
    rich.days[0].candidates += novel.days[0].candidates
    llm.return_value = rich
    day = (await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0}))['course'].itinerary.days[0]
    assert len(day.stops) >= 5 and len(core_ids(day)) >= 3
    llm.assert_awaited_once()
    assert provider.search.await_count == 9


@pytest.mark.parametrize('food_category', ['coffee_shop', 'bakery', 'dessert_restaurant', 'japanese_restaurant'])
@pytest.mark.asyncio
async def test_food_none_or_breakfast_cannot_fill_core_shortage_and_only_new_core_is_looked_up(visit_dependencies, food_category):
    from app.agent.course_graph import build_course_graph

    request, provider, history, llm, places, make_draft = visit_dependencies
    first = make_draft(cores=2, extras=[('none', food_category), ('breakfast', 'restaurant')])
    second = first.model_copy(deep=True)
    added = make_draft('new-core', cores=3).days[0].candidates[-1]
    second.days[0].candidates.append(added)
    llm.side_effect = [first, second]
    day = (await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0}))['course'].itinerary.days[0]
    assert len(day.stops) >= 5 and len(core_ids(day)) >= 3
    assert places[added.name].place.placeId in core_ids(day)
    assert llm.await_count == 2
    feedback = llm.await_args_list[1].args[2]
    assert '1일차' in feedback and ('관광' in feedback or '명소' in feedback) and '1' in feedback
    assert provider.search.await_count == len(first.days[0].candidates) + 1
    assert len({call.args[0].name for call in provider.search.await_args_list}) == provider.search.await_count


@pytest.mark.parametrize('failure', ['timeout', 'unsafe', 'deadline'])
@pytest.mark.asyncio
async def test_count_fill_failure_preserves_verified_four_with_explicit_day_note(visit_dependencies, failure):
    from app.agent.course_graph import build_course_graph
    from app.agent.tools.verified_maps import VerifiedPlace

    request, provider, history, llm, places, make_draft = visit_dependencies
    first = make_draft(cores=2)
    second = make_draft('new', cores=3)
    if failure == 'timeout':
        llm.side_effect = [first] + [TimeoutError('bounded refill failed')] * 3
    elif failure == 'unsafe':
        for candidate in second.days[0].candidates:
            places[candidate.name] = VerifiedPlace(places[candidate.name].place.model_copy(update={'latitude': 0.}))
        llm.side_effect = [first, second, second, second]
    else:
        async def response(*args, **kwargs):
            if llm.await_count > 1:
                await asyncio.Event().wait()
            return first
        llm.side_effect = response
    now = asyncio.get_running_loop().time()
    state = {'request': request, 'attempt': 0}
    if failure == 'deadline':
        state['deadline'] = now + .6
    course = (await asyncio.wait_for(build_course_graph(provider, history).ainvoke(state), timeout=.9 if failure == 'deadline' else 10))['course']
    day = course.itinerary.days[0]
    assert len(day.stops) == 4 and len(core_ids(day)) == 2
    assert course.title == f'{request.tripCondition.destinationCity} {request.tripCondition.totalDays}일 여행'
    assert '4' in day.memo and ('이동' in day.memo or '영업' in day.memo)
    if failure == 'deadline':
        assert llm.await_count <= 2
    else:
        assert 3 <= llm.await_count <= 4
        provider.discover_attractions.assert_awaited_once()
    assert '이전 코스' not in course.recommendationReason
    assert all(stop.place.latitude > 37 for stop in day.stops)


@pytest.mark.asyncio
async def test_refill_preserves_completed_other_day_and_reuses_verified_facts(visit_dependencies):
    from app.agent.course_graph import build_course_graph

    request, provider, history, llm, _, make_draft = visit_dependencies
    request.tripCondition.totalDays = 2
    first = make_draft(cores=3, days=2)
    first.days[1].candidates.pop()
    second = first.model_copy(deep=True)
    additional = make_draft('fill', cores=3).days[0].candidates[-1]
    second.days[1].candidates.append(additional)
    llm.side_effect = [first, second]
    course = (await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0}))['course']
    assert [len(day.stops) for day in course.itinerary.days] == [5, 5]
    assert {stop.place.placeName for stop in course.itinerary.days[0].stops} == {c.name for c in first.days[0].candidates}
    assert provider.search.await_count == 10
    assert llm.await_count == 2
    assert '2일차' in llm.await_args_list[1].args[2]
    ids = [stop.place.placeId for day in course.itinerary.days for stop in day.stops]
    assert len(ids) == len(set(ids))


@pytest.mark.asyncio
async def test_five_route_failure_uses_safe_four_before_three(visit_dependencies):
    from app.agent.course_graph import build_course_graph

    request, provider, history, llm, places, make_draft = visit_dependencies
    draft = make_draft(cores=3)
    impossible = places[draft.days[0].candidates[-1].name].place.placeId
    llm.return_value = draft
    async def route(left, right, **kwargs):
        return TransportToNextSchema(type='transit', distance=1500, minutes=100 if impossible in {left.place.placeId, right.place.placeId} else 10, cost=1500)
    provider.route.side_effect = route
    day = (await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0}))['course'].itinerary.days[0]
    assert len(day.stops) == 4 and len(core_ids(day)) == 2
    assert impossible not in {stop.place.placeId for stop in day.stops}
    assert '4' in day.memo
    assert 3 <= llm.await_count <= 4
    provider.discover_attractions.assert_awaited_once()
    assert all(stop.transportToNext.minutes <= 90 for stop in day.stops[:-1])


@pytest.mark.asyncio
async def test_only_three_verified_venues_remain_safe_exception_without_invention(visit_dependencies):
    from app.agent.course_graph import build_course_graph

    request, provider, history, llm, places, make_draft = visit_dependencies
    llm.return_value = make_draft(cores=1)
    day = (await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0}))['course'].itinerary.days[0]
    assert len(day.stops) == 3 and len(core_ids(day)) == 1
    assert {stop.place.placeId for stop in day.stops} == {venue.place.placeId for venue in places.values()}
    assert '3' in day.memo
    assert 3 <= llm.await_count <= 4
    provider.discover_attractions.assert_awaited_once()


def test_compact_repair_can_select_five_when_verified_pool_is_sufficient(visit_dependencies):
    from app.services.course_planning import _day_plans

    _, _, _, _, places, make_draft = visit_dependencies
    candidates = make_draft(cores=3).days[0].candidates
    available = [(candidate, places[candidate.name]) for candidate in candidates]
    plans = _day_plans(available, date(2026, 10, 5), 5, 3, set(), plan_limit=4, compact=True)
    assert plans and len(plans[0]) == 5
    assert len(plans) <= 4


@pytest.mark.asyncio
async def test_dense_five_complete_visits_do_not_trigger_llm_for_optional_sixth(visit_dependencies):
    from app.agent.course_graph import build_course_graph

    request, provider, history, llm, _, make_draft = visit_dependencies
    request.tasteProfile = profile_for_pace('dense_schedule')
    llm.return_value = make_draft(cores=3)
    day = (await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0}))['course'].itinerary.days[0]
    assert len(day.stops) == 5 and len(core_ids(day)) == 3
    llm.assert_awaited_once()
    provider.discover_attractions.assert_not_awaited()
    assert '줄' not in day.memo


@pytest.mark.asyncio
async def test_food_inflated_five_fallback_explains_core_deficit_without_fake_repeat(visit_dependencies):
    from app.agent.course_graph import build_course_graph

    request, provider, history, llm, _, make_draft = visit_dependencies
    draft = make_draft(cores=2, extras=[('none', 'coffee_shop')])
    llm.side_effect = [draft] + [TimeoutError('no additional core available')] * 3
    course = (await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0}))['course']
    day = course.itinerary.days[0]
    assert len(day.stops) == 5 and len(core_ids(day)) == 2
    assert '5곳' in day.memo or '5 곳' in day.memo
    assert not any(internal in day.memo for internal in ['최소 5', '명소 3', '목표', '축소'])
    assert '이전 코스' not in course.recommendationReason
    assert 3 <= llm.await_count <= 4
    provider.discover_attractions.assert_awaited_once()


@pytest.mark.asyncio
async def test_final_repair_returns_five_when_routes_recover_without_reduced_note(visit_dependencies):
    from app.agent.course_graph import build_course_graph

    request, provider, history, llm, _, make_draft = visit_dependencies
    draft = make_draft(cores=3)
    llm.side_effect = [draft] + [TimeoutError('final drafting outage')] * 3
    async def route(left, right, **kwargs):
        return TransportToNextSchema(type='transit', distance=1500, minutes=100 if llm.await_count == 1 else 10, cost=1500)
    provider.route.side_effect = route
    day = (await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0}))['course'].itinerary.days[0]
    assert len(day.stops) == 5 and len(core_ids(day)) == 3
    assert '줄' not in day.memo
    assert 2 <= llm.await_count <= 4
    assert provider.route.await_count <= 4 * 4 + 16 * 4


@pytest.mark.asyncio
async def test_short_verified_snapshot_survives_one_refill_timeout_then_reaches_five(visit_dependencies):
    """A transient model outage must not immediately restore an underfull day."""
    from app.agent.course_graph import build_course_graph

    request, provider, history, llm, places, make_draft = visit_dependencies
    short = make_draft(cores=2)
    filled = short.model_copy(deep=True)
    addition = make_draft('refill', cores=3).days[0].candidates[-1]
    filled.days[0].candidates.append(addition)
    llm.side_effect = [short, TimeoutError('504 DEADLINE_EXCEEDED'), filled]
    updates = [update async for update in build_course_graph(provider, history).astream({'request': request, 'attempt': 0}, stream_mode='updates')]
    course = next(update['finalize']['course'] for update in updates if 'finalize' in update)
    assert len(course.itinerary.days[0].stops) == 5
    assert len(core_ids(course.itinerary.days[0])) == 3
    assert places[addition.name].place.placeId in core_ids(course.itinerary.days[0])
    assert any('refill' in update for update in updates)
    assert llm.await_count == 3
    assert provider.search.await_count == 5
    assert all(stop.transportToNext.minutes == 10 for stop in course.itinerary.days[0].stops[:-1])


@pytest.mark.asyncio
async def test_initial_three_drafts_do_not_consume_independent_fullness_refills(visit_dependencies):
    """After failed initial route plans, a fourth call can still supply a safe core."""
    from app.agent.course_graph import build_course_graph

    request, provider, history, llm, _, make_draft = visit_dependencies
    request.tripCondition.totalDays = 2
    initial = make_draft(cores=3, days=2)
    completed = {candidate.name for candidate in initial.days[0].candidates}
    impossible = initial.days[1].candidates[-1].name
    filled = initial.model_copy(deep=True)
    replacement = make_draft('refill', cores=3).days[0].candidates[-1]
    filled.days[1].candidates = [candidate for candidate in filled.days[1].candidates if candidate.name != impossible] + [replacement]
    llm.side_effect = [initial, initial, initial, filled]

    async def route(left, right, **kwargs):
        minutes = 100 if impossible in {left.place.placeName, right.place.placeName} else 10
        if llm.await_count <= 3 and {left.place.placeName, right.place.placeName} - completed:
            minutes = 100
        return TransportToNextSchema(type='walking', distance=400, minutes=minutes, cost=0)

    provider.route.side_effect = route
    course = (await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0}, config={'recursion_limit': 60}))['course']
    assert [len(day.stops) for day in course.itinerary.days] == [5, 5]
    assert llm.await_count == 4
    assert {stop.place.placeName for stop in course.itinerary.days[0].stops} == completed
    assert all(call.kwargs['pending_days'] == [1] for call in llm.await_args_list[1:])
    assert provider.search.await_count == 11
    ids = [stop.place.placeId for day in course.itinerary.days for stop in day.stops]
    assert len(ids) == len(set(ids))
    assert all(stop.transportToNext.minutes <= 90 for day in course.itinerary.days for stop in day.stops[:-1])


@pytest.mark.asyncio
async def test_actual_nearby_attraction_completes_day_even_when_refill_llm_is_unavailable(visit_dependencies):
    """Google-supplied official place facts can fill one missing tourist slot."""
    from app.agent.course_graph import build_course_graph

    request, provider, history, llm, places, make_draft = visit_dependencies
    initial = make_draft(cores=2)
    nearby = make_draft('nearby', cores=3).days[0].candidates[-1]
    venue = places[nearby.name]
    provider.discover_attractions.return_value = [venue]
    llm.side_effect = [initial] + [TimeoutError('504 DEADLINE_EXCEEDED')] * 4
    course = (await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0}))['course']
    assert len(course.itinerary.days[0].stops) == 5
    assert len(core_ids(course.itinerary.days[0])) == 3
    assert venue.place.placeId in core_ids(course.itinerary.days[0])
    provider.discover_attractions.assert_awaited_once()
    assert provider.search.await_count == 4
    assert all(stop.transportToNext.minutes == 10 for stop in course.itinerary.days[0].stops[:-1])


@pytest.mark.asyncio
async def test_local_refill_queries_only_incomplete_day_and_respects_completed_day_ids(visit_dependencies):
    from app.agent.course_graph import build_course_graph

    request, provider, history, llm, places, make_draft = visit_dependencies
    request.tripCondition.totalDays = 2
    initial = make_draft(cores=3, days=2)
    initial.days[1].candidates.pop()
    completed_ids = {places[c.name].place.placeId for c in initial.days[0].candidates}
    completed_venue = places[initial.days[0].candidates[0].name]
    completed_venue = copy.deepcopy(completed_venue)
    completed_venue.place.placeId = completed_venue.place.placeId.removeprefix('places/')
    new = make_draft('local', cores=3).days[0].candidates[-1]
    provider.discover_attractions.return_value = [completed_venue, places[new.name]]
    llm.return_value = initial
    course = (await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0}))['course']
    assert [len(day.stops) for day in course.itinerary.days] == [5, 5]
    assert {stop.place.placeId for stop in course.itinerary.days[0].stops} == completed_ids
    provider.discover_attractions.assert_awaited_once()
    anchor = provider.discover_attractions.await_args.args[1]
    assert anchor.place.placeId not in completed_ids
    ids = [stop.place.placeId for day in course.itinerary.days for stop in day.stops]
    assert len(ids) == len(set(ids))
    assert all(call.kwargs['pending_days'] == [1] for call in llm.await_args_list[1:])
    assert provider.search.await_count == 9


@pytest.mark.asyncio
async def test_local_invalid_or_duplicate_candidates_cannot_satisfy_shortage(visit_dependencies):
    from app.agent.course_graph import build_course_graph
    from app.agent.tools.verified_maps import VerifiedPlace

    request, provider, history, llm, places, make_draft = visit_dependencies
    short = make_draft(cores=2)
    first = places[short.days[0].candidates[0].name]
    raw = first.place.model_copy(update={'placeId': 'places/new', 'placeName': 'invalid'})
    unsafe = [
        first,
        VerifiedPlace(raw.model_copy(update={'category': 'restaurant'})),
        VerifiedPlace(raw.model_copy(update={'latitude': 0.})),
        VerifiedPlace(raw.model_copy(update={'latitude': 37.7})),
        VerifiedPlace(raw.model_copy(update={'placeId': ''})),
    ]
    provider.discover_attractions.return_value = unsafe
    llm.return_value = short
    course = (await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0}))['course']
    assert len(course.itinerary.days[0].stops) == 4
    assert len(core_ids(course.itinerary.days[0])) == 2
    assert {stop.place.placeId for stop in course.itinerary.days[0].stops} == {places[c.name].place.placeId for c in short.days[0].candidates}
    provider.discover_attractions.assert_awaited_once()
    assert 3 <= llm.await_count <= 5


@pytest.mark.asyncio
async def test_underfull_refill_is_not_limited_by_novelty_only_twenty_five_seconds(visit_dependencies, mocker):
    """A retained short snapshot must not classify required fill as optional novelty."""
    from app.agent.course_graph import build_course_graph

    request, provider, history, llm, _, make_draft = visit_dependencies
    short = make_draft(cores=2)
    llm.return_value = short
    timeout_budgets = []
    real_timeout = asyncio.timeout

    def record_timeout(delay):
        timeout_budgets.append(delay)
        return real_timeout(delay)

    mocker.patch('app.agent.course_nodes.asyncio.timeout', side_effect=record_timeout)
    course = (await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0}))['course']
    assert len(course.itinerary.days[0].stops) == 4
    assert 3 <= llm.await_count <= 4
    assert 25. not in timeout_budgets
    provider.discover_attractions.assert_awaited_once()


@pytest.mark.asyncio
async def test_actual_route_refill_shortens_five_before_restoring_four_stop_snapshot(visit_dependencies):
    """Optimistic long visits can require shorter stays once real travel is known."""
    from app.agent.course_graph import build_course_graph
    from app.services.course_planning import _day_plans, _schedule_day

    request, provider, history, llm, places, make_draft = visit_dependencies
    short = make_draft(cores=2)
    long = short.model_copy(deep=True)
    additional = make_draft('long', cores=3).days[0].candidates[-1]
    long.days[0].candidates.append(additional)
    for candidate in long.days[0].candidates:
        candidate.stay_minutes = 150 if candidate.meal == 'none' else 60
    successive = []
    for duration in (150, 145, 140):
        proposal = long.model_copy(deep=True)
        for candidate in proposal.days[0].candidates:
            if candidate.meal == 'none':
                candidate.stay_minutes = duration
        successive.append(proposal)
    llm.side_effect = [short, *successive]
    actual = TransportToNextSchema(type='walking', distance=1500, minutes=60, cost=0)
    provider.route.return_value = actual
    available = [(candidate, places[candidate.name]) for candidate in long.days[0].candidates]
    optimistic = _day_plans(available, date(2026, 10, 5), 5, 3, set())
    assert optimistic, 'Fixture must have optimistic five-stop plans before actual travel validation.'
    for plan in optimistic:
        with pytest.raises(ValueError):
            _schedule_day(plan, [actual] * 4, date(2026, 10, 5), 1)
    updates = [update async for update in build_course_graph(provider, history).astream({'request': request, 'attempt': 0}, stream_mode='updates')]
    course = next(update['finalize']['course'] for update in updates if 'finalize' in update)
    assert any(len(update['verify_routes']['selected'][0]) == 4 for update in updates if 'verify_routes' in update and 'selected' in update['verify_routes'])
    assert any('refill' in update for update in updates)
    day = course.itinerary.days[0]
    assert len(day.stops) == 5 and len(core_ids(day)) == 3
    assert all(stop.stayMinutes <= 60 for stop in day.stops)
    assert all(stop.transportToNext.minutes == 60 for stop in day.stops[:-1])
    assert {stop.place.placeId for stop in day.stops} == {venue.place.placeId for _, venue in available}
    assert len({stop.place.placeId for stop in day.stops}) == 5
    assert 3 <= llm.await_count <= 4


@pytest.mark.asyncio
async def test_partial_initial_day_proposals_are_retained_when_only_missing_day_needs_refill(visit_dependencies):
    """Initial partial proposals survive both draft and independent refill budgets."""
    from app.agent.course_graph import build_course_graph
    from app.agent.course_state import PartialDraftError

    request, provider, history, llm, _, make_draft = visit_dependencies
    request.tripCondition.totalDays = 2
    full = make_draft(cores=3, days=2)
    partial = {0: full.days[0]}
    llm.side_effect = [PartialDraftError(partial, {1: TimeoutError('504')})] * 3 + [full]
    course = (await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0}))['course']
    assert [len(day.stops) for day in course.itinerary.days] == [5, 5]
    assert llm.await_count == 4
    for call in llm.await_args_list[1:]:
        assert call.kwargs['pending_days'] == [1]
        assert call.kwargs['existing_days'][0].model_dump() == full.days[0].model_dump()
    assert provider.search.await_count == 10
    assert {stop.place.placeName for stop in course.itinerary.days[0].stops} == {candidate.name for candidate in full.days[0].candidates}


@pytest.mark.asyncio
async def test_transient_attraction_discovery_retries_on_next_refill_and_keeps_five(visit_dependencies):
    """A temporary Maps outage must not permanently exhaust local supplementation."""
    from app.agent.course_graph import build_course_graph
    from app.agent.tools.verified_maps import MapsProviderError

    request, provider, history, llm, places, make_draft = visit_dependencies
    initial = make_draft(cores=2)
    nearby = make_draft('recovered', cores=3).days[0].candidates[-1]
    provider.discover_attractions.side_effect = [MapsProviderError('transient'), [places[nearby.name]]]
    llm.return_value = initial
    course = (await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0}))['course']
    assert len(course.itinerary.days[0].stops) == 5
    assert len(core_ids(course.itinerary.days[0])) == 3
    assert places[nearby.name].place.placeId in core_ids(course.itinerary.days[0])
    assert provider.discover_attractions.await_count == 2
    assert llm.await_count <= 4

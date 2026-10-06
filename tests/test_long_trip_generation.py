"""Long trips retain verified days, share a hard deadline, and never reuse a venue."""

import asyncio
from collections import Counter
from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest

from app.agent.course_graph import Candidate, CourseDraft, DraftDay
from app.schemas.course import CourseRequestSchema, PlaceSchema, TransportToNextSchema


@pytest.fixture
def trip_request():
    return CourseRequestSchema.model_validate({
        'userId': '550e8400-e29b-41d4-a716-446655440000', 'mbti': 'INTJ',
        'tripCondition': {'destinationCountry': '일본', 'destinationCity': '도쿄',
                          'startDate': '2026-10-17', 'totalDays': 5, 'budgetType': 'moderate'},
    })


def daily_draft(index):
    return CourseDraft(title='검증할 여행 후보', reason='문화와 휴식', days=[DraftDay(candidates=[
        Candidate(name=f'{index}-미술관'), Candidate(name=f'{index}-공원'),
        Candidate(name=f'{index}-역사관'), Candidate(name=f'{index}-점심', meal='lunch'),
        Candidate(name=f'{index}-저녁', meal='dinner'),
    ])])


@pytest.mark.asyncio
@pytest.mark.parametrize('total_days', [4, 5, 7])
async def test_long_draft_splits_days_with_bounded_parallelism_and_preserves_order(mocker, trip_request, total_days):
    from app.agent.course_graph import draft_candidates

    trip_request.tripCondition.totalDays = total_days
    active = peak = 0
    completed, seeds = [], []

    async def generate(request, recent_ids, feedback='', *, day_index, seed, **kwargs):
        nonlocal active, peak
        assert request.tripCondition.totalDays == total_days
        assert recent_ids == ['places/recent']
        seeds.append(seed)
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(.003 * (total_days - day_index))
            completed.append(day_index)
            return daily_draft(day_index)
        finally:
            active -= 1

    boundary = mocker.patch('app.agent.course_graph._draft_day_candidates', create=True, side_effect=generate)
    mocker.patch('app.agent.course_graph.ChatGoogleGenerativeAI', side_effect=AssertionError('Only the daily boundary may call the model'))
    result = await draft_candidates(trip_request, ['places/recent'])
    assert boundary.call_count == total_days
    assert 2 <= peak <= 6
    assert active == 0 and len(set(seeds)) == 1
    assert completed != sorted(completed)
    assert [day.candidates[0].name for day in result.days] == [f'{index}-미술관' for index in range(total_days)]


@pytest.mark.asyncio
async def test_one_day_504_retries_only_that_day_and_keeps_successful_results(mocker, trip_request):
    from google.genai.errors import APIError

    from app.agent.course_graph import draft_candidates

    calls = Counter()
    async def generate(request, recent_ids, feedback='', *, day_index, **kwargs):
        calls[day_index] += 1
        if day_index == 2 and calls[day_index] == 1:
            raise APIError(504, {'error': {'message': 'Deadline exceeded', 'status': 'DEADLINE_EXCEEDED'}})
        await asyncio.sleep(.001)
        return daily_draft(day_index)
    mocker.patch('app.agent.course_graph._draft_day_candidates', create=True, side_effect=generate)
    mocker.patch('app.agent.course_graph.ChatGoogleGenerativeAI', side_effect=AssertionError('Only the daily boundary may call the model'))
    result = await draft_candidates(trip_request, [])
    assert calls == Counter({0: 1, 1: 1, 2: 2, 3: 1, 4: 1})
    assert len(result.days) == 5


@pytest.mark.asyncio
async def test_partial_repair_does_not_request_already_verified_days(mocker, trip_request):
    from app.agent.course_graph import draft_candidates

    existing = {index: daily_draft(index).days[0] for index in (0, 1, 3, 4)}
    boundary = mocker.patch('app.agent.course_graph._draft_day_candidates', create=True, new_callable=AsyncMock, return_value=daily_draft(2))
    result = await draft_candidates(trip_request, [], pending_days=[2], existing_days=existing)
    boundary.assert_awaited_once()
    assert boundary.call_args.kwargs['day_index'] == 2
    assert [day.candidates[0].name for day in result.days] == [f'{index}-미술관' for index in range(5)]


@pytest.mark.asyncio
async def test_draft_cancellation_joins_all_started_daily_calls(mocker, trip_request):
    from app.agent.course_graph import draft_candidates

    entered, closed = set(), set()
    started = asyncio.Event()
    async def generate(request, recent_ids, feedback='', *, day_index, **kwargs):
        entered.add(day_index)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.add(day_index)
    mocker.patch('app.agent.course_graph._draft_day_candidates', create=True, side_effect=generate)
    mocker.patch('app.agent.course_graph.ChatGoogleGenerativeAI', side_effect=AssertionError('Only the daily boundary may call the model'))
    task = asyncio.create_task(draft_candidates(trip_request, []))
    try:
        await asyncio.wait_for(started.wait(), .5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert entered and entered == closed
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_daily_model_prompt_has_exact_assignment_and_whole_trip_context(mocker, trip_request):
    from langchain_core.runnables import RunnableLambda

    from app.agent import course_graph

    captured = []
    def respond(prompt):
        captured.append(prompt.to_string())
        return daily_draft(3)
    model = mocker.patch('app.agent.course_graph.ChatGoogleGenerativeAI')
    model.return_value.with_structured_output.return_value = RunnableLambda(respond)
    assert hasattr(course_graph, '_draft_day_candidates'), 'A single-day model boundary is required'
    result = await course_graph._draft_day_candidates(trip_request, ['places/avoid'], '다른 날과 장소를 겹치지 마세요', day_index=3, seed='shared-trip-seed')
    assert len(result.days) == 1 and len(captured) == 1
    prompt = captured[0]
    assert '2026-10-20' in prompt and '2026-10-17' in prompt
    assert '도쿄' in prompt and 'places/avoid' in prompt and 'shared-trip-seed' in prompt
    assert 'totalDays' in prompt and '5' in prompt
    assert model.call_args.kwargs['max_retries'] == 0
    assert model.call_args.kwargs['thinking_level'] == 'low'


@pytest.mark.asyncio
@pytest.mark.parametrize('configured', [120, 300, 45])
async def test_request_deadline_never_exceeds_ninety_seconds(mocker, trip_request, configured):
    from app.agent.course_graph import stream_course_generation

    captured = []
    async def events(state, **kwargs):
        captured.append(state)
        yield 'custom', {'step': 'GENERATING_ROUTE', 'message': '확인 중'}
    provider = mocker.MagicMock()
    provider.__aenter__ = AsyncMock(return_value=provider)
    provider.__aexit__ = AsyncMock(return_value=False)
    mocker.patch('app.agent.course_graph.VerifiedMapsProvider', return_value=provider)
    mocker.patch('app.agent.course_graph.CourseHistory')
    mocker.patch('app.agent.course_graph.build_course_graph').return_value.astream.side_effect = events
    mocker.patch('app.agent.course_graph.settings.COURSE_TIMEOUT_SECONDS', configured)
    before = asyncio.get_running_loop().time()
    assert [event async for event in stream_course_generation(trip_request)]
    assert 0 < captured[0]['deadline'] - before <= min(90, configured) + .1


def graph_with_days(mocker, tmp_path, trip_request, *, aliases=False, alternatives=False):
    from app.agent.tools.verified_maps import Destination, VerifiedPlace
    from app.services.course_history import CourseHistory

    trip_request.tripCondition.totalDays = 2
    first = daily_draft(0).days[0]
    second = daily_draft(1).days[0]
    extra = daily_draft(2).days[0]
    places = {}
    for index, day in enumerate([first, second, extra]):
        for slot, candidate in enumerate(day.candidates):
            identifier = f'places/{slot}' if index < 2 else f'places/alternate-{slot}'
            if aliases and index == 1:
                identifier = identifier.removeprefix('places/')
            places[candidate.name] = VerifiedPlace(PlaceSchema(placeId=identifier, placeName=candidate.name, category='restaurant' if candidate.meal != 'none' else 'museum', address='東京都千代田区', latitude=35.68 + slot * .001, longitude=139.76))
    if alternatives:
        first = DraftDay(candidates=first.candidates + extra.candidates)
    mocker.patch('app.agent.course_graph.draft_candidates', new_callable=AsyncMock, return_value=CourseDraft(title='도쿄', reason='문화 여행', days=[first, second]))
    provider = mocker.Mock()
    provider.resolve_destination = AsyncMock(return_value=Destination(country_code='JP', south=35, north=36, west=139, east=140))
    provider.search = AsyncMock(side_effect=lambda candidate, destination: places[candidate.name])
    provider.discover_meals = AsyncMock(return_value=[])
    provider.photo = AsyncMock(return_value=None)
    provider.route = AsyncMock(return_value=TransportToNextSchema(type='walking', distance=500, minutes=10, cost=0, memo='검증된 보행 경로'))
    history = CourseHistory(path=tmp_path / 'history.sqlite3')
    return provider, history, first, second, extra, places


@pytest.mark.asyncio
async def test_same_verified_id_with_resource_prefix_cannot_appear_on_two_days(mocker, tmp_path, trip_request):
    from app.agent.course_graph import CourseGenerationError, build_course_graph
    from app.services.course_history import history_key

    provider, history, *_ = graph_with_days(mocker, tmp_path, trip_request, aliases=True)
    updates = []
    with pytest.raises(CourseGenerationError):
        async for update in build_course_graph(provider, history).astream({'request': trip_request, 'attempt': 0}, stream_mode='updates'):
            updates.append(update)
    verified = next(update['verify_places'] for update in updates if 'verify_places' in update)
    assert all(len(verified['candidate_pools'][index]) == 5 for index in range(2))
    assert await history.recent(history_key(trip_request)) == []


@pytest.mark.asyncio
async def test_overlapping_day_pools_choose_disjoint_alternative_before_redrafting(mocker, tmp_path, trip_request):
    from app.agent.course_graph import build_course_graph

    provider, history, first, second, extra, places = graph_with_days(mocker, tmp_path, trip_request, alternatives=True)
    def plans(available, day_date, *args, **kwargs):
        choices = [first.candidates[:5], extra.candidates] if day_date == date(2026, 10, 17) else [second.candidates]
        return [[(candidate, places[candidate.name]) for candidate in choice] for choice in choices]
    mocker.patch('app.agent.course_graph._day_plans', side_effect=plans)
    result = await build_course_graph(provider, history).ainvoke({'request': trip_request, 'attempt': 0})
    days = result['course'].itinerary.days
    ids = [stop.place.placeId.removeprefix('places/') for day in days for stop in day.stops]
    assert len(ids) == len(set(ids)) == 10
    assert [day.date for day in days] == [(date(2026, 10, 17) + timedelta(days=index)).isoformat() for index in range(2)]
    assert all('alternate-' in stop.place.placeId for stop in days[0].stops)


@pytest.mark.asyncio
async def test_distinct_days_verify_routes_concurrently_and_keep_calendar_order(mocker, tmp_path, trip_request):
    from app.agent.course_graph import build_course_graph
    from app.agent.tools.verified_maps import Destination, VerifiedPlace
    from app.services.course_history import CourseHistory

    trip_request.tripCondition.totalDays = 5
    draft = CourseDraft(title='도쿄 문화 여행', reason='문화와 휴식', days=[daily_draft(index).days[0] for index in range(5)])
    mocker.patch('app.agent.course_graph.draft_candidates', new_callable=AsyncMock, return_value=draft)
    provider = mocker.Mock()
    provider.resolve_destination = AsyncMock(return_value=Destination(country_code='JP', south=35, north=36, west=139, east=140))
    places = {candidate.name: VerifiedPlace(PlaceSchema(placeId=f'places/{candidate.name}', placeName=candidate.name, category='restaurant' if candidate.meal != 'none' else 'museum', address='東京都千代田区', latitude=35.68 + slot * .001, longitude=139.76)) for day in draft.days for slot, candidate in enumerate(day.candidates)}
    provider.search = AsyncMock(side_effect=lambda candidate, destination: places[candidate.name])
    provider.discover_meals = AsyncMock(return_value=[])
    provider.photo = AsyncMock(return_value=None)
    active = peak = 0
    active_days = set()
    async def route(left, right, **kwargs):
        nonlocal active, peak
        index = int(left.place.placeName[0])
        assert right.place.placeName.startswith(str(index))
        assert index not in active_days, 'Route departure time depends on the previous edge within a day'
        active_days.add(index)
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(.001 * (5 - index))
            return TransportToNextSchema(type='walking', distance=500, minutes=10, cost=0, memo='검증된 보행 경로')
        finally:
            active -= 1
            active_days.remove(index)
    provider.route = AsyncMock(side_effect=route)
    result = await build_course_graph(provider, CourseHistory(path=tmp_path / 'history.sqlite3')).ainvoke({'request': trip_request, 'attempt': 0})
    days = result['course'].itinerary.days
    assert 2 <= peak <= 6 and active == 0
    assert [day.day for day in days] == list(range(1, 6))
    assert [day.date for day in days] == [(date(2026, 10, 17) + timedelta(days=index)).isoformat() for index in range(5)]
    assert all(len(day.stops) >= 5 for day in days)
    assert all(stop.place.placeName.startswith(str(index)) for index, day in enumerate(days) for stop in day.stops)


@pytest.mark.asyncio
async def test_daily_metadata_cannot_replace_whole_trip_title_after_repair(mocker, trip_request):
    from app.agent.course_graph import draft_candidates

    async def generate(request, recent_ids, feedback='', *, day_index, **kwargs):
        draft = daily_draft(day_index)
        draft.title = f'미확인 권역 {day_index}의 숨겨진 명소'
        draft.reason = f'{day_index}일차의 모델 추측에 기반한 설명'
        draft.tags = [f'미확인-{day_index}']
        return draft

    mocker.patch('app.agent.course_graph._draft_day_candidates', create=True, side_effect=generate)
    original = await draft_candidates(trip_request, [])
    existing = {index: day for index, day in enumerate(original.days) if index != 3}
    repaired = await draft_candidates(trip_request, [], pending_days=[3], existing_days=existing)
    assert original.title == repaired.title
    assert trip_request.tripCondition.destinationCity in repaired.title
    assert str(trip_request.tripCondition.totalDays) in repaired.title
    assert '미확인' not in repaired.title + repaired.reason + ' '.join(repaired.tags)

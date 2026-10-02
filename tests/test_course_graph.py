"""Exercise the actual LangGraph with offline LLM and Maps boundaries."""

import asyncio
from datetime import date
from itertools import pairwise
from unittest.mock import AsyncMock

import pytest

from app.schemas.course import CourseRequestSchema, PlaceSchema, TransportToNextSchema


@pytest.fixture
def request_data():
    return CourseRequestSchema.model_validate({
        'userId': '550e8400-e29b-41d4-a716-446655440000',
        'mbti': 'ENFP',
        'tripCondition': {
            'destinationCountry': '대한민국', 'destinationCity': '서울',
            'startDate': '2026-10-05', 'totalDays': 1, 'budgetType': 'moderate',
        },
    })


@pytest.fixture
def graph_dependencies(mocker, tmp_path):
    from app.agent.course_graph import Candidate, CourseDraft, DraftDay
    from app.agent.tools.verified_maps import Destination, VerifiedPlace
    from app.services.course_history import CourseHistory

    candidates = [
        Candidate(name='미술관', english_name='Museum', stay_minutes=60, reason='문화 체험'),
        Candidate(name='점심식당', english_name='Lunch', meal='lunch', stay_minutes=60, cost=15000, reason='현지 음식'),
        Candidate(name='공원', english_name='Park', stay_minutes=60, reason='휴식'),
        Candidate(name='저녁식당', english_name='Dinner', meal='dinner', stay_minutes=60, cost=20000, reason='미식'),
    ]
    draft = CourseDraft(title='서울 문화 미식', reason='문화 체험과 미식 여행', days=[DraftDay(candidates=candidates)])
    llm = mocker.patch('app.agent.course_graph.draft_candidates', new_callable=AsyncMock, return_value=draft)
    provider = mocker.Mock()
    provider.resolve_destination = AsyncMock(return_value=Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3))
    places = {
        candidate.name: VerifiedPlace(place=PlaceSchema(
            placeId=f'places/verified-{index}', placeName=candidate.name,
            placeEngName=candidate.english_name, category='restaurant' if candidate.meal != 'none' else 'attraction',
            address='대한민국 서울', latitude=37.55 + index * .001, longitude=126.98,
        ), periods=None)
        for index, candidate in enumerate(candidates)
    }
    provider.search = AsyncMock(side_effect=lambda candidate, destination: places[candidate.name])
    provider.discover_meals = AsyncMock(return_value=[])
    provider.route = AsyncMock(return_value=TransportToNextSchema(type='walking', distance=700, minutes=12, cost=0, memo='검증된 도보 경로'))
    history = CourseHistory(path=tmp_path / 'history.sqlite3')
    return provider, history, llm, draft, places


@pytest.mark.asyncio
async def test_actual_graph_verifies_and_schedules_before_complete(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph

    provider, history, llm, _, places = graph_dependencies
    graph = build_course_graph(provider, history)
    updates = [update async for update in graph.astream({'request': request_data, 'attempt': 0}, stream_mode='updates')]
    names = [name for update in updates for name in update]
    assert names[:6] == ['prepare', 'draft', 'verify_places', 'verify_routes', 'schedule', 'finalize']
    result = updates[-1]['finalize']['course']
    assert result.totalDays == 1
    assert result.startDate == request_data.tripCondition.startDate
    assert result.destinationCity == '서울'
    stops = result.itinerary.days[0].stops
    assert {stop.place.placeId for stop in stops} == {p.place.placeId for p in places.values()}
    assert [stop.sequence for stop in stops] == list(range(1, len(stops) + 1))
    assert result.itinerary.days[0].date == date(2026, 10, 5).isoformat()
    for current, following in pairwise(stops):
        h, m = map(int, current.arrivalTime.split(':'))
        next_h, next_m = map(int, following.arrivalTime.split(':'))
        assert next_h * 60 + next_m >= h * 60 + m + current.stayMinutes + 12
        assert current.transportToNext.minutes == 12
    assert stops[-1].transportToNext.type == 'none'
    assert provider.search.await_count == 4
    assert provider.route.await_count == 3
    llm.assert_awaited_once()
    assert llm.call_args.args[0].mbti == 'ENFP'


@pytest.mark.asyncio
async def test_missing_verified_route_cannot_produce_course(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph

    provider, history, llm, _, _ = graph_dependencies
    provider.route.side_effect = ValueError('No verified route')
    with pytest.raises(ValueError):
        await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    assert llm.await_count <= 2


@pytest.mark.asyncio
async def test_closed_all_day_places_cannot_produce_course(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph
    from app.agent.tools.verified_maps import VerifiedPlace

    provider, history, llm, _, places = graph_dependencies
    # An explicit empty periods list means closed, whereas None means unavailable.
    provider.search.side_effect = lambda candidate, destination: VerifiedPlace(place=places[candidate.name].place, periods=[])
    with pytest.raises(ValueError):
        await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    assert llm.await_count <= 2


@pytest.mark.asyncio
async def test_repeat_history_tries_variation_then_discloses_limited_choices(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph

    provider, history, llm, _, _ = graph_dependencies
    graph = build_course_graph(provider, history)
    await graph.ainvoke({'request': request_data, 'attempt': 0})
    repeated = (await graph.ainvoke({'request': request_data, 'attempt': 0}))['course']
    assert '중복' in repeated.recommendationReason or '이전' in repeated.recommendationReason
    assert llm.await_count <= 3
    assert any(call.args[1] for call in llm.await_args_list[1:])


@pytest.mark.asyncio
async def test_places_are_looked_up_concurrently(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph

    provider, history, _, _, places = graph_dependencies
    in_flight = peak = 0

    async def lookup(candidate, destination):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(.01)
        in_flight -= 1
        return places[candidate.name]

    provider.search.side_effect = lookup
    await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    assert 1 < peak <= 8


@pytest.mark.asyncio
async def test_history_persists_and_isolated_by_user_and_destination(tmp_path, request_data):
    from app.services.course_history import CourseHistory, history_key

    path = tmp_path / 'history.sqlite3'
    history = CourseHistory(path=path)
    key = history_key(request_data)
    assert str(request_data.userId) not in key
    assert await history.record_if_novel(key, {'places/a', 'places/b'})
    reopened = CourseHistory(path=path)
    assert not await reopened.record_if_novel(key, {'places/b', 'places/a'})
    assert await reopened.recent(key) == [{'places/a', 'places/b'}]
    other = request_data.model_copy(deep=True)
    from uuid import UUID
    other.userId = UUID('550e8400-e29b-41d4-a716-446655440099')
    assert await reopened.recent(history_key(other)) == []
    other = request_data.model_copy(deep=True)
    other.tripCondition.destinationCity = '부산'
    assert await reopened.recent(history_key(other)) == []


@pytest.mark.asyncio
async def test_concurrent_history_claim_accepts_only_one_duplicate(tmp_path):
    from app.services.course_history import CourseHistory

    history = CourseHistory(path=tmp_path / 'history.sqlite3')
    claims = await asyncio.gather(*[history.record_if_novel('same-user', {'places/a', 'places/b'}) for _ in range(4)])
    assert sum(claims) == 1


@pytest.mark.parametrize(('periods', 'day', 'earliest', 'duration', 'latest', 'expected'), [
    ([{'open': {'day': 1, 'hour': 10}, 'close': {'day': 1, 'hour': 18}}], date(2026, 10, 5), 9 * 60, 60, 20 * 60, 10 * 60),
    ([{'open': {'day': 1, 'hour': 10}, 'close': {'day': 1, 'hour': 18}}], date(2026, 10, 5), 17 * 60 + 30, 60, 20 * 60, None),
    ([{'open': {'day': 6, 'hour': 22}, 'close': {'day': 0, 'hour': 2}}], date(2026, 10, 4), 30, 60, 3 * 60, 30),
    ([{'open': {'day': 0, 'hour': 0, 'minute': 0}}], date(2026, 10, 5), 9 * 60, 60, 20 * 60, 9 * 60),
    ([{'open': {'day': 1, 'hour': 24}, 'close': {'day': 1, 'hour': 25}}], date(2026, 10, 5), 9 * 60, 60, 20 * 60, None),
    ([{'open': {'day': 1, 'hour': 9}}], date(2026, 10, 5), 9 * 60, 60, 20 * 60, None),
    (None, date(2026, 10, 5), 19 * 60 + 30, 60, 20 * 60, None),
])
def test_opening_hours_fit_whole_visit(periods, day, earliest, duration, latest, expected):
    """Weekly boundaries, closing time and malformed hours constrain the full visit."""
    from app.agent.course_graph import _opening_start

    assert _opening_start(periods, day, earliest, duration, latest) == expected


@pytest.mark.asyncio
async def test_generation_stream_closes_graph_iterator(request_data, graph_dependencies, mocker):
    """Closing the public generator must immediately close its nested graph stream."""
    from app.agent.course_graph import stream_course_generation

    provider, history, _, _, _ = graph_dependencies
    provider.__aenter__ = AsyncMock(return_value=provider)
    provider.__aexit__ = AsyncMock(return_value=False)
    closed = asyncio.Event()

    async def graph_events(*args, **kwargs):
        try:
            yield 'custom', {'step': 'GENERATING_ROUTE', 'message': '시작'}
            await asyncio.Event().wait()
        finally:
            closed.set()

    graph = mocker.Mock()
    graph.astream.side_effect = graph_events
    mocker.patch('app.agent.course_graph.VerifiedMapsProvider', return_value=provider)
    mocker.patch('app.agent.course_graph.CourseHistory', return_value=history)
    mocker.patch('app.agent.course_graph.build_course_graph', return_value=graph)
    stream = stream_course_generation(request_data)
    assert (await anext(stream))[0] == 'progress'
    await stream.aclose()
    assert closed.is_set()
    provider.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_breakfast_is_scheduled_when_verified(request_data, graph_dependencies):
    from app.agent.course_graph import Candidate, build_course_graph
    from app.agent.tools.verified_maps import VerifiedPlace

    provider, history, _, draft, places = graph_dependencies
    breakfast = Candidate(name='아침식당', english_name='Breakfast', meal='breakfast', stay_minutes=45)
    draft.days[0].candidates.insert(0, breakfast)
    places[breakfast.name] = VerifiedPlace(PlaceSchema(placeId='places/breakfast', placeName=breakfast.name, category='restaurant', address='대한민국 서울', latitude=37.55, longitude=126.98))
    state = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    first = state['course'].itinerary.days[0].stops[0]
    assert first.place.placeName == '아침식당'
    hour, minute = map(int, first.arrivalTime.split(':'))
    assert 9 * 60 <= hour * 60 + minute
    assert hour * 60 + minute + first.stayMinutes <= 11 * 60


@pytest.mark.asyncio
async def test_two_days_use_correct_weekday_and_separate_routes(request_data, graph_dependencies):
    from app.agent.course_graph import DraftDay, build_course_graph
    from app.agent.tools.verified_maps import VerifiedPlace

    provider, history, _, draft, places = graph_dependencies
    request_data.tripCondition.totalDays = 2
    next_candidates = []
    for candidate in draft.days[0].candidates:
        second = candidate.model_copy(update={'name': candidate.name + '2'})
        next_candidates.append(second)
        original = places[candidate.name]
        places[candidate.name] = VerifiedPlace(original.place, periods=[{'open': {'day': 1, 'hour': 9}, 'close': {'day': 1, 'hour': 21}}])
        places[second.name] = VerifiedPlace(original.place.model_copy(update={'placeId': original.place.placeId + '-day2', 'placeName': second.name}), periods=[{'open': {'day': 2, 'hour': 9}, 'close': {'day': 2, 'hour': 21}}])
    draft.days.append(DraftDay(candidates=next_candidates))
    state = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    days = state['course'].itinerary.days
    assert [(day.day, day.date) for day in days] == [(1, '2026-10-05'), (2, '2026-10-06')]
    assert all(len(day.stops) == 4 for day in days)
    assert all(day.stops[-1].transportToNext.type == 'none' for day in days)
    assert provider.route.await_count == 6
    for call in provider.route.await_args_list:
        origin, destination = call.args[:2]
        assert origin.place.placeId.endswith('-day2') == destination.place.placeId.endswith('-day2')


@pytest.mark.asyncio
async def test_balanced_day_rejects_only_meals(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph

    provider, history, _, draft, _ = graph_dependencies
    draft.days[0].candidates = [candidate for candidate in draft.days[0].candidates if candidate.meal != 'none']
    with pytest.raises(ValueError):
        await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})


def preference_profile(**scores):
    """Create a low-preference baseline with explicitly chosen high-score evidence."""
    from app.schemas.taste_profile import (
        ActivityPreferenceSchema,
        FoodPreferenceSchema,
        PreferredLocationTypeSchema,
        TasteProfileSchema,
        TravelPurposeSchema,
    )

    payload = {
        key: dict.fromkeys(model.model_fields, 1)
        for key, model in {
            'travelPurpose': TravelPurposeSchema,
            'activityPreference': ActivityPreferenceSchema,
            'preferredLocationType': PreferredLocationTypeSchema,
            'foodPreference': FoodPreferenceSchema,
        }.items()
    }
    payload.update(travelPaceDensity='balanced', spendingTendency='moderate', companionType='friends')
    for path, value in scores.items():
        section, field = path.split('__')
        payload[section][field] = value
    return TasteProfileSchema.model_validate(payload)


@pytest.mark.asyncio
async def test_same_museum_reason_changes_with_actual_high_preferences(request_data, graph_dependencies):
    from uuid import UUID

    from app.agent.course_graph import build_course_graph

    provider, history, llm, _, places = graph_dependencies
    places['미술관'].place.category = 'museum'
    request_data.tasteProfile = preference_profile(travelPurpose__culturalExperience=5)
    first = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    request_data.userId = UUID('550e8400-e29b-41d4-a716-446655440099')
    request_data.tasteProfile = preference_profile(activityPreference__viewing=5)
    second = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    culture_reason = first['course'].itinerary.days[0].stops[0].reason
    viewing_reason = second['course'].itinerary.days[0].stops[0].reason
    assert '문화' in culture_reason
    assert '관람' in viewing_reason
    assert culture_reason != viewing_reason
    assert llm.await_count == 2  # No extra LLM call for explanation generation.


@pytest.mark.asyncio
@pytest.mark.parametrize('low_score', [1, 2, 3])
async def test_low_preference_scores_do_not_become_positive_reasons(request_data, graph_dependencies, low_score):
    from app.agent.course_graph import build_course_graph

    provider, history, _, _, places = graph_dependencies
    places['미술관'].place.category = 'museum'
    request_data.tasteProfile = preference_profile(travelPurpose__culturalExperience=low_score)
    result = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    reason = result['course'].itinerary.days[0].stops[0].reason
    assert '문화' not in reason
    assert '선호' not in reason
    assert reason.strip()


@pytest.mark.asyncio
async def test_reasons_use_provider_category_and_discard_untrusted_draft_prose(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph

    provider, history, _, draft, places = graph_dependencies
    request_data.tasteProfile = preference_profile(travelPurpose__culturalExperience=5, preferredLocationType__beachResort=5, foodPreference__dietaryRestriction=5)
    places['미술관'].place.category = 'museum'
    draft.days[0].candidates[0].category = 'beach'
    unsafe = '숨은 명소이며 알레르기 안전을 보장하는 유명한 사진 명소'
    for candidate in draft.days[0].candidates:
        candidate.reason = unsafe
    draft.reason = unsafe
    result = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    course = result['course']
    assert '문화' in course.itinerary.days[0].stops[0].reason
    for text in [course.recommendationReason, *[stop.reason for stop in course.itinerary.days[0].stops]]:
        assert unsafe not in text
        assert not any(claim in text for claim in ['알레르기', '숨은 명소', '사진 명소', '해변'])


@pytest.mark.asyncio
async def test_explanations_reflect_final_opening_time_and_visit_duration(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph
    from app.agent.tools.verified_maps import VerifiedPlace

    provider, history, _, draft, places = graph_dependencies
    request_data.tasteProfile = preference_profile(travelPurpose__culturalExperience=4)
    original = places['미술관'].place
    original.category = 'museum'
    places['미술관'] = VerifiedPlace(original, periods=[{'open': {'day': 1, 'hour': 10}, 'close': {'day': 1, 'hour': 18}}])
    draft.days[0].candidates[0].stay_minutes = 75
    result = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    stop = result['course'].itinerary.days[0].stops[0]
    assert stop.arrivalTime == '10:00' and stop.stayMinutes == 75
    assert '문화' in stop.reason
    assert '10:00' in stop.reason and '75분' in stop.reason
    assert '09:00' not in stop.reason and '60분' not in stop.reason


@pytest.mark.asyncio
async def test_course_summary_only_uses_selected_verified_places(request_data, graph_dependencies):
    from app.agent.course_graph import Candidate, build_course_graph

    provider, history, _, draft, places = graph_dependencies
    request_data.tasteProfile = preference_profile(travelPurpose__culturalExperience=5, activityPreference__nightlife=5)
    places['미술관'].place.category = 'museum'
    draft.days[0].candidates.append(Candidate(name='탈락한 나이트클럽', category='night_club', reason='밤문화 선호를 위한 장소'))
    draft.reason = '탈락한 나이트클럽에서 밤문화를 즐기는 코스'
    result = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    summary = result['course'].recommendationReason
    assert '탈락한' not in summary and '밤문화' not in summary
    assert '문화' in summary


@pytest.mark.asyncio
async def test_mbti_only_reason_uses_honest_schedule_fallback(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph

    provider, history, _, draft, _ = graph_dependencies
    draft.reason = 'ENFP이므로 외향적이고 모험적인 사용자'
    for candidate in draft.days[0].candidates:
        candidate.reason = draft.reason
    result = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    course = result['course']
    for stop in course.itinerary.days[0].stops:
        assert stop.reason.strip()
        assert stop.place.placeName in stop.reason
        assert not any(claim in stop.reason for claim in ['외향', '모험', '선호', 'ENFP'])
    assert '외향' not in course.recommendationReason


@pytest.mark.asyncio
async def test_repeated_supported_preferences_vary_explanation_wording(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph

    provider, history, _, _, places = graph_dependencies
    request_data.tasteProfile = preference_profile(travelPurpose__culturalExperience=5)
    places['미술관'].place.category = 'museum'
    places['공원'].place.category = 'museum'
    result = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    cultural_stops = [stop for stop in result['course'].itinerary.days[0].stops if stop.place.category == 'museum']
    clauses = []
    for stop in cultural_stops:
        reason = stop.reason
        assert '문화' in reason
        for value in [stop.place.placeName, stop.arrivalTime, str(stop.stayMinutes)]:
            reason = reason.replace(value, '')
        clauses.append(reason)
    assert len(set(clauses)) == len(cultural_stops)


@pytest.mark.asyncio
async def test_unverifiable_high_preferences_do_not_invent_venue_properties(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph

    provider, history, _, _, places = graph_dependencies
    places['미술관'].place.category = 'museum'
    places['공원'].place.category = 'park'
    request_data.tasteProfile = preference_profile(
        activityPreference__photographyVideo=5,
        preferredLocationType__hiddenSpotPreferred=5,
        preferredLocationType__famousSpotPreferred=5,
        foodPreference__dietaryRestriction=5,
        foodPreference__localFoodActive=5,
    )
    result = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    texts = [result['course'].recommendationReason, *[stop.reason for stop in result['course'].itinerary.days[0].stops]]
    for text in texts:
        assert not any(claim in text for claim in ['선호', '알레르기', '숨은', '유명', '사진', '현지 음식'])


def three_day_dinner_conflict(request_data, graph_dependencies, alternate_closes=21):
    """Reproduce a second-draft dinner conflict after first-draft lunch failure."""
    from app.agent.course_graph import Candidate, DraftDay
    from app.agent.tools.verified_maps import VerifiedPlace

    _, _, llm, draft, places = graph_dependencies
    request_data.tripCondition.totalDays = 3
    original_candidates = list(draft.days[0].candidates)
    for day_number in (2, 3):
        candidates = []
        for original in original_candidates:
            candidate = original.model_copy(update={'name': f'{original.name}-{day_number}'})
            candidates.append(candidate)
            original_place = places[original.name].place
            places[candidate.name] = VerifiedPlace(original_place.model_copy(update={
                'placeId': f'{original_place.placeId}-day{day_number}', 'placeName': candidate.name,
            }))
        draft.days.append(DraftDay(candidates=candidates))
    for name, opening, closing in [('공원-2', 17, 18), ('저녁식당-2', 17, 18)]:
        places[name] = VerifiedPlace(places[name].place, periods=[{
            'open': {'day': 2, 'hour': opening, 'minute': 0 if name.startswith('공원') else 30},
            'close': {'day': 2, 'hour': closing, 'minute': 0 if name.startswith('공원') else 30},
        }])
    alternate = Candidate(name='대안저녁-2', english_name='Alternative Dinner', meal='dinner', stay_minutes=60)
    draft.days[1].candidates.append(alternate)
    places[alternate.name] = VerifiedPlace(
        places['저녁식당-2'].place.model_copy(update={'placeId': 'places/alternative-dinner-day2', 'placeName': alternate.name}),
        periods=[{'open': {'day': 2, 'hour': 17, 'minute': 30}, 'close': {'day': 2, 'hour': alternate_closes, 'minute': 30 if alternate_closes == 18 else 0}}],
    )
    initial = draft.model_copy(deep=True)
    initial.days[1].candidates[1].name = '찾을 수 없는 점심식당'
    llm.side_effect = [initial, draft, draft]
    return draft


@pytest.mark.asyncio
async def test_three_day_dinner_conflict_uses_verified_alternative_after_draft_repair(request_data, graph_dependencies):
    """A late dinner conflict can recover locally even after the LLM repair is spent."""
    from app.agent.course_graph import build_course_graph

    provider, history, llm, _, _ = graph_dependencies
    three_day_dinner_conflict(request_data, graph_dependencies)
    result = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    course = result['course']
    assert len(course.itinerary.days) == 3
    assert [stop.place.placeName for stop in course.itinerary.days[0].stops] == ['미술관', '점심식당', '공원', '저녁식당']
    assert [stop.place.placeName for stop in course.itinerary.days[2].stops] == ['미술관-3', '점심식당-3', '공원-3', '저녁식당-3']
    dinner = course.itinerary.days[1].stops[-1]
    assert dinner.place.placeName == '대안저녁-2'
    assert dinner.transportToNext.type == 'none'
    hour, minute = map(int, dinner.arrivalTime.split(':'))
    assert 17 * 60 + 30 <= hour * 60 + minute
    assert hour * 60 + minute + dinner.stayMinutes <= 20 * 60
    assert llm.await_count == 2
    assert '저녁식당-2' not in dinner.reason
    # New adjacency must be verified after replacement, not reuse the old edge.
    assert any(call.args[1].place.placeName == '대안저녁-2' for call in provider.route.await_args_list)


@pytest.mark.asyncio
async def test_three_day_impossible_dinner_still_fails_without_history_or_unbounded_llm(request_data, graph_dependencies):
    """Alternatives must never bypass full visit opening or meal windows."""
    from app.agent.course_graph import CourseGenerationError, build_course_graph
    from app.agent.tools.verified_maps import VerifiedPlace
    from app.services.course_history import history_key

    provider, history, llm, _, places = graph_dependencies
    three_day_dinner_conflict(request_data, graph_dependencies, alternate_closes=18)
    # Every verified restaurant on the failed day closes before a full dinner
    # can fit, including venues whose lunch role can legitimately be reassigned.
    for name in ('점심식당-2', '저녁식당-2', '대안저녁-2'):
        places[name] = VerifiedPlace(places[name].place, periods=[{
            'open': {'day': 2, 'hour': 9}, 'close': {'day': 2, 'hour': 18},
        }])
    with pytest.raises(CourseGenerationError):
        await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    assert llm.await_count <= 3
    assert await history.recent(history_key(request_data)) == []


@pytest.mark.asyncio
async def test_verified_dinner_alternative_recovers_without_another_llm_call(request_data, graph_dependencies):
    """Known meal alternatives should be tried before requesting a whole new draft."""
    from app.agent.course_graph import build_course_graph

    provider, history, llm, _, _ = graph_dependencies
    three_day_dinner_conflict(request_data, graph_dependencies)
    llm.side_effect = None  # Start directly with the complete candidate pool.
    result = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    assert result['course'].itinerary.days[1].stops[-1].place.placeName == '대안저녁-2'
    assert llm.await_count == 1
    route_keys = [(call.args[0].place.placeId, call.args[1].place.placeId, call.kwargs.get('mode'), call.kwargs.get('departure_time')) for call in provider.route.await_args_list]
    assert len(route_keys) == len(set(route_keys))
    assert provider.route.await_count <= 4 * 3 * 5  # Bounded plans/day and current pace cap.


@pytest.mark.asyncio
async def test_unreachable_first_dinner_tries_verified_meal_alternative(request_data, graph_dependencies):
    from app.agent.course_graph import Candidate, build_course_graph
    from app.agent.tools.verified_maps import VerifiedPlace

    provider, history, llm, draft, places = graph_dependencies
    alternate = Candidate(name='대안식당', meal='dinner', stay_minutes=60)
    draft.days[0].candidates.append(alternate)
    places[alternate.name] = VerifiedPlace(places['저녁식당'].place.model_copy(update={'placeId': 'places/alternative-dinner', 'placeName': alternate.name}))

    async def route(origin, destination, mode='walking', departure_time=None):
        if destination.place.placeName == '저녁식당':
            raise ValueError('Provider cannot verify route to first restaurant')
        return TransportToNextSchema(type='walking', distance=700, minutes=12, cost=0)

    provider.route.side_effect = route
    result = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    day = result['course'].itinerary.days[0]
    assert day.stops[-1].place.placeName == '대안식당'
    assert llm.await_count == 1
    assert len(day.stops) == 4
    assert {stop.place.placeName for stop in day.stops} >= {'점심식당', '미술관', '공원'}
    keys = [(call.args[0].place.placeId, call.args[1].place.placeId, call.kwargs.get('mode'), call.kwargs.get('departure_time')) for call in provider.route.await_args_list]
    assert len(keys) == len(set(keys))
    assert provider.route.await_count <= 4 * 5


@pytest.mark.asyncio
async def test_three_day_lunch_shortage_gets_bounded_third_draft(request_data, graph_dependencies):
    """Repeated multi-day candidate shortage may use one final bounded draft."""
    from app.agent.course_graph import build_course_graph
    from app.services.course_history import history_key

    provider, history, llm, _, _ = graph_dependencies
    valid_draft = three_day_dinner_conflict(request_data, graph_dependencies)
    missing_lunch = valid_draft.model_copy(deep=True)
    missing_lunch.days[1].candidates[1].name = '찾을 수 없는 점심식당'
    llm.side_effect = [missing_lunch, missing_lunch, valid_draft]
    result = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    course = result['course']
    assert llm.await_count == 3
    assert course.totalDays == 3
    assert [(day.day, day.date) for day in course.itinerary.days] == [(1, '2026-10-05'), (2, '2026-10-06'), (3, '2026-10-07')]
    assert all(len(day.stops) >= 4 for day in course.itinerary.days)
    assert len(await history.recent(history_key(request_data))) == 1
    for call in llm.await_args_list[1:]:
        feedback = call.args[2] if len(call.args) > 2 else call.kwargs['feedback']
        assert '찾을 수 없는 점심식당' in feedback


@pytest.mark.asyncio
async def test_breakfast_only_venue_cannot_fill_missing_lunch_slot(request_data, graph_dependencies):
    """Unknown lunch service cannot be inferred from breakfast venue hours alone."""
    from app.agent.course_graph import (
        Candidate,
        CourseGenerationError,
        build_course_graph,
    )
    from app.agent.tools.verified_maps import VerifiedPlace

    provider, history, llm, draft, places = graph_dependencies
    draft.days[0].candidates = [candidate for candidate in draft.days[0].candidates if candidate.meal != 'lunch']
    breakfast = Candidate(name='조식전문점', meal='breakfast', stay_minutes=60)
    draft.days[0].candidates.insert(0, breakfast)
    places[breakfast.name] = VerifiedPlace(places['점심식당'].place.model_copy(update={'placeId': 'places/breakfast-only', 'placeName': breakfast.name, 'category': 'breakfast_restaurant'}))
    with pytest.raises(CourseGenerationError):
        await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    assert llm.await_count <= 2


@pytest.mark.asyncio
@pytest.mark.parametrize('original_meal', ['none', 'lunch'])
async def test_market_cannot_supply_required_lunch_by_label_or_remapping(request_data, graph_dependencies, original_meal):
    """A market is a valid visit type, but does not establish a served meal venue."""
    from app.agent.course_graph import (
        Candidate,
        CourseGenerationError,
        build_course_graph,
    )
    from app.agent.tools.verified_maps import VerifiedPlace

    provider, history, llm, draft, places = graph_dependencies
    draft.days[0].candidates = [candidate for candidate in draft.days[0].candidates if candidate.meal != 'lunch']
    market = Candidate(name='방문시장', meal=original_meal, stay_minutes=60)
    draft.days[0].candidates.insert(1, market)
    places[market.name] = VerifiedPlace(places['점심식당'].place.model_copy(update={'placeId': 'places/market-visit', 'placeName': market.name, 'category': 'market'}))
    with pytest.raises(CourseGenerationError):
        await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    assert llm.await_count <= 2


@pytest.mark.asyncio
async def test_failed_dinner_lookup_does_not_poison_same_venue_lunch_on_repair(request_data, graph_dependencies):
    """Place-validation caching must distinguish the meal role being validated."""
    from app.agent.course_graph import build_course_graph
    from app.agent.tools.verified_maps import VerifiedPlace

    provider, history, llm, draft, places = graph_dependencies
    first = draft.model_copy(deep=True)
    first.days[0].candidates[-1].name = '브런치식당'
    first.days[0].candidates[-1].english_name = 'Brunch Venue'
    repaired = draft.model_copy(deep=True)
    repaired.days[0].candidates[1].name = '브런치식당'
    repaired.days[0].candidates[1].english_name = 'Brunch Venue'
    brunch = VerifiedPlace(places['점심식당'].place.model_copy(update={'placeId': 'places/brunch', 'placeName': '브런치식당', 'category': 'brunch_restaurant'}))

    async def role_sensitive_search(candidate, destination):
        if candidate.name == '브런치식당':
            if candidate.meal == 'dinner':
                raise ValueError('Brunch venue cannot establish dinner service')
            assert candidate.meal == 'lunch'
            return brunch
        return places[candidate.name]

    provider.search.side_effect = role_sensitive_search
    llm.side_effect = [first, repaired]
    result = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    assert llm.await_count == 2
    roles = [call.args[0].meal for call in provider.search.await_args_list if call.args[0].name == '브런치식당']
    assert roles == ['dinner', 'lunch']
    assert any(stop.place.placeId == 'places/brunch' for stop in result['course'].itinerary.days[0].stops)


@pytest.mark.asyncio
async def test_complete_waits_for_graph_natural_exhaustion_and_cleanup(request_data, graph_dependencies, mocker):
    """Consumers may close on complete, so LangGraph must already have ended normally."""
    from app.agent.course_graph import build_course_graph, stream_course_generation

    provider, history, _, _, _ = graph_dependencies
    course = (await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0}))['course']
    provider.__aenter__ = AsyncMock(return_value=provider)
    provider.__aexit__ = AsyncMock(return_value=False)
    exhausted = asyncio.Event()
    cleaned = asyncio.Event()
    cancelled = asyncio.Event()

    async def graph_events(*args, **kwargs):
        try:
            yield 'custom', {'step': 'GENERATING_ROUTE', 'message': '최종 검증 중'}
            yield 'updates', {'finalize': {'course': course}}
            await asyncio.sleep(0)
            exhausted.set()
        except (asyncio.CancelledError, GeneratorExit):
            cancelled.set()
            raise
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    graph = mocker.Mock()
    graph.astream.side_effect = graph_events
    mocker.patch('app.agent.course_graph.VerifiedMapsProvider', return_value=provider)
    mocker.patch('app.agent.course_graph.CourseHistory', return_value=history)
    mocker.patch('app.agent.course_graph.build_course_graph', return_value=graph)
    stream = stream_course_generation(request_data)
    try:
        assert (await anext(stream))[0] == 'progress'
        event, payload = await anext(stream)
        assert event == 'complete'
        assert payload['course'] == course.model_dump()
        assert exhausted.is_set()
        assert cleaned.is_set()
        provider.__aexit__.assert_awaited_once()
    finally:
        await stream.aclose()
    assert not cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize('terminal_failure', ['error', 'timeout'])
async def test_graph_failure_after_finalize_update_does_not_emit_complete(request_data, graph_dependencies, mocker, terminal_failure):
    """A finalize update alone is not proof that the graph run finished successfully."""
    from app.agent.course_graph import build_course_graph, stream_course_generation

    provider, history, _, _, _ = graph_dependencies
    course = (await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0}))['course']
    provider.__aenter__ = AsyncMock(return_value=provider)
    provider.__aexit__ = AsyncMock(return_value=False)

    async def graph_events(*args, **kwargs):
        yield 'custom', {'step': 'GENERATING_ROUTE', 'message': '검증 중'}
        yield 'updates', {'finalize': {'course': course}}
        if terminal_failure == 'timeout':
            await asyncio.Event().wait()
        raise RuntimeError('Graph failed before reaching normal termination')

    graph = mocker.Mock()
    graph.astream.side_effect = graph_events
    mocker.patch('app.agent.course_graph.VerifiedMapsProvider', return_value=provider)
    mocker.patch('app.agent.course_graph.CourseHistory', return_value=history)
    mocker.patch('app.agent.course_graph.build_course_graph', return_value=graph)
    events = []
    mocker.patch('app.core.config.settings.COURSE_TIMEOUT_SECONDS', .03)
    expected = TimeoutError if terminal_failure == 'timeout' else RuntimeError
    with pytest.raises(expected):
        async for event in stream_course_generation(request_data):
            events.append(event)
    assert [event[0] for event in events] == ['progress']


@pytest.mark.asyncio
async def test_actual_graph_root_callback_finishes_before_complete(request_data, graph_dependencies, mocker):
    """Observe real LangGraph root completion locally without sending tracing data."""
    from langchain_core.callbacks import AsyncCallbackHandler

    from app.agent.course_graph import build_course_graph, stream_course_generation

    normal_roots = []
    error_roots = []

    class RootTerminationObserver(AsyncCallbackHandler):
        async def on_chain_end(self, outputs, *, run_id, parent_run_id=None, **kwargs):
            if parent_run_id is None:
                normal_roots.append(run_id)

        async def on_chain_error(self, error, *, run_id, parent_run_id=None, **kwargs):
            if parent_run_id is None:
                error_roots.append(error)

    provider, history, _, _, _ = graph_dependencies
    provider.__aenter__ = AsyncMock(return_value=provider)
    provider.__aexit__ = AsyncMock(return_value=False)
    graph = build_course_graph(provider, history).with_config({'callbacks': [RootTerminationObserver()]})
    mocker.patch('app.agent.course_graph.VerifiedMapsProvider', return_value=provider)
    mocker.patch('app.agent.course_graph.CourseHistory', return_value=history)
    mocker.patch('app.agent.course_graph.build_course_graph', return_value=graph)
    stream = stream_course_generation(request_data)
    completed = False
    try:
        async for event, _ in stream:
            if event == 'complete':
                assert len(normal_roots) == 1
                assert error_roots == []
                completed = True
                break
    finally:
        await stream.aclose()
    assert completed
    assert len(normal_roots) == 1
    assert error_roots == []


def candidate_pool_repair_fixture(request_data, graph_dependencies, include_second_attraction=True):
    """Build three days where only the failed day's candidate types complement on retry."""
    from app.agent.course_graph import DraftDay
    from app.agent.tools.verified_maps import VerifiedPlace

    _, _, llm, draft, places = graph_dependencies
    request_data.tripCondition.totalDays = 3
    original = list(draft.days[0].candidates)
    for number in (2, 3):
        candidates = []
        for candidate in original:
            cloned = candidate.model_copy(update={'name': f'{candidate.name}-pool-{number}'})
            candidates.append(cloned)
            places[cloned.name] = VerifiedPlace(places[candidate.name].place.model_copy(update={
                'placeId': f'{places[candidate.name].place.placeId}-pool-{number}',
                'placeName': cloned.name,
            }))
        draft.days.append(DraftDay(candidates=candidates))
    first = draft.model_copy(deep=True)
    first.days[2].candidates[1].name = '검증불가 점심'
    first.days[2].candidates[3].name = '검증불가 저녁'
    if not include_second_attraction:
        first.days[2].candidates[2].name = '검증불가 명소'
    repaired = draft.model_copy(deep=True)
    repaired.days[2].candidates = [candidate for candidate in repaired.days[2].candidates if candidate.meal != 'none']
    llm.side_effect = [first, repaired, repaired]
    return first, repaired


@pytest.mark.asyncio
async def test_failed_day_reuses_verified_attractions_when_retry_only_proposes_meals(request_data, graph_dependencies):
    """A retry must add verified options rather than erase the failed day's useful ones."""
    from app.agent.course_graph import build_course_graph

    provider, history, llm, _, _ = graph_dependencies
    first, _ = candidate_pool_repair_fixture(request_data, graph_dependencies)
    result = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    days = result['course'].itinerary.days
    assert llm.await_count == 2
    assert len(days) == 3
    for index in (0, 1):
        assert [stop.place.placeName for stop in days[index].stops] == [candidate.name for candidate in first.days[index].candidates]
    assert {stop.place.placeName for stop in days[2].stops} == {'미술관-pool-3', '공원-pool-3', '점심식당-pool-3', '저녁식당-pool-3'}
    assert not any('검증불가' in stop.place.placeName for day in days for stop in day.stops)
    assert all(day.stops[-1].transportToNext.type == 'none' for day in days)


@pytest.mark.asyncio
async def test_candidate_pool_reduces_optional_visits_without_inventing_missing_attraction(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph
    from app.services.course_history import history_key

    provider, history, llm, _, _ = graph_dependencies
    candidate_pool_repair_fixture(request_data, graph_dependencies, include_second_attraction=False)
    course = (await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0}))['course']
    assert llm.await_count <= 3
    last = course.itinerary.days[2]
    assert len(last.stops) == 3
    assert {stop.place.placeName for stop in last.stops} == {'미술관-pool-3', '점심식당-pool-3', '저녁식당-pool-3'}
    assert '여유' in last.memo
    assert await history.recent(history_key(request_data))


@pytest.mark.asyncio
async def test_accumulated_pool_respects_saved_day_ids_and_duplicate_meal_roles(request_data, graph_dependencies):
    from app.agent.course_graph import Candidate, build_course_graph
    from app.agent.tools.verified_maps import VerifiedPlace

    provider, history, llm, _, places = graph_dependencies
    first, repaired = candidate_pool_repair_fixture(request_data, graph_dependencies)
    # A retry proposes a venue already used by a successful day under a new name.
    reserved_alias = Candidate(name='이전날명소 별칭', stay_minutes=60)
    places[reserved_alias.name] = VerifiedPlace(places['미술관'].place)
    repaired.days[2].candidates.append(reserved_alias)
    # The same physical restaurant appears with two different meal roles.
    repeated_dinner = repaired.days[2].candidates[0].model_copy(update={'meal': 'dinner'})
    repaired.days[2].candidates.append(repeated_dinner)
    llm.side_effect = [first, repaired, repaired]
    result = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    days = result['course'].itinerary.days
    all_ids = [stop.place.placeId for day in days for stop in day.stops]
    assert len(all_ids) == len(set(all_ids))
    assert len(days[2].stops) >= 4
    assert places['미술관'].place.placeId not in {stop.place.placeId for stop in days[2].stops}
    assert {'미술관-pool-3', '공원-pool-3'} <= {stop.place.placeName for stop in days[2].stops}
    assert llm.await_count == 2


@pytest.mark.asyncio
async def test_verified_pool_combines_complementary_candidates_across_three_drafts(request_data, graph_dependencies):
    from app.agent.course_graph import Candidate, build_course_graph

    provider, history, llm, _, _ = graph_dependencies
    first, repaired = candidate_pool_repair_fixture(request_data, graph_dependencies)
    second = repaired.model_copy(deep=True)
    second.days[2].candidates = [second.days[2].candidates[0], Candidate(name='검증불가 둘째초안')]
    third = repaired.model_copy(deep=True)
    third.days[2].candidates = [third.days[2].candidates[1], Candidate(name='검증불가 셋째초안')]
    llm.side_effect = [first, second, third]
    result = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    assert llm.await_count == 3
    final_names = {stop.place.placeName for stop in result['course'].itinerary.days[2].stops}
    assert final_names == {'미술관-pool-3', '공원-pool-3', '점심식당-pool-3', '저녁식당-pool-3'}


@pytest.mark.asyncio
async def test_latest_stay_proposal_replaces_same_verified_place_and_role_in_pool(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph

    provider, history, llm, _, _ = graph_dependencies
    first, repaired = candidate_pool_repair_fixture(request_data, graph_dependencies)
    first.days[2].candidates[0].stay_minutes = 90
    updated_visit = first.days[2].candidates[0].model_copy(update={'stay_minutes': 30})
    repaired.days[2].candidates.insert(0, updated_visit)
    llm.side_effect = [first, repaired, repaired]
    result = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    museum = next(stop for stop in result['course'].itinerary.days[2].stops if stop.place.placeName == updated_visit.name)
    assert museum.stayMinutes == 30
    assert llm.await_count == 2
    queries = [call for call in provider.search.await_args_list if call.args[0].name == updated_visit.name]
    assert len(queries) == 1  # Reuse verified facts while replacing proposal metadata.


def test_failed_plan_exclusion_happens_before_four_plan_shortlist(graph_dependencies):
    """Previously failed choices must not hide still-untried feasible permutations."""
    from app.agent.course_graph import _day_plans

    _, _, _, draft, places = graph_dependencies
    available = [(candidate, places[candidate.name]) for candidate in draft.days[0].candidates]
    initial = _day_plans(available, date(2026, 10, 5), 6, 2, set())
    assert len(initial) == 4
    rejected = {tuple((venue.place.placeId, candidate.meal, candidate.stay_minutes) for candidate, venue in plan) for plan in initial}
    alternatives = _day_plans(available, date(2026, 10, 5), 6, 2, set(), rejected_plans=rejected)
    assert 1 <= len(alternatives) <= 4
    for plan in alternatives:
        signature = tuple((venue.place.placeId, candidate.meal, candidate.stay_minutes) for candidate, venue in plan)
        assert signature not in rejected
        assert len({venue.place.placeId for _, venue in plan}) == len(plan)
        assert {'lunch', 'dinner'} <= {candidate.meal for candidate, _ in plan}
        assert sum(candidate.meal == 'none' for candidate, _ in plan) >= 2


def test_repaired_stay_duration_is_not_blocked_by_old_failed_plan(graph_dependencies):
    """A genuinely changed visit duration can make a previously failed order feasible."""
    from app.agent.course_graph import _day_plans

    _, _, _, draft, places = graph_dependencies
    available = [(candidate, places[candidate.name]) for candidate in draft.days[0].candidates]
    original = _day_plans(available, date(2026, 10, 5), 6, 2, set())[0]
    rejected = {tuple((venue.place.placeId, candidate.meal, candidate.stay_minutes) for candidate, venue in original)}
    repaired = [(candidate.model_copy(update={'stay_minutes': 30}) if candidate.name == '미술관' else candidate, venue) for candidate, venue in original]
    alternatives = _day_plans(repaired, date(2026, 10, 5), 6, 2, set(), rejected_plans=rejected)
    assert alternatives
    assert any(
        [venue.place.placeId for _, venue in plan] == [venue.place.placeId for _, venue in original]
        and next(candidate.stay_minutes for candidate, _ in plan if candidate.name == '미술관') == 30
        for plan in alternatives
    )


@pytest.mark.asyncio
async def test_korean_routes_use_transit_and_trip_departure_time(request_data, graph_dependencies):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.agent.course_graph import build_course_graph

    provider, history, _, _, _ = graph_dependencies
    result = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    stops = result['course'].itinerary.days[0].stops
    assert provider.route.await_count == len(stops) - 1
    for call, stop in zip(provider.route.await_args_list, stops):
        assert call.kwargs['mode'] == 'transit'
        hour, minute = map(int, stop.arrivalTime.split(':'))
        expected = datetime(2026, 10, 5, hour, minute, tzinfo=ZoneInfo('Asia/Seoul'))
        from datetime import timedelta
        expected += timedelta(minutes=stop.stayMinutes)
        assert call.kwargs['departure_time'] == expected


@pytest.mark.asyncio
async def test_missing_route_with_nearby_verified_places_returns_explicit_estimate(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph
    from app.agent.tools.verified_maps import NoRouteError

    provider, history, llm, _, places = graph_dependencies
    provider.route.side_effect = NoRouteError()
    course = (await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0}))['course']
    assert {stop.place.placeId for stop in course.itinerary.days[0].stops} == {p.place.placeId for p in places.values()}
    for stop in course.itinerary.days[0].stops[:-1]:
        assert stop.transportToNext.type == 'walking'
        assert stop.transportToNext.distance is None
        assert 5 <= stop.transportToNext.minutes <= 35
        assert '[추정 도보]' in stop.transportToNext.memo
        assert '확인된 약' not in stop.reason
    llm.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['unauthorized', 'invalid'])
async def test_provider_permission_or_malformed_route_never_becomes_estimate(request_data, graph_dependencies, kind):
    from app.agent.course_graph import build_course_graph
    from app.agent.tools.verified_maps import MapsProviderError
    from app.services.course_history import history_key

    provider, history, _, _, _ = graph_dependencies
    provider.route.side_effect = MapsProviderError(kind=kind, status_code=403 if kind == 'unauthorized' else 400)
    with pytest.raises(ValueError):
        await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    assert await history.recent(history_key(request_data)) == []


@pytest.mark.asyncio
async def test_distant_missing_routes_cannot_be_invented_as_short_walks(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph
    from app.agent.tools.verified_maps import NoRouteError, VerifiedPlace
    from app.services.course_history import history_key

    provider, history, llm, _, places = graph_dependencies
    for index, (name, place) in enumerate(list(places.items())):
        places[name] = VerifiedPlace(place.place.model_copy(update={'latitude': 37.40 + index * .08}))
    provider.route.side_effect = NoRouteError()
    with pytest.raises(ValueError):
        await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    assert llm.await_count <= 2
    assert await history.recent(history_key(request_data)) == []


@pytest.mark.asyncio
async def test_transient_place_failure_is_retried_without_negative_cache(request_data, graph_dependencies):
    from collections import Counter

    from app.agent.course_graph import build_course_graph
    from app.agent.tools.verified_maps import MapsProviderError

    provider, history, llm, _, places = graph_dependencies
    queries = Counter()

    async def search(candidate, destination):
        queries[candidate.name] += 1
        if candidate.name == '점심식당' and queries[candidate.name] == 1:
            raise MapsProviderError(kind='transient', status_code=503)
        return places[candidate.name]

    provider.search.side_effect = search
    course = (await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0}))['course']
    assert len(course.itinerary.days[0].stops) == 4
    assert queries['점심식당'] == 2
    assert queries['미술관'] == queries['공원'] == queries['저녁식당'] == 1
    assert llm.await_count <= 2


@pytest.mark.asyncio
async def test_temporary_llm_error_retries_inside_existing_attempt_budget(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph

    provider, history, llm, draft, _ = graph_dependencies
    llm.side_effect = [TimeoutError('temporary model timeout'), draft]
    course = (await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0}))['course']
    assert len(course.itinerary.days[0].stops) == 4
    assert llm.await_count == 2


@pytest.mark.asyncio
async def test_failed_final_llm_repair_keeps_verified_pool_for_lighter_day(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph

    provider, history, llm, _, _ = graph_dependencies
    first, repaired = candidate_pool_repair_fixture(request_data, graph_dependencies, include_second_attraction=False)
    llm.side_effect = [first, repaired, TimeoutError('final model timeout')]
    result = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    days = result['course'].itinerary.days
    assert llm.await_count == 3
    assert len(days) == 3
    assert len(days[2].stops) == 3
    assert {stop.place.placeName for stop in days[2].stops} == {'미술관-pool-3', '점심식당-pool-3', '저녁식당-pool-3'}
    assert '여유' in days[2].memo
    assert [stop.place.placeName for stop in days[0].stops] == [candidate.name for candidate in first.days[0].candidates]


@pytest.mark.asyncio
async def test_history_write_failure_keeps_valid_course_with_notice(request_data, graph_dependencies, mocker):
    import sqlite3

    from app.agent.course_graph import build_course_graph

    provider, history, _, _, _ = graph_dependencies
    mocker.patch.object(history, 'record_if_novel', side_effect=sqlite3.OperationalError('database is locked'))
    course = (await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0}))['course']
    assert len(course.itinerary.days[0].stops) == 4
    assert '이력' in course.recommendationReason


@pytest.mark.asyncio
async def test_schema_parse_failure_is_repaired_without_unbounded_llm(request_data, graph_dependencies):
    from langchain_core.exceptions import OutputParserException

    from app.agent.course_graph import build_course_graph

    provider, history, llm, draft, _ = graph_dependencies
    llm.side_effect = [OutputParserException('malformed course'), draft]
    assert (await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0}))['course']
    assert llm.await_count == 2


@pytest.mark.asyncio
async def test_cancelled_llm_propagates_without_retry_or_history(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph
    from app.services.course_history import history_key

    provider, history, llm, _, _ = graph_dependencies
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def blocking_draft(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    llm.side_effect = blocking_draft
    task = asyncio.create_task(build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0}))
    try:
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert cleaned.is_set()
    llm.assert_awaited_once()
    assert await history.recent(history_key(request_data)) == []


@pytest.mark.asyncio
async def test_repair_requires_real_attraction_in_addition_to_real_meals(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph
    from app.services.course_history import history_key

    provider, history, llm, draft, _ = graph_dependencies
    draft.days[0].candidates = [candidate for candidate in draft.days[0].candidates if candidate.meal != 'none']
    with pytest.raises(ValueError):
        await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    assert llm.await_count <= 2
    assert await history.recent(history_key(request_data)) == []


@pytest.mark.asyncio
async def test_estimated_course_serializes_under_current_api_and_passes_output_validator(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph
    from app.agent.tools.verified_maps import NoRouteError
    from scripts.verify_course_output import validate_output

    provider, history, _, _, _ = graph_dependencies
    provider.route.side_effect = NoRouteError()
    course = (await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0}))['course']
    output = {'completed': True, 'events': [{'event': 'progress', 'data': {'step': 'GENERATING_ROUTE', 'message': '검증 중'}}, {'event': 'complete', 'data': {}}], 'course': course.model_dump()}
    report = validate_output(request_data.model_dump(), output)
    assert report['passed'], report['errors']
    assert any('추정' in warning for warning in report['warnings'])


@pytest.mark.asyncio
async def test_history_read_failure_keeps_valid_course_with_limited_protection_notice(request_data, graph_dependencies, mocker):
    from app.agent.course_graph import build_course_graph

    provider, history, _, _, _ = graph_dependencies
    mocker.patch.object(history, 'recent', side_effect=PermissionError('private path unavailable'))
    course = (await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0}))['course']
    assert len(course.itinerary.days[0].stops) == 4
    assert '이력' in course.recommendationReason
    assert 'private path' not in course.recommendationReason


@pytest.mark.asyncio
async def test_model_authentication_failure_is_not_retried(request_data, graph_dependencies):
    from google.genai.errors import APIError

    from app.agent.course_graph import build_course_graph

    provider, history, llm, _, _ = graph_dependencies
    llm.side_effect = APIError(403, {'error': {'message': 'permission denied', 'status': 'PERMISSION_DENIED'}})
    with pytest.raises(APIError):
        await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    llm.assert_awaited_once()
    provider.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_nearby_route_transient_exhaustion_can_use_disclosed_formula_estimate(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph
    from app.agent.tools.verified_maps import MapsProviderError

    provider, history, llm, _, _ = graph_dependencies
    provider.route.side_effect = MapsProviderError(kind='transient', status_code=503)
    course = (await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0}))['course']
    for stop in course.itinerary.days[0].stops[:-1]:
        assert stop.transportToNext.distance is None
        assert '[추정 도보]' in stop.transportToNext.memo
        assert 5 <= stop.transportToNext.minutes <= 35
    llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_same_temporary_route_failure_is_deduplicated_per_stage_and_retried_next_draft(request_data, graph_dependencies):
    from collections import Counter

    from app.agent.course_graph import Candidate, build_course_graph
    from app.agent.tools.verified_maps import MapsProviderError, VerifiedPlace

    provider, history, llm, draft, places = graph_dependencies
    for name in ('조각공원', '정원'):
        draft.days[0].candidates.append(Candidate(name=name, stay_minutes=60))
        places[name] = VerifiedPlace(places['미술관'].place.model_copy(update={'placeId': f'places/{name}', 'placeName': name}))
    for index, (name, venue) in enumerate(list(places.items())):
        places[name] = VerifiedPlace(venue.place.model_copy(update={'latitude': 37.4 + index * .04}))
    queried = []

    async def route(origin, destination, mode='walking', departure_time=None):
        stage = llm.await_count
        queried.append((stage, origin.place.placeId, destination.place.placeId, mode, departure_time))
        if stage == 1:
            raise MapsProviderError(kind='transient', status_code=503)
        return TransportToNextSchema(type=mode, distance=5000, minutes=12, cost=0)

    provider.route.side_effect = route
    course = (await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0}))['course']
    assert len(course.itinerary.days[0].stops) >= 3
    assert llm.await_count == 2
    first_stage = [entry[1:] for entry in queried if entry[0] == 1]
    second_stage = [entry[1:] for entry in queried if entry[0] == 2]
    assert first_stage and second_stage
    assert max(Counter(first_stage).values()) == 1
    assert set(first_stage) & set(second_stage)  # A new draft may recover the failed edge.


@pytest.mark.asyncio
async def test_missing_named_dinner_discovers_actual_nearby_meal_venue_before_llm_retry(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph
    from app.agent.tools.verified_maps import VerifiedPlace

    provider, history, llm, _, places = graph_dependencies
    official = VerifiedPlace(places['저녁식당'].place.model_copy(update={
        'placeId': 'places/official-nearby-restaurant', 'placeName': '실제 검색된 식당',
    }))

    async def search(candidate, destination):
        if candidate.name == '저녁식당':
            raise ValueError('Proposed branch does not match actual provider name')
        return places[candidate.name]

    provider.search.side_effect = search
    provider.discover_meals.return_value = [official]
    course = (await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0}))['course']
    stops = course.itinerary.days[0].stops
    assert len(stops) == 4
    assert official.place.placeId in {stop.place.placeId for stop in stops}
    assert '저녁식당' not in {stop.place.placeName for stop in stops}
    assert {stop.place.placeName for stop in stops} >= {'점심식당', '미술관', '공원', '실제 검색된 식당'}
    discovered_stop = next(stop for stop in stops if stop.place.placeId == official.place.placeId)
    assert discovered_stop.stayMinutes == 45
    assert discovered_stop.cost == 17500
    assert '계획용 추정치' in discovered_stop.memo
    provider.discover_meals.assert_awaited_once()
    llm.assert_awaited_once()
    # Discovery returns actual provider facts directly rather than making another
    # identity query using a generated business name.
    assert not any(call.args[0].name == official.place.placeName for call in provider.search.await_args_list)


@pytest.mark.asyncio
async def test_complete_meal_pool_does_not_issue_extra_discovery_requests(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph

    provider, history, _, _, _ = graph_dependencies
    await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    provider.discover_meals.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_meal_discovery_is_bounded_to_one_request_per_day(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph

    provider, history, llm, draft, _ = graph_dependencies
    draft.days[0].candidates = [candidate for candidate in draft.days[0].candidates if candidate.meal != 'dinner']
    with pytest.raises(ValueError):
        await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    assert llm.await_count <= 2
    provider.discover_meals.assert_awaited_once()


@pytest.mark.asyncio
async def test_meal_discovery_does_not_change_already_validated_days(request_data, graph_dependencies):
    from app.agent.course_graph import build_course_graph

    provider, history, llm, _, _ = graph_dependencies
    first, _ = candidate_pool_repair_fixture(request_data, graph_dependencies)
    course = (await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0}))['course']
    assert llm.await_count == 2
    for index in (0, 1):
        assert [stop.place.placeName for stop in course.itinerary.days[index].stops] == [candidate.name for candidate in first.days[index].candidates]
    provider.discover_meals.assert_awaited_once()
    assert provider.discover_meals.call_args.args[1].place.placeName.endswith('-pool-3')


@pytest.mark.asyncio
@pytest.mark.parametrize('deficiency', ['lunch_only_venues', 'closed_venue'])
async def test_meal_discovery_uses_actual_meal_roles_and_visit_day_hours(request_data, graph_dependencies, deficiency):
    from app.agent.course_graph import build_course_graph
    from app.agent.tools.verified_maps import VerifiedPlace

    provider, history, llm, draft, places = graph_dependencies
    official = VerifiedPlace(places['저녁식당'].place.model_copy(update={'placeId': 'places/available-official-dinner', 'placeName': '영업 중인 실제 식당'}))
    if deficiency == 'lunch_only_venues':
        for candidate in draft.days[0].candidates:
            if candidate.meal != 'none':
                candidate.meal = 'lunch'
                places[candidate.name] = VerifiedPlace(places[candidate.name].place.model_copy(update={'category': 'brunch_restaurant'}))
    else:
        places['저녁식당'] = VerifiedPlace(places['저녁식당'].place, periods=[])
    provider.discover_meals.return_value = [official]
    course = (await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0}))['course']
    assert len(course.itinerary.days[0].stops) == 4
    assert official.place.placeId in {stop.place.placeId for stop in course.itinerary.days[0].stops}
    provider.discover_meals.assert_awaited_once()
    llm.assert_awaited_once()
    assert course.itinerary.days[0].stops[-1].place.category == 'restaurant'

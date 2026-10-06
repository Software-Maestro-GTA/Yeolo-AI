"""Exercise minimum visit completeness and bounded safety fallback without paid APIs."""

import asyncio
from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.schemas.course import CourseRequestSchema, PlaceSchema, TransportToNextSchema


@pytest.fixture
def visit_dependencies(mocker, tmp_path):
    from app.agent.course_graph import Candidate, CourseDraft, DraftDay
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
    provider.photo = AsyncMock(return_value=None)
    llm = mocker.patch('app.agent.course_graph.draft_candidates', new_callable=AsyncMock)
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
        llm.side_effect = [first, TimeoutError('bounded refill failed')]
    elif failure == 'unsafe':
        for candidate in second.days[0].candidates:
            places[candidate.name] = VerifiedPlace(places[candidate.name].place.model_copy(update={'latitude': 0.}))
        llm.side_effect = [first, second]
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
    assert course.title == first.title
    assert '4' in day.memo and ('이동' in day.memo or '영업' in day.memo)
    if failure == 'deadline':
        assert llm.await_count <= 2
    else:
        assert llm.await_count == 2
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
    assert llm.await_count <= 2
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
    assert llm.await_count <= 2


def test_compact_repair_can_select_five_when_verified_pool_is_sufficient(visit_dependencies):
    from app.agent.course_graph import _day_plans

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
    assert '줄' not in day.memo


@pytest.mark.asyncio
async def test_food_inflated_five_fallback_explains_core_deficit_without_fake_repeat(visit_dependencies):
    from app.agent.course_graph import build_course_graph

    request, provider, history, llm, _, make_draft = visit_dependencies
    draft = make_draft(cores=2, extras=[('none', 'coffee_shop')])
    llm.side_effect = [draft, TimeoutError('no additional core available')]
    course = (await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0}))['course']
    day = course.itinerary.days[0]
    assert len(day.stops) == 5 and len(core_ids(day)) == 2
    assert '5곳' in day.memo or '5 곳' in day.memo
    assert not any(internal in day.memo for internal in ['최소 5', '명소 3', '목표', '축소'])
    assert '이전 코스' not in course.recommendationReason
    assert llm.await_count == 2


@pytest.mark.asyncio
async def test_final_repair_returns_five_when_routes_recover_without_reduced_note(visit_dependencies):
    from app.agent.course_graph import build_course_graph

    request, provider, history, llm, _, make_draft = visit_dependencies
    draft = make_draft(cores=3)
    llm.side_effect = [draft, TimeoutError('final drafting outage')]
    async def route(left, right, **kwargs):
        return TransportToNextSchema(type='transit', distance=1500, minutes=100 if llm.await_count == 1 else 10, cost=1500)
    provider.route.side_effect = route
    day = (await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0}))['course'].itinerary.days[0]
    assert len(day.stops) == 5 and len(core_ids(day)) == 3
    assert '줄' not in day.memo
    assert llm.await_count == 2
    assert provider.route.await_count <= 4 * 4 + 12 * 4

"""Verify repeat diversity with real selection, history and graph; paid APIs stay offline."""

import asyncio
import json
import sqlite3
import time
from datetime import date
from itertools import combinations
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.schemas.course import CourseRequestSchema, PlaceSchema, TransportToNextSchema


def profile(ids, attractions=None, areas=(), experiences=()):
    """Build internal history from IDs and model planned text, never provider content."""
    return {'place_ids': list(ids), 'attraction_ids': list(attractions or ()), 'areas': list(areas), 'experiences': list(experiences)}


@pytest.mark.parametrize(('current', 'attractions', 'recent', 'expected'), [
    ({'a', 'b', 'c', 'd'}, {'a', 'b'}, [profile({'a', 'b', 'c', 'd'}, {'a', 'b'})], (1, 1)),
    ({'a', 'b', 'c', 'x'}, {'a', 'b'}, [profile({'a', 'b', 'c', 'd'}, {'a', 'b'})], (.75, 1)),
    ({'a', 'b', 'x', 'y'}, {'a', 'b'}, [profile({'a', 'b', 'c', 'd'}, {'a', 'b'})], (.5, 1)),
    ({'w', 'x', 'c', 'd'}, {'w', 'x'}, [profile({'a', 'b', 'c', 'd'}, {'a', 'b'})], (.5, 0)),
    ({'a', 'b'}, {'a'}, [profile({'a', 'b', 'c', 'd'}, {'a', 'z'})], (1, 1)),
    ({'a', 'b', 'c', 'd'}, {'a', 'b'}, [profile({'a', 'x', 'y', 'z'}), profile({'b', 'w', 'v', 'u'})], (.25, 0)),
    (set(), set(), [profile(set()), {'place_ids': ['old']}], (0, 0)),
])
def test_overlap_detects_restaurant_only_swaps_and_uses_max_individual_history(current, attractions, recent, expected):
    from app.services.course_diversity import overlap_scores

    result = overlap_scores(current, attractions, recent)
    assert (result['place_overlap'], result['attraction_overlap']) == expected


def test_area_normalization_and_candidate_metadata_remain_internal():
    from app.agent.course_graph import Candidate
    from app.services.course_diversity import normalize_area

    assert normalize_area('  Ｓｅｏｕｌ   Forest ') == 'seoul forest'
    candidate = Candidate(name='기존 초안')
    assert candidate.planned_area == '' and candidate.experiences == []
    with pytest.raises(ValidationError):
        Candidate(name='메타데이터', experiences=['unsupported_experience'])
    assert 'planned_area' not in PlaceSchema.model_fields


@pytest.mark.asyncio
async def test_history_reads_legacy_database_and_new_metadata_without_map_content(tmp_path):
    from app.services.course_history import CourseHistory

    path = tmp_path / 'history.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE courses (user_key TEXT NOT NULL, signature TEXT NOT NULL, ids TEXT NOT NULL, created REAL NOT NULL, PRIMARY KEY(user_key, signature))')
        db.execute('INSERT INTO courses VALUES (?, ?, ?, ?)', ('user', 'legacy', '["old"]', time.time()))
    history = CourseHistory(path)
    legacy = (await history.recent_profiles('user'))[0]
    assert set(legacy['place_ids']) == {'old'}
    assert not legacy.get('attraction_ids')
    metadata = {'attraction_ids': ['new'], 'areas': ['  Ｓｅｏｕｌ   Forest '], 'experiences': ['nature'], 'address': 'MUST NOT PERSIST', 'latitude': 37.55}
    assert await history.record_if_novel('user', {'new', 'lunch'}, metadata=metadata, max_overlap=.4)
    entries = await CourseHistory(path).recent_profiles('user')
    entry = next(item for item in entries if 'new' in item['place_ids'])
    assert set(entry['attraction_ids']) == {'new'}
    assert entry['areas'] == ['seoul forest']
    assert entry['experiences'] == ['nature']
    with sqlite3.connect(path) as db:
        dump = '\n'.join(db.iterdump())
    assert 'MUST NOT PERSIST' not in dump and 'latitude' not in dump
    assert await history.recent('user') == [set(item['place_ids']) for item in entries]


@pytest.mark.asyncio
async def test_history_partial_claim_is_atomic_and_old_exact_api_still_works(tmp_path):
    from app.services.course_history import CourseHistory

    history = CourseHistory(tmp_path / 'history.sqlite3')
    claims = await asyncio.gather(*[
        history.record_if_novel('same', {'a', 'b', f'meal-{i}'}, metadata={'attraction_ids': ['a', 'b'], 'areas': ['central']}, max_overlap=.4)
        for i in range(4)
    ])
    assert sum(claims) == 1
    assert len(await history.recent_profiles('same')) == 1
    assert await history.record_if_novel('legacy', {'one', 'two'})
    assert not await history.record_if_novel('legacy', {'two', 'one'})


@pytest.mark.asyncio
async def test_history_ttl_and_per_user_pruning_remove_metadata_together(tmp_path, mocker):
    from app.services.course_history import CourseHistory

    clock = mocker.patch('app.services.course_history.time.time', return_value=1000.)
    history = CourseHistory(tmp_path / 'history.sqlite3', ttl_seconds=10, max_entries=2)
    for number in range(3):
        clock.return_value = 1000. + number
        assert await history.record_if_novel('user', {f'id-{number}'}, metadata={'attraction_ids': [f'id-{number}'], 'areas': [f'area-{number}']})
    entries = await history.recent_profiles('user')
    assert {item['areas'][0] for item in entries} == {'area-1', 'area-2'}
    with sqlite3.connect(history.path) as db:
        assert 'area-0' not in '\n'.join(db.iterdump())
    clock.return_value = 1020.
    assert await history.recent_profiles('user') == []
    with sqlite3.connect(history.path) as db:
        assert not any(f'area-{number}' in '\n'.join(db.iterdump()) for number in range(3))


@pytest.fixture
def diversity_dependencies(tmp_path, mocker):
    from app.agent.course_graph import Candidate, CourseDraft, DraftDay
    from app.agent.tools.verified_maps import Destination, VerifiedPlace
    from app.services.course_history import CourseHistory

    request = CourseRequestSchema.model_validate({'userId': '550e8400-e29b-41d4-a716-446655440000', 'mbti': 'INTJ', 'tripCondition': {'destinationCountry': '대한민국', 'destinationCity': '서울특별시', 'startDate': '2026-10-05', 'totalDays': 1, 'budgetType': 'moderate'}})
    places = {}

    def make_draft(area, version='', *, same_attractions=False):
        index = {'광화문 북촌': 0, '성수 서울숲': 1, '마포 망원': 2}[area]
        candidates = []
        for number, (role, category) in enumerate([('none', 'museum'), ('lunch', 'restaurant'), ('none', 'park'), ('dinner', 'restaurant'), ('none', 'art_gallery')]):
            suffix = '' if same_attractions and role == 'none' else version
            name = f'{area}-{number}{suffix}'
            candidate = Candidate(name=name, meal=role, stay_minutes=45, planned_area=area, experiences=['culture' if category == 'museum' else 'nature' if category == 'park' else 'food'])
            candidates.append(candidate)
            places[name] = VerifiedPlace(PlaceSchema(placeId=f'places/{name}', placeName=name, category=category, address='대한민국 서울특별시', latitude=37.50 + index * .04 + number * .0001, longitude=127.0))
        return CourseDraft(title=f'계획 {area}{version}', reason='취향을 유지한 계획', days=[DraftDay(candidates=candidates)])

    drafts = {area: make_draft(area) for area in ('광화문 북촌', '성수 서울숲', '마포 망원')}
    provider = mocker.Mock()
    provider.resolve_destination = AsyncMock(return_value=Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3))
    provider.search = AsyncMock(side_effect=lambda candidate, destination: places[candidate.name])
    provider.discover_meals = AsyncMock(return_value=[])
    provider.photo = AsyncMock(return_value=None)

    async def route(left, right, **kwargs):
        minutes = 120 if abs(left.place.latitude - right.place.latitude) > .01 else 10
        return TransportToNextSchema(type='transit', distance=500, minutes=minutes, cost=1500)

    provider.route = AsyncMock(side_effect=route)
    llm = mocker.patch('app.agent.course_graph.draft_candidates', new_callable=AsyncMock)
    history = CourseHistory(tmp_path / 'actual-history.sqlite3')
    return request, provider, history, llm, drafts, places, make_draft


def course_attractions(course):
    return {stop.place.placeId for day in course.itinerary.days for stop in day.stops if stop.place.category not in {'restaurant', 'cafe'}}


@pytest.mark.asyncio
async def test_three_identical_seoul_requests_change_real_selected_neighborhoods(diversity_dependencies):
    """Scripted repeated drafts require the actual graph to detect and repair similarity."""
    from app.agent.course_graph import build_course_graph
    from app.services.course_diversity import overlap_scores
    from app.services.course_history import history_key
    from app.services.course_routing import distance_meters

    request, provider, history, llm, drafts, places, make_draft = diversity_dependencies
    areas = list(drafts)
    llm.side_effect = [drafts[areas[0]], make_draft(areas[0], '-new-food', same_attractions=True), drafts[areas[1]], make_draft(areas[1], '-all-new'), drafts[areas[2]]]
    courses = []
    calls = []
    for _ in range(3):
        before = llm.await_count
        state = await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0})
        courses.append(state['course'])
        calls.append(llm.await_count - before)
    assert calls == [1, 2, 2]
    for course, area in zip(courses, areas):
        assert all(area in stop.place.placeName for stop in course.itinerary.days[0].stops)
    entries = await history.recent_profiles(history_key(request))
    assert {area for entry in entries for area in entry['areas']} == set(areas)
    for left, right in combinations(courses, 2):
        left_ids = {stop.place.placeId for stop in left.itinerary.days[0].stops}
        right_ids = {stop.place.placeId for stop in right.itinerary.days[0].stops}
        scores = overlap_scores(left_ids, course_attractions(left), [profile(right_ids, course_attractions(right))])
        assert scores['attraction_overlap'] <= .4
        assert distance_meters(places[left.itinerary.days[0].stops[0].place.placeName], places[right.itinerary.days[0].stops[0].place.placeName]) >= 3000
    assert areas[0] in llm.await_args_list[1].args[2]
    assert areas[1] in llm.await_args_list[4].args[2]
    # Every search uses a current draft name, never an old ID reverse lookup.
    assert all(call.args[0].name in places for call in provider.search.await_args_list)
    provider.discover_meals.assert_not_awaited()
    assert all(len(day.stops) == 5 for course in courses for day in course.itinerary.days)
    assert not any(key in json.dumps(course.model_dump()) for course in courses for key in ('planned_area', 'experiences', 'place_overlap'))


@pytest.mark.asyncio
@pytest.mark.parametrize('repair_failure', ['timeout', 'unsafe_places'])
async def test_novelty_repair_failure_returns_captured_verified_course(diversity_dependencies, repair_failure, offline_place_copy):
    from app.agent.course_graph import build_course_graph
    from app.agent.tools.verified_maps import VerifiedPlace
    from app.services.course_history import history_key

    request, provider, history, llm, drafts, places, make_draft = diversity_dependencies
    central = drafts['광화문 북촌']
    ids = {places[candidate.name].place.placeId for candidate in central.days[0].candidates}
    attractions = {places[candidate.name].place.placeId for candidate in central.days[0].candidates if candidate.meal == 'none'}
    await history.record_if_novel(history_key(request), ids, metadata={'attraction_ids': list(attractions), 'areas': ['광화문 북촌']})
    similar = make_draft('광화문 북촌', '-food', same_attractions=True)
    alternate = make_draft('성수 서울숲', '-unsafe')
    if repair_failure == 'timeout':
        llm.side_effect = [similar, TimeoutError('temporary LLM timeout')]
    else:
        for candidate in alternate.days[0].candidates:
            venue = places[candidate.name]
            places[candidate.name] = VerifiedPlace(venue.place.model_copy(update={'latitude': 0.}))
        llm.side_effect = [similar, alternate]
    course = (await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0}))['course']
    assert course.title == similar.title
    assert len(course.itinerary.days[0].stops) == 5
    assert all('광화문 북촌' in stop.place.placeName for stop in course.itinerary.days[0].stops)
    assert '이전' in course.recommendationReason or '중복' in course.recommendationReason
    assert llm.await_count <= 2
    assert all(stop.transportToNext.minutes <= 90 for stop in course.itinerary.days[0].stops[:-1])
    offline_place_copy.assert_awaited_once()
    assert {row['placeId'] for row in offline_place_copy.call_args.args[0]['stops']} == {stop.place.placeId for stop in course.itinerary.days[0].stops}


@pytest.mark.asyncio
async def test_near_deadline_novelty_retry_does_not_lose_verified_fallback(diversity_dependencies):
    from app.agent.course_graph import build_course_graph
    from app.services.course_history import history_key

    request, provider, history, llm, drafts, places, _ = diversity_dependencies
    draft = drafts['광화문 북촌']
    ids = {places[candidate.name].place.placeId for candidate in draft.days[0].candidates}
    await history.record_if_novel(history_key(request), ids)

    async def draft_response(*args):
        if llm.await_count > 1:
            await asyncio.Event().wait()
        return draft

    llm.side_effect = draft_response
    now = asyncio.get_running_loop().time()
    state = await asyncio.wait_for(build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0, 'deadline': now + .6}), timeout=.9)
    assert len(state['course'].itinerary.days[0].stops) == 5
    assert state['course'].title == draft.title
    assert asyncio.get_running_loop().time() < now + .9


def test_day_shortlist_prefers_new_attractions_over_original_and_meal_variations(diversity_dependencies):
    from app.agent.course_graph import _day_plans

    _, _, _, _, drafts, places, _ = diversity_dependencies
    old = drafts['광화문 북촌'].days[0].candidates
    fresh = drafts['성수 서울숲'].days[0].candidates
    available = [(candidate, places[candidate.name]) for candidate in [*old, *[candidate for candidate in fresh if candidate.meal == 'none']]]
    old_ids = {places[candidate.name].place.placeId for candidate in old}
    old_attractions = {places[candidate.name].place.placeId for candidate in old if candidate.meal == 'none'}
    plans = _day_plans(available, date(2026, 10, 5), 4, 2, old_ids, recent_profiles=[profile(old_ids, old_attractions, ['광화문 북촌'])])
    assert plans and len(plans) <= 4
    first_attractions = {venue.place.placeId for candidate, venue in plans[0] if candidate.meal == 'none'}
    assert first_attractions.isdisjoint(old_attractions)
    assert {candidate.meal for candidate, _ in plans[0]} >= {'lunch', 'dinner'}


@pytest.mark.asyncio
async def test_verified_alternate_neighborhood_in_same_pool_needs_no_extra_llm(diversity_dependencies):
    from app.agent.course_graph import CourseDraft, DraftDay, build_course_graph
    from app.services.course_history import history_key

    request, provider, history, llm, drafts, places, _ = diversity_dependencies
    central = drafts['광화문 북촌'].days[0].candidates
    alternate = drafts['성수 서울숲'].days[0].candidates
    ids = {places[candidate.name].place.placeId for candidate in central}
    attractions = {places[candidate.name].place.placeId for candidate in central if candidate.meal == 'none'}
    await history.record_if_novel(history_key(request), ids, metadata={'attraction_ids': list(attractions), 'areas': ['광화문 북촌']})
    llm.return_value = CourseDraft(title='취향과 동선을 유지한 새 권역', reason='대안', days=[DraftDay(candidates=[*central, *alternate])])
    course = (await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0}))['course']
    assert all('성수 서울숲' in stop.place.placeName for stop in course.itinerary.days[0].stops)
    assert course_attractions(course).isdisjoint(attractions)
    llm.assert_awaited_once()
    assert provider.search.await_count == 10
    assert len({call.args[0].name for call in provider.search.await_args_list}) == 10
    assert len({(call.args[0].place.placeId, call.args[1].place.placeId, call.kwargs.get('departure_time')) for call in provider.route.await_args_list}) == provider.route.await_count


@pytest.mark.asyncio
async def test_first_request_no_history_keeps_existing_calls_and_stream_contract(diversity_dependencies, mocker):
    from app.agent.course_graph import stream_course_generation

    request, provider, history, llm, drafts, _, _ = diversity_dependencies
    llm.return_value = drafts['광화문 북촌']
    provider.__aenter__ = AsyncMock(return_value=provider)
    provider.__aexit__ = AsyncMock(return_value=False)
    mocker.patch('app.agent.course_graph.VerifiedMapsProvider', return_value=provider)
    mocker.patch('app.agent.course_graph.CourseHistory', return_value=history)
    events = [event async for event in stream_course_generation(request)]
    assert events[-1][0] == 'complete' and sum(event == 'complete' for event, _ in events) == 1
    assert all(set(data) == {'step', 'message'} and data['step'] == 'GENERATING_ROUTE' for event, data in events if event == 'progress')
    assert set(events[-1][1]) == {'course'}
    assert events[-1][1]['course']['destinationCity'] == '서울특별시'
    assert provider.search.await_count == 5 and provider.route.await_count == 4
    llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_history_core_attractions_exclude_coffee_dessert_and_meal_businesses(diversity_dependencies):
    """Food-only changes cannot dilute the remembered museum/park core."""
    from uuid import UUID

    from app.agent.course_graph import Candidate, build_course_graph
    from app.agent.tools.verified_maps import VerifiedPlace
    from app.services.course_diversity import overlap_scores
    from app.services.course_history import history_key

    request, provider, history, llm, drafts, places, _ = diversity_dependencies
    base = drafts['광화문 북촌']
    core = {places[candidate.name].place.placeId for candidate in base.days[0].candidates if candidate.meal == 'none'}
    for index, category in enumerate(('coffee_shop', 'tea_house', 'bakery', 'dessert_shop', 'dessert_restaurant', 'japanese_restaurant')):
        request.userId = UUID(int=index + 1)
        food = Candidate(name=f'별도 음식 후보-{category}', planned_area='음식점만 붙인 계획 라벨', experiences=['food'], stay_minutes=30)
        places[food.name] = VerifiedPlace(PlaceSchema(placeId=f'places/food-{category}', placeName=food.name, category=category, address='대한민국 서울특별시', latitude=37.50015, longitude=127.0))
        draft = base.model_copy(deep=True)
        draft.days[0].candidates.append(food)
        llm.return_value = draft
        course = (await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0}))['course']
        assert f'places/food-{category}' in {stop.place.placeId for stop in course.itinerary.days[0].stops}
        remembered = (await history.recent_profiles(history_key(request)))[0]
        assert set(remembered['attraction_ids']) == core
        assert remembered['areas'] == ['광화문 북촌']
        # A completely different food list leaves the same core experience.
        scores = overlap_scores(core | {'new-food-a', 'new-food-b'}, core, [remembered])
        assert scores['place_overlap'] == .6 and scores['attraction_overlap'] == 1.


@pytest.mark.asyncio
async def test_legacy_id_only_history_retries_restaurant_only_changes(diversity_dependencies):
    from app.agent.course_graph import build_course_graph
    from app.services.course_history import history_key

    request, provider, history, llm, drafts, places, make_draft = diversity_dependencies
    old = drafts['광화문 북촌']
    ids = {places[candidate.name].place.placeId for candidate in old.days[0].candidates}
    assert await history.record_if_novel(history_key(request), ids)
    assert not (await history.recent_profiles(history_key(request)))[0].get('attraction_ids')
    llm.side_effect = [make_draft('광화문 북촌', '-restaurants', same_attractions=True), drafts['성수 서울숲']]
    course = (await build_course_graph(provider, history).ainvoke({'request': request, 'attempt': 0}))['course']
    assert all('성수 서울숲' in stop.place.placeName for stop in course.itinerary.days[0].stops)
    assert llm.await_count == 2
    assert all(call.args[0].name in places for call in provider.search.await_args_list)


def test_pool_ranking_prefers_all_novelty_targets_over_zero_core_overlap_alone():
    """A feasible 0/.5 core/place score must not outrank feasible 1/3/.2."""
    from app.agent.course_graph import Candidate, _diversity_rank, _schedule_day
    from app.agent.tools.verified_maps import VerifiedPlace
    from app.services.course_diversity import overlap_scores, planning_overlap

    old_ids = {'shared-core', 'old-core-2', 'old-core-3', 'old-lunch', 'old-dinner'}
    old_core = {'shared-core', 'old-core-2', 'old-core-3'}
    recent = [profile(old_ids, old_core, ['이전 계획 권역'])]
    pool = {}
    for index, (identifier, meal) in enumerate([
        ('new-core-a', 'none'), ('old-lunch', 'lunch'), ('new-core-b', 'none'), ('old-dinner', 'dinner'),
        ('shared-core', 'none'), ('new-lunch', 'lunch'), ('new-dinner', 'dinner'),
    ]):
        candidate = Candidate(name=identifier, meal=meal, planned_area='새 계획 권역', experiences=['culture' if meal == 'none' else 'food'], stay_minutes=45)
        venue = VerifiedPlace(PlaceSchema(placeId=identifier, placeName=identifier, category='museum' if meal == 'none' else 'restaurant', address='대한민국 서울특별시', latitude=37.55 + index * .0001, longitude=127.))
        pool[identifier] = (candidate, venue)
    lower_core_only = [pool[key] for key in ('new-core-a', 'old-lunch', 'new-core-b', 'old-dinner')]
    all_targets = [pool[key] for key in ('new-core-a', 'new-lunch', 'shared-core', 'new-core-b', 'new-dinner')]
    for path in (lower_core_only, all_targets):
        routes = [TransportToNextSchema(type='transit', distance=500, minutes=10, cost=1500) for _ in range(len(path) - 1)]
        assert len(_schedule_day(path, routes, date(2026, 10, 5), 1).stops) == len(path)
        assert planning_overlap([candidate.planned_area for candidate, _ in path], recent, 'areas') <= .4
    def scores(path):
        return overlap_scores({venue.place.placeId for _, venue in path}, {venue.place.placeId for candidate, venue in path if candidate.meal == 'none'}, recent)
    assert scores(lower_core_only) == {'place_overlap': .5, 'attraction_overlap': 0.}
    assert scores(all_targets) == {'place_overlap': .2, 'attraction_overlap': 1 / 3}
    assert _diversity_rank(all_targets, recent) < _diversity_rank(lower_core_only, recent)

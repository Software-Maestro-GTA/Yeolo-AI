"""Keep feasible measured long transfers while preferring bounded alternatives.

LLM and Maps boundaries are offline; real graph scheduling and route selection
must retain meal windows, stay durations, unique venues and actual metrics.
"""

import asyncio
from collections import Counter

import pytest

from app.agent.course_graph import build_course_graph
from app.agent.course_nodes import CourseGraphNodes
from app.agent.course_state import DraftDay
from app.agent.tools.verified_maps import (
    InternalTransportSchema,
    NoRouteError,
    VerifiedPlace,
)
from app.services.course_planning import MEAL_WINDOWS
from tests.test_course_graph import graph_dependencies as _graph_dependencies
from tests.test_course_graph import request_data as _request_data

graph_dependencies = _graph_dependencies
request_data = _request_data


def _spread_places(places):
    """Keep every fixture pair over 3 km so walking is not queried as fallback."""
    for index, (name, venue) in enumerate(list(places.items())):
        places[name] = VerifiedPlace(venue.place.model_copy(update={
            'latitude': 37.5, 'longitude': 126.8 + index * .05,
        }), periods=venue.periods)


def _assert_schedule_fits(record):
    """Check real travel and complete visits against the daily and meal windows."""
    stops = record['day'].stops
    for index, (stop, (candidate, _)) in enumerate(zip(stops, record['selected'])):
        hours, minutes = map(int, stop.arrivalTime.split(':'))
        arrival = hours * 60 + minutes
        earliest, latest = MEAL_WINDOWS[candidate.meal]
        assert max(540, earliest) <= arrival
        assert arrival + stop.stayMinutes <= min(1260, latest)
        assert stop.stayMinutes == candidate.stay_minutes
        if index:
            previous = stops[index - 1]
            hours, minutes = map(int, previous.arrivalTime.split(':'))
            earliest_arrival = hours * 60 + minutes + previous.stayMinutes
            earliest_arrival += previous.transportToNext.minutes + 10
            assert arrival >= earliest_arrival


@pytest.mark.asyncio
async def test_faster_verified_taxi_is_used_before_a_long_transit_route(
    request_data, graph_dependencies,
):
    provider, history, llm, _, places = graph_dependencies
    _spread_places(places)

    async def route(origin, destination, mode='walking', departure_time=None):
        if mode == 'transit':
            return InternalTransportSchema(type=mode, distance=20000, minutes=120, cost=2000)
        assert mode == 'taxi' and departure_time is None
        return InternalTransportSchema(type=mode, distance=15000, minutes=30, cost=18000)

    provider.route.side_effect = route
    result = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    stops = result['course'].itinerary.days[0].stops
    assert len(stops) == 5
    assert all((stop.transportToNext.type, stop.transportToNext.minutes, stop.transportToNext.cost)
               == ('taxi', 30, 18000) for stop in stops[:-1])
    assert result['course'].totalCost == 35000 + 4 * 18000
    modes = [call.kwargs['mode'] for call in provider.route.await_args_list]
    assert set(modes) == {'transit', 'taxi'} and modes.count('taxi') == 4
    assert provider.search.await_count == 5
    _assert_schedule_fits(result['validated_days'][0])
    llm.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('total_days', [1, 2])
async def test_long_transit_survives_missing_faster_mode_without_duplicate_days(
    request_data, graph_dependencies, total_days,
):
    provider, history, llm, draft, places = graph_dependencies
    request_data.tripCondition.totalDays = total_days
    _spread_places(places)
    if total_days == 2:
        candidates = []
        for index, original in enumerate(draft.days[0].candidates):
            candidate = original.model_copy(update={'name': f'둘째날 {original.name}'})
            candidates.append(candidate)
            venue = places[original.name]
            places[candidate.name] = VerifiedPlace(venue.place.model_copy(update={
                'placeId': f'places/second-day-{index}', 'placeName': candidate.name,
            }), periods=venue.periods)
        draft.days.append(DraftDay(candidates=candidates))

    async def route(origin, destination, mode='walking', departure_time=None):
        if mode == 'taxi':
            raise NoRouteError()
        assert mode == 'transit'
        long = '미술관' in {origin.place.placeName, destination.place.placeName}
        return InternalTransportSchema(type=mode, distance=25000 if long else 4000,
                                       minutes=120 if long else 12, cost=2500 if long else 1000)

    provider.route.side_effect = route
    result = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    days = result['course'].itinerary.days
    assert len(days) == total_days and all(len(day.stops) == 5 for day in days)
    ids = [stop.place.placeId for day in days for stop in day.stops]
    assert len(ids) == len(set(ids)) == 5 * total_days
    first_routes = [stop.transportToNext for stop in days[0].stops[:-1]]
    assert any((route.type, route.minutes, route.distance, route.cost) == ('transit', 120, 25000, 2500)
               for route in first_routes)
    assert all(stop.transportToNext.type == 'transit' for day in days for stop in day.stops[:-1])
    for record in result['validated_days'].values():
        _assert_schedule_fits(record)
    assert any(route.minutes == 120 for route in result['route_cache'].values())
    assert all(route.type != 'unknown' for route in result['route_cache'].values())
    queries = Counter((call.args[0].place.placeId, call.args[1].place.placeId,
                       call.kwargs['mode'], call.kwargs['departure_time'])
                      for call in provider.route.await_args_list)
    assert max(queries.values()) == 1
    assert provider.search.await_count == 5 * total_days
    llm.assert_awaited_once()


async def _route_node_state(request_data, graph_dependencies, mocker):
    """Build real prepared state for deterministic existing-plan alternatives."""
    provider, history, _, draft, places = graph_dependencies
    mocker.patch('app.agent.course_nodes.get_stream_writer', return_value=lambda event: None)
    nodes = CourseGraphNodes(provider, history)
    state = {'request': request_data, 'attempt': 0}
    state.update(await nodes.prepare(state))
    candidates = {candidate.name: candidate for candidate in draft.days[0].candidates}
    plan = [(candidates[name], places[name])
            for name in ('미술관', '점심식당', '공원', '역사관', '저녁식당')]
    state.update({'draft_data': draft, 'day_plans': [[plan]], 'failures': []})
    return nodes, state, plan


@pytest.mark.asyncio
async def test_existing_nearby_candidate_is_preferred_to_a_feasible_long_route(
    request_data, graph_dependencies, mocker,
):
    provider, _, llm, _, _ = graph_dependencies
    nodes, state, nearby_plan = await _route_node_state(request_data, graph_dependencies, mocker)
    original, venue = nearby_plan[0]
    remote_candidate = original.model_copy(update={'name': '먼 미술관'})
    remote_venue = VerifiedPlace(venue.place.model_copy(update={
        'placeId': 'places/remote-museum', 'placeName': '먼 미술관', 'latitude': 37.7,
    }))
    state['day_plans'] = [[[(remote_candidate, remote_venue), *nearby_plan[1:]], nearby_plan]]

    async def route(origin, destination, mode='walking', departure_time=None):
        long = origin.place.placeId == 'places/remote-museum'
        return InternalTransportSchema(type=mode, distance=20000 if long else 700,
                                       minutes=120 if long else 12, cost=0)

    provider.route.side_effect = route
    result = await nodes.verify_routes(state)
    assert result['feedback'] == ''
    assert result['selected'][0] == nearby_plan
    assert all(route.minutes == 12 for route in result['routes'][0])
    _assert_schedule_fits(result['validated_days'][0])
    provider.search.assert_not_awaited()
    llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_measured_long_transfer_does_not_override_impossible_meal_windows(
    request_data, graph_dependencies, mocker,
):
    provider, _, llm, _, _ = graph_dependencies
    nodes, state, _ = await _route_node_state(request_data, graph_dependencies, mocker)

    async def route(origin, destination, mode='walking', departure_time=None):
        return InternalTransportSchema(type=mode, distance=100000, minutes=800, cost=0)

    provider.route.side_effect = route
    result = await nodes.verify_routes(state)
    assert result['validated_days'] == {}
    assert '방문/식사 시간' in result['feedback']
    assert 'selected' not in result
    assert result['rejected_plans'][0]
    assert all(route.minutes == 800 and route.type != 'unknown' for route in result['route_cache'].values())
    llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_known_long_route_is_preferred_to_a_missing_route(
    request_data, graph_dependencies, mocker,
):
    provider, _, llm, _, _ = graph_dependencies
    nodes, state, measured_plan = await _route_node_state(request_data, graph_dependencies, mocker)
    candidate, venue = measured_plan[0]
    unknown_candidate = candidate.model_copy(update={'name': '경로 미확인 미술관'})
    unknown_venue = VerifiedPlace(venue.place.model_copy(update={
        'placeId': 'places/unknown-museum', 'placeName': unknown_candidate.name,
    }))
    state['day_plans'] = [[[(unknown_candidate, unknown_venue), *measured_plan[1:]], measured_plan]]

    async def route(origin, destination, mode='walking', departure_time=None):
        if origin.place.placeId == 'places/unknown-museum':
            raise NoRouteError()
        long = origin.place.placeName == '미술관'
        return InternalTransportSchema(type=mode, distance=20000 if long else 700,
                                       minutes=120 if long else 12, cost=0)

    provider.route.side_effect = route
    result = await nodes.verify_routes(state)
    assert result['feedback'] == ''
    assert result['selected'][0] == measured_plan
    assert result['routes'][0][0].minutes == 120
    assert all(route.type != 'unknown' for route in result['routes'][0])
    _assert_schedule_fits(result['validated_days'][0])
    llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_feasible_five_stop_long_day_is_not_reduced_for_a_shorter_route(
    request_data, graph_dependencies, mocker,
):
    provider, _, llm, _, _ = graph_dependencies
    nodes, state, full_plan = await _route_node_state(request_data, graph_dependencies, mocker)
    state['repairing'] = True
    state['day_plans'] = [[full_plan, full_plan[1:]]]

    async def route(origin, destination, mode='walking', departure_time=None):
        long = origin.place.placeName == '미술관'
        return InternalTransportSchema(type=mode, distance=20000 if long else 700,
                                       minutes=120 if long else 12, cost=0)

    provider.route.side_effect = route
    result = await nodes.verify_routes(state)
    assert result['feedback'] == ''
    assert result['selected'][0] == full_plan
    assert len(result['validated_days'][0]['day'].stops) == 5
    assert result['routes'][0][0].minutes == 120
    _assert_schedule_fits(result['validated_days'][0])
    llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_feasible_long_route_does_not_query_a_stalled_mode(request_data, graph_dependencies, mocker):
    provider, _, _, _, places = graph_dependencies
    _spread_places(places)
    nodes, state, plan = await _route_node_state(request_data, graph_dependencies, mocker)
    mocker.patch('app.agent.course_nodes.ROUTE_ALTERNATIVE_SECONDS', .02)
    cancelled = []

    async def route(origin, destination, mode='walking', departure_time=None):
        if mode == 'taxi':
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        long = origin.place.placeName == '미술관'
        return InternalTransportSchema(type=mode, distance=20000 if long else 700,
                                       minutes=120 if long else 12, cost=0)

    provider.route.side_effect = route
    result = await asyncio.wait_for(nodes.verify_routes(state), timeout=1)
    assert result['selected'][0] == plan
    assert result['routes'][0][0].minutes == 120
    assert cancelled == []
    _assert_schedule_fits(result['validated_days'][0])


@pytest.mark.asyncio
async def test_slow_nearby_candidate_cannot_discard_a_feasible_long_day(
    request_data, graph_dependencies, mocker,
):
    provider, _, llm, _, _ = graph_dependencies
    nodes, state, plan = await _route_node_state(request_data, graph_dependencies, mocker)
    mocker.patch('app.agent.course_nodes.ROUTE_ALTERNATIVE_SECONDS', .02)
    candidate, venue = plan[0]
    nearby = VerifiedPlace(venue.place.model_copy(update={
        'placeId': 'places/slow-nearby', 'latitude': plan[1][1].place.latitude,
    }))
    state['day_plans'][0].append([(candidate, nearby), *plan[1:]])
    cancelled = []

    async def route(origin, destination, mode='walking', departure_time=None):
        if origin.place.placeId == 'places/slow-nearby':
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        long = origin.place.placeName == '미술관'
        return InternalTransportSchema(type=mode, distance=20000 if long else 700,
                                       minutes=120 if long else 12, cost=0)

    provider.route.side_effect = route
    result = await asyncio.wait_for(nodes.verify_routes(state), timeout=1)
    assert result['feedback'] == ''
    assert result['selected'][0] == plan
    assert result['routes'][0][0].minutes == 120
    assert cancelled == [True]
    _assert_schedule_fits(result['validated_days'][0])
    llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_feasible_days_do_not_query_an_equal_distance_conflicting_alternative(
    request_data, graph_dependencies, mocker,
):
    """Choose two disjoint long days over one short day that consumes both options."""
    provider, _, llm, draft, _ = graph_dependencies
    request_data.tripCondition.totalDays = 2
    nodes, state, first_plan = await _route_node_state(request_data, graph_dependencies, mocker)
    second_plan = []
    for index, (candidate, venue) in enumerate(first_plan):
        copied = candidate.model_copy(update={'name': f'둘째날 {candidate.name}'})
        second_plan.append((copied, VerifiedPlace(venue.place.model_copy(update={
            'placeId': f'places/second-day-{index}', 'placeName': copied.name,
        }), periods=venue.periods)))
    draft.days.append(DraftDay(candidates=[candidate for candidate, _ in second_plan]))
    short_conflicting_plan = [second_plan[0], *first_plan[1:]]
    state['day_plans'] = [[first_plan, short_conflicting_plan], [second_plan]]
    long_edges = {
        (plan[0][1].place.placeId, plan[1][1].place.placeId)
        for plan in (first_plan, second_plan)
    }
    queried = []

    async def route(origin, destination, mode='walking', departure_time=None):
        edge = origin.place.placeId, destination.place.placeId
        queried.append(edge)
        long = edge in long_edges
        return InternalTransportSchema(type=mode, distance=20000 if long else 700,
                                       minutes=120 if long else 12, cost=0)

    provider.route.side_effect = route
    result = await nodes.verify_routes(state)
    assert result['feedback'] == ''
    assert result['selected'] == [first_plan, second_plan]
    assert set(result['validated_days']) == {0, 1}
    ids = [venue.place.placeId for plan in result['selected'] for _, venue in plan]
    assert len(ids) == len(set(ids)) == 10
    assert (second_plan[0][1].place.placeId, first_plan[1][1].place.placeId) not in queried
    assert all(routes[0].minutes == 120 for routes in result['routes'])
    for record in result['validated_days'].values():
        _assert_schedule_fits(record)
    provider.search.assert_not_awaited()
    llm.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('minutes', [89, 90, 91, 120])
async def test_fitting_transfer_has_identical_lookup_policy_across_former_boundary(
    request_data, graph_dependencies, mocker, minutes,
):
    provider, _, llm, _, _ = graph_dependencies
    nodes, state, plan = await _route_node_state(request_data, graph_dependencies, mocker)

    async def route(origin, destination, mode='walking', departure_time=None):
        assert mode == 'transit', 'A fitting measured link must not trigger extra mode lookups'
        duration = minutes if origin.place.placeName == '미술관' else 12
        return InternalTransportSchema(type=mode, distance=25000, minutes=duration, cost=2500)

    provider.route.side_effect = route
    result = await nodes.verify_routes(state)
    assert result['selected'][0] == plan and result['feedback'] == ''
    assert result['routes'][0][0].minutes == minutes
    assert provider.route.await_count == len(plan) - 1
    _assert_schedule_fits(result['validated_days'][0])
    llm.assert_not_awaited()

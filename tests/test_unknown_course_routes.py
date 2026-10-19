"""Keep multi-day venues, known fares and route-cache truth during outages."""

from dataclasses import replace

import pytest

from app.agent.course_graph import build_course_graph
from app.agent.course_state import DraftDay
from app.agent.tools.verified_maps import (
    InternalTransportSchema,
    MapsProviderError,
    NoRouteError,
    VerifiedPlace,
)
from tests.test_course_graph import graph_dependencies as _graph_dependencies
from tests.test_course_graph import request_data as _request_data

graph_dependencies = _graph_dependencies
request_data = _request_data


@pytest.mark.asyncio
async def test_three_days_complete_with_only_failed_day_transfers_unknown(request_data, graph_dependencies):
    provider, history, llm, draft, places = graph_dependencies
    request_data.tripCondition.totalDays = 3
    originals = list(draft.days[0].candidates)
    for day in (2, 3):
        candidates = []
        for index, original in enumerate(originals):
            candidate = original.model_copy(update={'name': f'{day}일차 {original.name}'})
            candidates.append(candidate)
            venue = places[original.name]
            places[candidate.name] = VerifiedPlace(venue.place.model_copy(update={
                'placeId': f'places/day-{day}-{index}', 'placeName': candidate.name,
            }), periods=venue.periods)
        draft.days.append(DraftDay(candidates=candidates))

    async def route(origin, destination, mode='walking', departure_time=None):
        if origin.place.placeName.startswith('2일차'):
            raise NoRouteError()
        return InternalTransportSchema(type=mode, distance=700, minutes=12, cost=1800)

    provider.route.side_effect = route
    state = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    course = state['course']
    days = course.itinerary.days
    assert len(days) == 3 and all(len(day.stops) == 5 for day in days)
    ids = [stop.place.placeId for day in days for stop in day.stops]
    assert len(set(ids)) == len(ids) == 15
    assert all(stop.transportToNext.type == 'unknown' and stop.transportToNext.minutes is None
               and stop.transportToNext.distance is None and stop.transportToNext.cost is None
               for stop in days[1].stops[:-1])
    assert all(stop.transportToNext.minutes == 12 and stop.transportToNext.distance == 700
               for day in (days[0], days[2]) for stop in day.stops[:-1])
    assert all(day.stops[-1].transportToNext.type == 'none' for day in days)
    # Missing stop-to-stop fares stay unknown; two hotel nights are independent.
    assert state['trip_quotes'].lodging.status == 'estimated'
    assert course.totalCost == 3 * 35000 + 8 * 1800 + 2 * 100000
    assert all(route.type != 'unknown' for route in state['route_cache'].values())
    assert state['route_pass_failures']
    llm.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['no_route', 'transient'])
@pytest.mark.parametrize('far_apart', [False, True])
async def test_verified_taxi_is_used_before_unknown_without_invented_fares(
    request_data, graph_dependencies, failure, far_apart,
):
    provider, history, llm, _, places = graph_dependencies
    if far_apart:
        for index, (name, venue) in enumerate(list(places.items())):
            places[name] = VerifiedPlace(venue.place.model_copy(update={'latitude': 37.4 + index * .04}))
    queried = []

    async def route(origin, destination, mode='walking', departure_time=None):
        queried.append((origin.place.placeId, destination.place.placeId, mode))
        if mode != 'taxi':
            raise NoRouteError() if failure == 'no_route' else MapsProviderError('transient', 503)
        assert departure_time is None
        return InternalTransportSchema(type='taxi', distance=5200, minutes=18, cost=None)

    provider.route.side_effect = route
    state = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    stops = state['course'].itinerary.days[0].stops
    assert len(stops) == 5
    assert all((stop.transportToNext.type, stop.transportToNext.distance, stop.transportToNext.minutes,
                stop.transportToNext.cost) == ('taxi', 5200, 18, None) for stop in stops[:-1])
    assert state['course'].totalCost == 35000
    modes = [mode for _, _, mode in queried]
    assert modes[:2 if far_apart else 3] == (['transit', 'taxi'] if far_apart else ['transit', 'walking', 'taxi'])
    assert ('walking' in modes) is not far_apart
    assert len(queried) == len(set(queried))
    if failure == 'transient':
        assert modes.count('transit') == 1
        assert modes.count('walking') <= 1
    llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_walking_outage_still_allows_verified_transit(request_data, graph_dependencies):
    provider, history, llm, _, _ = graph_dependencies
    provider.resolve_destination.return_value = replace(provider.resolve_destination.return_value, country_code='JP')
    queried = []

    async def route(origin, destination, mode='walking', departure_time=None):
        queried.append(mode)
        if mode == 'walking':
            raise MapsProviderError('transient', 503)
        assert mode == 'transit'
        return InternalTransportSchema(type='transit', distance=700, minutes=12, cost=1800)

    provider.route.side_effect = route
    state = await build_course_graph(provider, history).ainvoke({'request': request_data, 'attempt': 0})
    assert all(stop.transportToNext.type == 'transit' for stop in state['course'].itinerary.days[0].stops[:-1])
    assert queried == ['walking', 'transit', 'transit', 'transit', 'transit']
    llm.assert_awaited_once()


def test_disjoint_fallback_minimizes_total_unknown_edges_instead_of_first_complete_choice(graph_dependencies):
    from app.services.course_planning import _choose_disjoint_plans

    _, _, _, draft, places = graph_dependencies
    shared = [(draft.days[0].candidates[0], places['미술관'])]
    alternative = [(draft.days[0].candidates[2], places['공원'])]
    expensive = [(draft.days[0].candidates[4], places['역사관'])]
    plans = {0: [shared, alternative], 1: [shared, expensive]}
    costs = {(0, '미술관'): 1, (0, '공원'): 2, (1, '미술관'): 1, (1, '역사관'): 4}
    chosen = _choose_disjoint_plans(plans, set(), plan_cost=lambda day, plan: costs[day, plan[0][0].name])
    assert chosen == {0: alternative, 1: shared}


def test_route_quality_never_discards_a_day_or_reuses_a_reserved_place(graph_dependencies):
    from app.services.course_planning import _choose_disjoint_plans

    _, _, _, draft, places = graph_dependencies
    reserved = [(draft.days[0].candidates[0], places['미술관'])]
    shared = [(draft.days[0].candidates[2], places['공원'])]
    alternative = [(draft.days[0].candidates[4], places['역사관'])]
    chosen = _choose_disjoint_plans(
        {0: [reserved, shared, alternative], 1: [shared]}, {'verified-0'},
        plan_cost=lambda day, plan: 4 if plan == alternative else 0,
    )
    assert chosen == {0: alternative, 1: shared}

"""Exercise compact API output, optional quote totals and bounded lazy lookups."""

from dataclasses import replace
from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.agent.course_graph import build_course_graph, stream_course_generation
from app.agent.course_state import Candidate, DraftDay
from app.agent.tools.verified_maps import (
    InternalPlaceSchema,
    NoRouteError,
    VerifiedPlace,
)
from app.core.config import settings
from app.services.myrealtrip_quotes import (
    MyRealTripQuoteProvider,
    QuoteItem,
    TripQuotes,
)
from tests.test_course_graph import graph_dependencies as _graph_dependencies
from tests.test_course_graph import request_data as _request_data

# Export only the shared fixture objects into this module, without collecting its tests.
graph_dependencies = _graph_dependencies
request_data = _request_data


def add_second_day(request, draft, places):
    """Reuse normal fixtures with distinct verified identities on the next date."""
    request.tripCondition.totalDays = 2
    candidates = []
    for candidate in draft.days[0].candidates[:5]:
        other = candidate.model_copy(update={'name': candidate.name + '-2일차'})
        original = places[candidate.name]
        places[other.name] = VerifiedPlace(
            original.place.model_copy(update={'placeId': original.place.placeId + '-day2', 'placeName': other.name}),
            periods=original.periods,
        )
        candidates.append(other)
    draft.days.append(DraftDay(candidates=candidates))


def add_deferred_places(draft, places):
    """Nine genuine candidates allow an initial six-query batch plus alternatives."""
    # Keep all six initial visits genuinely feasible with the fixture's 12-minute routes.
    for candidate in draft.days[0].candidates:
        if candidate.meal == 'none':
            candidate.stay_minutes = 30
    for index in range(4):
        candidate = Candidate(name=f'추가명소{index + 1}', stay_minutes=30)
        draft.days[0].candidates.append(candidate)
        places[candidate.name] = VerifiedPlace(InternalPlaceSchema(
            placeId=f'places/extra-{index}', placeName=candidate.name, category='museum',
            address='대한민국 서울', latitude=37.5502 + index * .0001, longitude=126.98,
        ))


@pytest.mark.asyncio
@pytest.mark.parametrize('lodging_available', [True, False])
async def test_real_graph_includes_known_travel_prices_and_keeps_over_budget_course(
    request_data, graph_dependencies, mocker, lodging_available,
):
    provider, history, llm, draft, places = graph_dependencies
    provider.resolve_destination.return_value = replace(
        provider.resolve_destination.return_value,
        verified_country_name='대한민국', verified_city_name='Seoul',
    )
    add_second_day(request_data, draft, places)
    request_data.tripCondition.budget = 20000
    provider.route.return_value.cost = 1800
    quotes = mocker.Mock(spec=MyRealTripQuoteProvider)
    quotes.quote = AsyncMock(return_value=TripQuotes(
        QuoteItem('available', 365000),
        QuoteItem('available', 200000) if lodging_available else QuoteItem('unavailable', reason='provider_error'),
    ))
    graph = build_course_graph(provider, history, quotes=quotes)
    course = None
    progress = []
    async for mode, value in graph.astream(
        {'request': request_data, 'attempt': 0}, stream_mode=['custom', 'updates'],
    ):
        if mode == 'custom':
            progress.append(value)
        elif 'finalize_course' in value:
            course = value['finalize_course']['course']
    assert course is not None
    # Two days of 35,000 KRW activities, eight actual 1,800 KRW fares, and known quotes.
    assert course.totalCost == 70000 + 8 * 1800 + 365000 + (200000 if lodging_available else 0)
    assert course.totalCost > request_data.tripCondition.budget
    assert len(course.itinerary.days) == 2
    assert all(len(day.stops) >= 5 for day in course.itinerary.days)
    ids = [stop.place.placeId for day in course.itinerary.days for stop in day.stops]
    assert len(ids) == len(set(ids))
    assert all(set(stop['transportToNext']) == {'type', 'distance', 'minutes', 'memo'}
               for day in course.model_dump()['itinerary']['days'] for stop in day['stops'])
    assert any('예산' in message['message'] for message in progress)
    if not lodging_available:
        assert any('숙박' in message['message'] and ('포함' in message['message'] or '합계' in message['message']) for message in progress)
    quotes.quote.assert_awaited_once()
    quoted = quotes.quote.await_args.args[0]
    assert (quoted.country_code, quoted.city) == ('KR', '서울')
    assert (quoted.verified_country_name, quoted.verified_city_name) == ('대한민국', 'Seoul')
    assert (quoted.start_date, quoted.end_date) == (date(2026, 10, 5), date(2026, 10, 6))
    assert provider.route.await_count == 8
    llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_nine_candidates_use_one_small_initial_lookup_batch(
    request_data, graph_dependencies,
):
    provider, history, llm, draft, places = graph_dependencies
    add_deferred_places(draft, places)
    updates = [update async for update in build_course_graph(provider, history).astream(
        {'request': request_data, 'attempt': 0}, stream_mode='updates',
    )]
    course = updates[-1]['finalize_course']['course']
    assert 5 <= len(course.itinerary.days[0].stops) <= 6
    assert provider.search.await_count <= 6, [(name, value.get('feedback', '')) for update in updates for name, value in update.items()]
    assert not any('expand_places' in update for update in updates)
    searched = {call.args[0].name for call in provider.search.await_args_list}
    assert {stop.place.placeName for stop in course.itinerary.days[0].stops} <= searched
    llm.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['invalid_place', 'no_route'])
async def test_deferred_real_candidates_restore_failed_day_before_another_llm_call(
    request_data, graph_dependencies, failure,
):
    provider, history, llm, draft, places = graph_dependencies
    add_second_day(request_data, draft, places)
    add_deferred_places(draft, places)
    failed_names = {'미술관', '공원'}
    if failure == 'invalid_place':
        for name in failed_names:
            places[name].place.latitude = 0
    else:
        failed_ids = {places[name].place.placeId for name in failed_names}
        for index, name in enumerate(failed_names):
            places[name].place.latitude = 37.65 + index * .01

        async def route(origin, destination, *args, **kwargs):
            if origin.place.placeId in failed_ids or destination.place.placeId in failed_ids:
                raise NoRouteError()
            return provider.route.return_value.model_copy(deep=True)

        provider.route.side_effect = route
    query_counts = []
    stages = []
    final = None
    async for update in build_course_graph(provider, history).astream(
        {'request': request_data, 'attempt': 0}, stream_mode='updates',
    ):
        stages.extend(update)
        if 'verify_places' in update:
            query_counts.append(provider.search.await_count)
        if 'finalize_course' in update:
            final = update['finalize_course']['course']
    assert final is not None
    assert stages.count('expand_places') == 1, (stages, query_counts)
    assert query_counts == [11, 13]  # First day six plus second day five; expand only two.
    assert all(len(day.stops) >= 5 for day in final.itinerary.days)
    assert not any(stop.place.placeName in failed_names for stop in final.itinerary.days[0].stops)
    ids = [stop.place.placeId for day in final.itinerary.days for stop in day.stops]
    assert len(ids) == len(set(ids))
    assert all(stop.transportToNext.type != 'none' and stop.transportToNext.minutes == 12
               for day in final.itinerary.days for stop in day.stops[:-1])
    llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_stream_passes_explicit_origin_occupancy_and_language_to_providers(
    request_data, graph_dependencies, mocker,
):
    provider, history, llm, _, _ = graph_dependencies
    provider.__aenter__ = AsyncMock(return_value=provider)
    provider.__aexit__ = AsyncMock(return_value=False)
    quotes = mocker.Mock(spec=MyRealTripQuoteProvider)
    quotes.quote = AsyncMock(return_value=TripQuotes(QuoteItem('available', 365000), QuoteItem('not_applicable')))
    quotes.__aenter__ = AsyncMock(return_value=quotes)
    quotes.__aexit__ = AsyncMock(return_value=False)
    mocker.patch.object(settings, 'MYREALTRIP_ORIGIN_AIRPORT', 'ICN')
    mocker.patch.object(settings, 'MYREALTRIP_ADULTS', 1)
    quote_factory = mocker.patch('app.agent.course_graph.MyRealTripQuoteProvider', return_value=quotes)
    maps_factory = mocker.patch('app.agent.course_graph.VerifiedMapsProvider', return_value=provider)
    mocker.patch('app.agent.course_graph.CourseHistory', return_value=history)
    events = [event async for event in stream_course_generation(request_data)]
    complete = [payload for event, payload in events if event == 'complete']
    assert len(complete) == 1 and events[-1][0] == 'complete'
    assert complete[0]['course']['totalCost'] == 365000 + 35000
    assert quote_factory.call_args.kwargs['origin_airport'] == 'ICN'
    assert quote_factory.call_args.kwargs['adults'] == 1
    assert maps_factory.call_args.kwargs['language'] == request_data.language
    quoted = quotes.quote.await_args.args[0]
    assert quoted.start_date == quoted.end_date == date(2026, 10, 5)
    quotes.__aexit__.assert_awaited_once()
    provider.__aexit__.assert_awaited_once()
    llm.assert_awaited_once()


@pytest.mark.asyncio
async def test_optional_quote_configuration_error_keeps_verified_course(
    request_data, graph_dependencies, mocker,
):
    """An invalid quote-only setting cannot discard verified places and routes."""
    provider, history, llm, _, _ = graph_dependencies
    provider.__aenter__ = AsyncMock(return_value=provider)
    provider.__aexit__ = AsyncMock(return_value=False)
    mocker.patch('app.agent.course_graph.VerifiedMapsProvider', return_value=provider)
    mocker.patch('app.agent.course_graph.CourseHistory', return_value=history)
    mocker.patch('app.agent.course_graph.MyRealTripQuoteProvider', side_effect=ValueError('Invalid origin'))
    events = [event async for event in stream_course_generation(request_data)]
    complete = [payload for event, payload in events if event == 'complete']
    assert len(complete) == 1 and events[-1][0] == 'complete'
    assert complete[0]['course']['totalCost'] == 35000
    assert len(complete[0]['course']['itinerary']['days'][0]['stops']) == 5
    assert any(event == 'progress' and '항공' in payload['message'] and '견적은 포함되지 않았습니다' in payload['message'] for event, payload in events)
    provider.__aexit__.assert_awaited_once()
    llm.assert_awaited_once()

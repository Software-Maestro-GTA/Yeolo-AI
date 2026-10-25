"""Verify airport endpoints without inventing flights or replacing daytime visits.

Maps and quote calls stay offline; graph scheduling, cost totals, diversity
history, localized fallback copy and public serialization run for real.
"""

import asyncio
from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest

from app.agent.course_graph import build_course_graph
from app.agent.tools.verified_maps import (
    Destination,
    InternalPlaceSchema,
    InternalTransportSchema,
    NoRouteError,
    VerifiedPlace,
)
from app.schemas.course import (
    CourseSchema,
    DayItinerarySchema,
    ItinerarySchema,
    StopSchema,
)
from app.services import course_localization
from app.services.course_airports import attach_airports
from app.services.course_history import history_key
from app.services.myrealtrip_quotes import (
    AirportIdentity,
    LodgingIdentity,
    MyRealTripQuoteProvider,
    QuoteItem,
    TripQuotes,
)
from tests.test_course_contract_v2 import add_second_day, use_overseas_destination
from tests.test_course_graph import graph_dependencies as _graph_dependencies
from tests.test_course_graph import request_data as _request_data

graph_dependencies = _graph_dependencies
request_data = _request_data


def airport_identity():
    """Use an actual supplier airport name rather than an inferred city airport."""
    return AirportIdentity('NRT', '나리타 국제공항', 'JP', 'Narita International Airport')


def verified_airport():
    """An airport may legitimately sit outside the tourist destination viewport."""
    return VerifiedPlace(InternalPlaceSchema(
        placeId='places/verified-nrt', placeName='나리타 국제공항',
        category='international_airport', providerTypes=['international_airport', 'airport'],
        address='Japan, Narita', latitude=35.772, longitude=140.392,
    ))


@pytest.fixture
def airport_case(request_data, mocker):
    """Three full days already include a whole round-trip flight cost in totalCost."""
    request_data.tripCondition.destinationCountry = '일본'
    request_data.tripCondition.destinationCity = '도쿄'
    request_data.tripCondition.totalDays = 3
    start = date.fromisoformat(request_data.tripCondition.startDate)
    days = []
    for day_number in range(1, 4):
        stops = [StopSchema(
            sequence=index + 1, stayMinutes=60, memo='방문 팁', reason='취향에 맞는 경험', cost=1000,
            place=InternalPlaceSchema(
                placeId=f'places/day-{day_number}-visit-{index}',
                placeName=f'{day_number}일차 {index + 1}번 장소', category='museum',
                address='Japan, Tokyo', latitude=35.68 + index * .001, longitude=139.76,
            ),
            transportToNext=InternalTransportSchema(
                type='none' if index == 4 else 'walking',
                distance=None if index == 4 else 500,
                minutes=None if index == 4 else 10,
                cost=None if index == 4 else 0,
                memo=None if index == 4 else '확인된 보행 안내',
            ),
        ) for index in range(5)]
        days.append(DayItinerarySchema(
            day=day_number, date=(start + timedelta(days=day_number - 1)).isoformat(), stops=stops,
        ))
    course = CourseSchema(
        title='도쿄 3일 여행', destinationCountry='일본', destinationCity='도쿄',
        startDate=start.isoformat(), totalDays=3, totalCost=415000,
        itinerary=ItinerarySchema(days=days),
    )
    destination = Destination('JP', 35.5, 139.5, 35.9, 139.95)
    quotes = TripQuotes(
        QuoteItem('available', 400000, airport_identity=airport_identity()),
        QuoteItem('unavailable', reason='no_options'),
    )
    provider = mocker.Mock()
    provider.search_airport = AsyncMock(return_value=verified_airport())
    provider.route = AsyncMock(return_value=InternalTransportSchema(
        type='transit', distance=67000, minutes=75, cost=2300, memo='공항철도를 이용하세요.',
    ))
    return request_data, course, destination, quotes, provider


@pytest.mark.asyncio
async def test_arrival_and_departure_surround_all_visits_and_charge_only_transfers(airport_case):
    """Keep five visits per day and flight identity without charging airfare twice."""
    request, course, destination, quotes, provider = airport_case
    before = course.model_dump()
    result = await attach_airports(request, course, destination, quotes, provider)

    assert [len(day.stops) for day in result.itinerary.days] == [6, 5, 6]
    assert course.model_dump() == before
    assert not destination.contains(verified_airport().place.latitude, verified_airport().place.longitude)
    arrival, departure = result.itinerary.days[0].stops[0], result.itinerary.days[-1].stops[-1]
    assert arrival.place.placeId == departure.place.placeId == 'places/verified-nrt'
    assert (arrival.stayMinutes, departure.stayMinutes) == (60, 120)
    assert arrival.cost == departure.cost == 200000
    assert arrival.cost + departure.cost == quotes.flight.amount_krw
    assert departure.transportToNext.type == 'none'
    assert arrival.transportToNext.minutes == result.itinerary.days[-1].stops[-2].transportToNext.minutes == 75
    assert result.totalCost == course.totalCost + 2 * 2300
    for original, updated in zip(course.itinerary.days, result.itinerary.days, strict=True):
        assert [stop.place.placeId for stop in updated.stops if stop.place.placeId != 'places/verified-nrt'] == [stop.place.placeId for stop in original.stops]
        assert [stop.sequence for stop in updated.stops] == list(range(1, len(updated.stops) + 1))
    assert result.itinerary.days[1].model_dump() == before['itinerary']['days'][1]
    provider.search_airport.assert_awaited_once_with(quotes.flight.airport_identity, destination)
    assert {(call.args[0].place.placeId, call.args[1].place.placeId) for call in provider.route.await_args_list} == {
        ('places/verified-nrt', 'places/day-1-visit-0'),
        ('places/day-3-visit-4', 'places/verified-nrt'),
    }
    assert all(call.kwargs.get('departure_time') is None for call in provider.route.await_args_list)
    public = result.model_dump(by_alias=True, context={'public_response': True})
    for stop in (public['itinerary']['days'][0]['stops'][0], public['itinerary']['days'][-1]['stops'][-1]):
        assert stop['place']['category'] == 'AIRPORT'
        assert stop['place']['googlePlaceId'] == 'verified-nrt'
        assert 'arrivalTime' not in stop


@pytest.mark.asyncio
async def test_one_day_flight_places_same_airport_at_both_ends(airport_case):
    """Endpoint repetition is intentional, including a same-day return flight."""
    request, course, destination, quotes, provider = airport_case
    request.tripCondition.totalDays = course.totalDays = 1
    course.itinerary.days = course.itinerary.days[:1]
    result = await attach_airports(request, course, destination, quotes, provider)
    stops = result.itinerary.days[0].stops
    assert len(stops) == 7
    assert stops[0].place.placeId == stops[-1].place.placeId == 'places/verified-nrt'
    assert stops[0].cost == stops[-1].cost == 200000
    assert [stop.place.placeId for stop in stops[1:-1]] == [stop.place.placeId for stop in course.itinerary.days[0].stops]
    assert [stop.sequence for stop in stops] == list(range(1, 8))
    assert result.totalCost == course.totalCost + 4600


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['ground', 'rail', 'none'])
async def test_non_air_trips_never_search_or_add_airports(airport_case, mode):
    """The feature cannot turn a ground itinerary into an invented flight trip."""
    request, course, destination, _, provider = airport_case
    quotes = TripQuotes(QuoteItem('not_applicable'), QuoteItem('not_applicable'), transport_mode=mode)
    result = await attach_airports(request, course, destination, quotes, provider)
    assert result is course
    provider.search_airport.assert_not_awaited()
    provider.route.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('quotes', [None, TripQuotes(QuoteItem('available', 400000), QuoteItem('unavailable'))])
async def test_price_without_airport_identity_does_not_invent_endpoints(airport_case, quotes):
    """A round-trip amount alone does not establish which airport serves the quote."""
    request, course, destination, _, provider = airport_case
    result = await attach_airports(request, course, destination, quotes, provider)
    assert result is course
    provider.search_airport.assert_not_awaited()
    provider.route.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('status', ['available', 'estimated'])
async def test_round_trip_fare_splits_without_losing_odd_won_or_recharging_total(airport_case, status):
    """Displayed outbound/return allocations preserve quoted or estimated whole-trip fares."""
    request, course, destination, _, provider = airport_case
    quotes = TripQuotes(
        QuoteItem(status, 400001, source='offline-test-price', reference_date='2026-10-19', airport_identity=airport_identity()),
        QuoteItem('unavailable'),
    )
    course.totalCost = 415001
    result = await attach_airports(request, course, destination, quotes, provider)
    assert [len(day.stops) for day in result.itinerary.days] == [6, 5, 6]
    arrival, departure = result.itinerary.days[0].stops[0], result.itinerary.days[-1].stops[-1]
    assert (arrival.cost, departure.cost) == (200001, 200000)
    assert arrival.cost + departure.cost == quotes.flight.amount_krw
    assert result.totalCost == course.totalCost + 4600
    public = result.model_dump(by_alias=True, context={'public_response': True})
    assert public['itinerary']['days'][0]['stops'][0]['cost'] == 200001
    assert public['itinerary']['days'][-1]['stops'][-1]['cost'] == 200000


@pytest.mark.asyncio
@pytest.mark.parametrize('result,failure', [
    (None, None),
    (None, RuntimeError('offline Maps failure')),
    (VerifiedPlace(InternalPlaceSchema(placeId='places/not-an-airport', placeName='철도역', category='train_station', latitude=35.7, longitude=139.8)), None),
])
async def test_unverified_airport_preserves_full_course(airport_case, result, failure):
    """A missing, failed or non-airport Maps match must not invalidate the itinerary."""
    request, course, destination, quotes, provider = airport_case
    provider.search_airport.return_value = result
    provider.search_airport.side_effect = failure
    updated = await attach_airports(request, course, destination, quotes, provider)
    assert updated is course
    provider.route.assert_not_awaited()


@pytest.mark.asyncio
async def test_long_measured_airport_transfer_is_preserved(airport_case):
    """A genuine airport journey has no fixed route-duration limit."""
    request, course, destination, quotes, provider = airport_case
    provider.route.return_value = InternalTransportSchema(type='transit', distance=95000, minutes=110, cost=4200)
    result = await attach_airports(request, course, destination, quotes, provider)
    assert [len(day.stops) for day in result.itinerary.days] == [6, 5, 6]
    routes = [result.itinerary.days[0].stops[0].transportToNext, result.itinerary.days[-1].stops[-2].transportToNext]
    assert all((route.type, route.distance, route.minutes) == ('transit', 95000, 110) for route in routes)
    assert result.totalCost == course.totalCost + 8400


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [NoRouteError(), RuntimeError('offline route failure')])
async def test_failed_transfer_retains_airport_with_unknown_metrics(airport_case, failure):
    """Missing transfer data is explicit and does not become a fabricated walking time."""
    request, course, destination, quotes, provider = airport_case
    provider.route.side_effect = failure
    result = await attach_airports(request, course, destination, quotes, provider)
    assert [len(day.stops) for day in result.itinerary.days] == [6, 5, 6]
    for route in (result.itinerary.days[0].stops[0].transportToNext, result.itinerary.days[-1].stops[-2].transportToNext):
        assert route.type == 'unknown'
        assert route.distance is route.minutes is route.memo is getattr(route, 'cost', None) is None
    assert result.itinerary.days[0].stops[0].cost == result.itinerary.days[-1].stops[-1].cost == 200000
    assert result.totalCost == course.totalCost


@pytest.mark.asyncio
async def test_optional_deadline_expired_before_lookup_preserves_course(airport_case):
    """The endpoint extension must not delay an already completed course indefinitely."""
    request, course, destination, quotes, provider = airport_case
    result = await attach_airports(request, course, destination, quotes, provider, deadline=asyncio.get_running_loop().time() - 1)
    assert result is course
    provider.search_airport.assert_not_awaited()
    provider.route.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_transfer_budget_retains_previously_verified_airport(airport_case):
    """A late optional stage cannot discard a real airport found before scheduling."""
    request, course, destination, quotes, provider = airport_case
    before = course.model_dump()
    result = await attach_airports(
        request, course, destination, quotes, provider,
        deadline=asyncio.get_running_loop().time() - 1,
        verified_airport=verified_airport(), lookup=False,
    )

    assert [len(day.stops) for day in result.itinerary.days] == [6, 5, 6]
    assert result.itinerary.days[0].stops[0].place.placeId == 'places/verified-nrt'
    assert result.itinerary.days[-1].stops[-1].place.placeId == 'places/verified-nrt'
    assert result.itinerary.days[0].stops[0].cost == result.itinerary.days[-1].stops[-1].cost == 200000
    for route in (result.itinerary.days[0].stops[0].transportToNext,
                  result.itinerary.days[-1].stops[-2].transportToNext):
        assert route.type == 'unknown'
        assert route.distance is route.minutes is route.memo is getattr(route, 'cost', None) is None
    assert result.totalCost == course.totalCost
    assert course.model_dump() == before
    provider.search_airport.assert_not_awaited()
    provider.route.assert_not_awaited()


@pytest.mark.asyncio
async def test_airport_omission_has_reason_without_supplier_error_text(airport_case, caplog):
    """Diagnostics explain a skipped airport without logging upstream secrets or names."""
    request, course, destination, quotes, provider = airport_case
    provider.search_airport.side_effect = RuntimeError('secret-key=do-not-log')
    with caplog.at_level('INFO', logger='app.services.course_airports'):
        result = await attach_airports(request, course, destination, quotes, provider)
    assert result is course
    assert 'reason=attachment_error' in caplog.text
    assert 'secret-key' not in caplog.text


@pytest.mark.asyncio
async def test_repeated_airport_attachment_is_idempotent(airport_case):
    """Re-entering an optional graph node must not add another airport or fare."""
    request, course, destination, quotes, provider = airport_case
    result = await attach_airports(request, course, destination, quotes, provider)
    before = result.model_dump()
    repeated = await attach_airports(request, result, destination, quotes, provider)
    assert repeated.model_dump() == before
    assert repeated.totalCost == course.totalCost + 4600
    assert repeated.itinerary.days[0].stops[0].cost + repeated.itinerary.days[-1].stops[-1].cost == quotes.flight.amount_krw
    provider.search_airport.assert_awaited_once()
    assert provider.route.await_count == 2


@pytest.mark.asyncio
async def test_airport_route_timeout_joins_children_and_preserves_endpoints(airport_case):
    """A route timeout yields unknown routes and leaves no airport transfer task running."""
    request, course, destination, quotes, provider = airport_case
    finished = []

    async def stalled_route(origin, target, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            finished.append((origin.place.placeId, target.place.placeId))

    provider.route.side_effect = stalled_route
    result = await asyncio.wait_for(attach_airports(
        request, course, destination, quotes, provider,
        deadline=asyncio.get_running_loop().time() + .1,
    ), timeout=1)
    assert [len(day.stops) for day in result.itinerary.days] == [6, 5, 6]
    assert len(finished) == provider.route.await_count == 2
    assert result.itinerary.days[0].stops[0].transportToNext.type == result.itinerary.days[-1].stops[-2].transportToNext.type == 'unknown'
    assert result.itinerary.days[0].stops[0].cost == result.itinerary.days[-1].stops[-1].cost == 200000
    assert result.totalCost == course.totalCost


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['search_airport', 'route'])
async def test_airport_external_cancellation_is_not_hidden(airport_case, boundary):
    """Client cancellation propagates instead of reporting a successful fallback."""
    request, course, destination, quotes, provider = airport_case
    getattr(provider, boundary).side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await attach_airports(request, course, destination, quotes, provider)


@pytest.mark.asyncio
async def test_caller_cancellation_joins_both_inflight_airport_transfers(airport_case):
    """Cancellation cannot leak background route calls or mutate the existing course."""
    request, course, destination, quotes, provider = airport_case
    before = course.model_dump()
    entered = asyncio.Event()
    started = []
    finished = []

    async def stalled_route(origin, target, **kwargs):
        pair = (origin.place.placeId, target.place.placeId)
        started.append(pair)
        if len(started) == 2:
            entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.append(pair)

    provider.route.side_effect = stalled_route
    task = asyncio.create_task(attach_airports(request, course, destination, quotes, provider))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert len(started) == len(finished) == 2
    assert set(started) == set(finished)
    assert course.model_dump() == before


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ko', 'en', 'ja', 'zh-CN', 'zh-TW', 'fr', 'es', 'de', 'pt'])
async def test_airport_copy_stays_empty_without_model_generation(airport_case, language):
    """Localization failure must not create airport or attraction template copy."""
    request, course, destination, quotes, provider = airport_case
    request.language = language
    request.tripCondition.totalDays = course.totalDays = 1
    course.itinerary.days = course.itinerary.days[:1]
    attached = await attach_airports(request, course, destination, quotes, provider)
    result = await course_localization.localize_course(request, attached, timeout_seconds=0)
    first, last = result.itinerary.days[0].stops[0], result.itinerary.days[0].stops[-1]
    assert first.reason == first.memo == last.reason == last.memo == ""
    assert first.place.placeId == last.place.placeId == 'places/verified-nrt'
    assert result.totalCost == attached.totalCost


@pytest.mark.asyncio
async def test_real_graph_adds_airports_after_hotels_without_polluting_diversity_history(
    request_data, graph_dependencies, mocker,
):
    """Airport endpoints and overnight stays cannot replace five genuine day visits."""
    provider, history, llm, draft, places = graph_dependencies
    use_overseas_destination(request_data, provider)
    add_second_day(request_data, draft, places)
    provider.search_airport = AsyncMock(return_value=verified_airport())
    provider.search_lodging = AsyncMock(return_value=VerifiedPlace(InternalPlaceSchema(
        placeId='places/verified-hotel', placeName='도쿄 숙소', category='lodging',
        providerTypes=['lodging'], address='Japan, Tokyo', latitude=37.552, longitude=126.98,
    )))
    quotes = mocker.Mock(spec=MyRealTripQuoteProvider)
    quotes.quote = AsyncMock(return_value=TripQuotes(
        QuoteItem('available', 400000, airport_identity=airport_identity()),
        QuoteItem('available', 100001, lodging_identity=LodgingIdentity(12345, '도쿄 숙소')),
    ))

    async def route(origin, target, *args, **kwargs):
        ids = {origin.place.placeId, target.place.placeId}
        fare = 2300 if 'places/verified-nrt' in ids else 1000 if 'places/verified-hotel' in ids else 0
        return InternalTransportSchema(type='transit', distance=1000, minutes=12, cost=fare, memo='확인된 이동 안내')

    provider.route.side_effect = route
    localized_ids = []

    async def localize(request, course, **kwargs):
        localized_ids.extend(stop.place.placeId for day in course.itinerary.days for stop in day.stops)
        return course

    localizer = mocker.patch('app.agent.course_nodes.localize_course', side_effect=localize)
    updates = [update async for update in build_course_graph(provider, history, quotes=quotes).astream(
        {'request': request_data, 'attempt': 0}, stream_mode='updates',
    )]
    stages = [node for update in updates for node in update]
    assert stages.index('finalize') < stages.index('attach_lodging') < stages.index('attach_airports') < stages.index('translate_display') < stages.index('generate_place_copy') < stages.index('finalize_course')
    result = updates[-1]['finalize_course']['course']
    assert [len(day.stops) for day in result.itinerary.days] == [7, 6]
    assert result.itinerary.days[0].stops[0].place.placeId == result.itinerary.days[-1].stops[-1].place.placeId == 'places/verified-nrt'
    assert result.itinerary.days[0].stops[-1].place.placeId == 'places/verified-hotel'
    visit_ids = [stop.place.placeId for day in result.itinerary.days for stop in day.stops if stop.place.placeId not in {'places/verified-nrt', 'places/verified-hotel'}]
    assert len(visit_ids) == len(set(visit_ids)) == 10
    assert result.totalCost == 70000 + 400000 + 100001 + 1000 + 4600
    assert result.itinerary.days[0].stops[0].cost == result.itinerary.days[-1].stops[-1].cost == 200000
    assert await history.recent(history_key(request_data)) == [set(visit_ids)]
    assert all(not ({'places/verified-nrt', 'places/verified-hotel'} & set(entry['attraction_ids'])) for entry in await history.recent_profiles(history_key(request_data)))
    assert localized_ids.count('places/verified-nrt') == 2
    assert localized_ids.count('places/verified-hotel') == 1
    provider.search_airport.assert_awaited_once()
    provider.search_lodging.assert_awaited_once()
    assert provider.search.await_count == 10
    llm.assert_awaited_once()
    localizer.assert_awaited_once()

"""Check verified overnight lodging without reducing visits or duplicating costs.

Maps and quote boundaries are offline. The graph regression keeps scheduling,
history storage, node ordering and public serialization real.
"""

import asyncio
from dataclasses import replace
from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest

from app.agent.course_graph import build_course_graph
from app.agent.course_state import DraftDay, ScheduledDaySchema, ScheduledStopSchema
from app.agent.tools.verified_maps import (
    Destination,
    InternalPlaceSchema,
    InternalTransportSchema,
    NoRouteError,
    VerifiedPlace,
)
from app.schemas.course import CourseSchema, ItinerarySchema
from app.services.course_history import history_key
from app.services.course_lodging import attach_lodging
from app.services.myrealtrip_quotes import (
    LodgingIdentity,
    MyRealTripQuoteProvider,
    QuoteItem,
    TripQuotes,
)
from tests.test_course_contract_v2 import add_second_day
from tests.test_course_graph import graph_dependencies as _graph_dependencies
from tests.test_course_graph import request_data as _request_data

graph_dependencies = _graph_dependencies
request_data = _request_data


def hotel_quote(amount=100001):
    """Supply one real whole-stay price and its exact matched property identity."""
    return TripQuotes(
        QuoteItem('not_applicable'),
        QuoteItem('available', amount, lodging_identity=LodgingIdentity(12345, '서울 숙소')),
        transport_mode='none',
    )


def verified_hotel():
    """Return only Essentials-style Maps identity, coordinates and lodging type."""
    return VerifiedPlace(InternalPlaceSchema(
        placeId='places/verified-hotel', placeName='서울 숙소', category='lodging',
        providerTypes=['lodging'], address='대한민국 서울', latitude=37.552, longitude=126.98,
    ))


@pytest.fixture
def lodging_case(request_data, mocker):
    """Three full sightseeing days already include the whole-stay quoted price."""
    request_data.tripCondition.totalDays = 3
    start = date.fromisoformat(request_data.tripCondition.startDate)
    days = []
    for day_number in range(1, 4):
        stops = [
            ScheduledStopSchema(
                sequence=index + 1, arrivalTime=f'{9 + index * 2:02}:00',
                stayMinutes=60, memo='방문 팁', reason='취향에 맞는 경험', cost=1000,
                place=InternalPlaceSchema(
                    placeId=f'places/day-{day_number}-visit-{index}',
                    placeName=f'{day_number}일차 {index + 1}번 장소', category='museum',
                    address='대한민국 서울', latitude=37.55 + index * .001, longitude=126.98,
                ),
                transportToNext=InternalTransportSchema(
                    type='none' if index == 4 else 'walking',
                    distance=None if index == 4 else 500,
                    minutes=None if index == 4 else 10,
                    cost=None if index == 4 else 0,
                    memo=None if index == 4 else '확인된 보행 안내',
                ),
            ) for index in range(5)
        ]
        days.append(ScheduledDaySchema(
            day=day_number, date=(start + timedelta(days=day_number - 1)).isoformat(), stops=stops,
        ))
    course = CourseSchema(
        title='서울 3일 여행', destinationCountry='대한민국', destinationCity='서울',
        startDate=start.isoformat(), totalDays=3, totalCost=115001,
        itinerary=ItinerarySchema(days=days),
    )
    destination = Destination('KR', 37.3, 126.7, 37.8, 127.3)
    provider = mocker.Mock()
    provider.search_lodging = AsyncMock(return_value=verified_hotel())
    provider.route = AsyncMock(return_value=InternalTransportSchema(
        type='transit', distance=2300, minutes=18, cost=1400, memo='숙소로 가는 버스를 이용하세요.',
    ))
    return request_data, course, destination, hotel_quote(), provider


@pytest.mark.asyncio
async def test_verified_lodging_follows_five_visits_only_on_overnight_days(lodging_case):
    """Keep all visits, use one property, split whole-stay cost and add only fares."""
    request, original, destination, quotes, provider = lodging_case
    before = original.model_dump()
    result = await attach_lodging(request, original, destination, quotes, provider)

    assert [len(day.stops) for day in result.itinerary.days] == [6, 6, 5]
    assert original.model_dump() == before
    for original_day, updated_day in zip(original.itinerary.days, result.itinerary.days, strict=True):
        assert [stop.place.placeId for stop in updated_day.stops[:5]] == [
            stop.place.placeId for stop in original_day.stops
        ]
        assert [stop.sequence for stop in updated_day.stops] == list(range(1, len(updated_day.stops) + 1))
    nights = [day.stops[-1] for day in result.itinerary.days[:-1]]
    assert {stop.place.placeId for stop in nights} == {'places/verified-hotel'}
    assert sorted(stop.cost for stop in nights) == [50000, 50001]
    assert sum(stop.cost for stop in nights) == quotes.lodging.amount_krw
    assert all(stop.stayMinutes == 30 for stop in nights)
    assert all(stop.transportToNext.type == 'none' for stop in nights)
    assert result.itinerary.days[-1].model_dump() == original.itinerary.days[-1].model_dump()
    assert result.totalCost == original.totalCost + 2 * 1400
    provider.search_lodging.assert_awaited_once()
    searched, region = provider.search_lodging.await_args.args
    assert searched.name == quotes.lodging.lodging_identity.name
    assert searched.english_name == '' and searched.meal == 'none'
    assert region == destination
    assert provider.route.await_count == 2
    for call, day in zip(provider.route.await_args_list, original.itinerary.days, strict=False):
        assert call.args[0].place.placeId == day.stops[-1].place.placeId
        assert call.args[1].place.placeId == 'places/verified-hotel'
    public = result.model_dump(context={'public_response': True})
    assert [day['stops'][-1]['place']['category'] for day in public['itinerary']['days'][:-1]] == ['LODGING', 'LODGING']
    assert public['itinerary']['days'][0]['stops'][-1]['place']['placeId'] == 'verified-hotel'


@pytest.mark.asyncio
async def test_same_day_trip_has_no_lodging_lookups(lodging_case):
    """A supplied whole-stay quote cannot create a night on the checkout date."""
    request, course, destination, quotes, provider = lodging_case
    request.tripCondition.totalDays = course.totalDays = 1
    course.itinerary.days = course.itinerary.days[:1]
    result = await attach_lodging(request, course, destination, quotes, provider)
    assert result.model_dump() == course.model_dump()
    provider.search_lodging.assert_not_awaited()
    provider.route.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('quote', [
    None,
    QuoteItem('available', 100000),
    QuoteItem('estimated', 100000, source='offline-baseline', reference_date='2026-10-19'),
    QuoteItem('unavailable', reason='no_options'),
])
async def test_unidentified_lodging_keeps_validated_course(lodging_case, quote):
    """A city-level price must never manufacture or price an arbitrary hotel."""
    request, course, destination, _, provider = lodging_case
    quotes = None if quote is None else TripQuotes(QuoteItem('not_applicable'), quote)
    result = await attach_lodging(request, course, destination, quotes, provider)
    assert result.model_dump() == course.model_dump()
    provider.search_lodging.assert_not_awaited()
    provider.route.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [None, RuntimeError('offline Maps failure')])
async def test_unverified_hotel_does_not_discard_course(lodging_case, failure):
    """Hotel identity lookup is optional and cannot invalidate a full itinerary."""
    request, course, destination, quotes, provider = lodging_case
    provider.search_lodging.return_value = None
    provider.search_lodging.side_effect = failure
    result = await attach_lodging(request, course, destination, quotes, provider)
    assert result.model_dump() == course.model_dump()
    provider.route.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('route_error', [NoRouteError(), RuntimeError('offline route failure')])
async def test_failed_return_route_preserves_hotel_with_unknown_transfer(lodging_case, route_error):
    """Lookup errors disclose missing metrics instead of removing the itinerary."""
    request, course, destination, quotes, provider = lodging_case
    provider.route.side_effect = route_error
    result = await attach_lodging(request, course, destination, quotes, provider)
    assert [len(day.stops) for day in result.itinerary.days] == [6, 6, 5]
    assert result.totalCost == course.totalCost
    for day in result.itinerary.days[:-1]:
        transfer = day.stops[-2].transportToNext
        assert transfer.type == 'unknown'
        assert transfer.distance is transfer.minutes is getattr(transfer, 'cost', None) is None
        assert day.stops[-1].transportToNext.type == 'none'


@pytest.mark.asyncio
async def test_known_impractical_return_does_not_masquerade_as_unknown(lodging_case):
    """An actual 91-minute journey cannot be hidden by attaching an unknown route."""
    request, course, destination, quotes, provider = lodging_case

    async def route(origin, hotel, *, mode, **kwargs):
        if origin.place.placeId == course.itinerary.days[0].stops[-1].place.placeId:
            return InternalTransportSchema(type='transit', distance=1000, minutes=10, cost=1400)
        if mode == 'transit':
            return InternalTransportSchema(type='transit', distance=65000, minutes=91, cost=5000)
        raise NoRouteError()

    provider.route.side_effect = route
    result = await attach_lodging(request, course, destination, quotes, provider)
    assert result.model_dump() == course.model_dump()


@pytest.mark.asyncio
async def test_overlong_walk_uses_verified_transit_alternative(lodging_case):
    """A rejected walking duration must not hide a valid later transport mode."""
    request, course, destination, quotes, provider = lodging_case
    # Reuse close fixture points while selecting the overseas walking-first policy.
    destination = replace(destination, country_code='JP')

    async def route(origin, hotel, *, mode, **kwargs):
        if mode == 'walking':
            return InternalTransportSchema(type='walking', distance=8000, minutes=100, cost=0)
        if mode == 'transit':
            return InternalTransportSchema(type='transit', distance=3000, minutes=10, cost=500)
        raise NoRouteError()

    provider.route.side_effect = route
    result = await attach_lodging(request, course, destination, quotes, provider)
    assert [len(day.stops) for day in result.itinerary.days] == [6, 6, 5]
    for day in result.itinerary.days[:-1]:
        transfer = day.stops[-2].transportToNext
        assert (transfer.type, transfer.distance, transfer.minutes, transfer.cost) == ('transit', 3000, 10, 500)
    modes = [call.kwargs['mode'] for call in provider.route.await_args_list]
    assert modes.count('walking') == modes.count('transit') == 2
    assert 'taxi' not in modes
    assert result.totalCost == course.totalCost + 1000


@pytest.mark.asyncio
async def test_expired_optional_budget_keeps_course_without_starting_lookup(lodging_case):
    """A completed daytime course must remain available after the shared deadline."""
    request, course, destination, quotes, provider = lodging_case
    result = await attach_lodging(
        request, course, destination, quotes, provider,
        deadline=asyncio.get_running_loop().time() - 1,
    )
    assert result.model_dump() == course.model_dump()
    provider.search_lodging.assert_not_awaited()
    provider.route.assert_not_awaited()


@pytest.mark.asyncio
async def test_repeated_attachment_does_not_duplicate_lodging_or_return_fares(lodging_case):
    """Re-entering the optional step preserves already attached hotel nights."""
    request, course, destination, quotes, provider = lodging_case
    result = await attach_lodging(request, course, destination, quotes, provider)
    snapshot = result.model_dump()
    repeated = await attach_lodging(request, result, destination, quotes, provider)
    assert repeated.model_dump() == snapshot
    assert repeated.totalCost == course.totalCost + 2800
    provider.search_lodging.assert_awaited_once()
    assert provider.route.await_count == 2


@pytest.mark.asyncio
async def test_return_route_timeout_joins_children_and_keeps_verified_hotel(lodging_case):
    """A stuck return lookup ends as unknown while all started tasks are cleaned up."""
    request, course, destination, quotes, provider = lodging_case
    finished = []

    async def stalled_route(origin, *args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            finished.append(origin.place.placeId)

    provider.route.side_effect = stalled_route
    result = await asyncio.wait_for(attach_lodging(
        request, course, destination, quotes, provider,
        deadline=asyncio.get_running_loop().time() + .1,
    ), timeout=1)
    assert [len(day.stops) for day in result.itinerary.days] == [6, 6, 5]
    assert len(finished) == provider.route.await_count == 2
    assert result.totalCost == course.totalCost
    assert all(day.stops[-2].transportToNext.type == 'unknown' for day in result.itinerary.days[:-1])


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['search_lodging', 'route'])
async def test_lodging_external_cancellation_propagates(lodging_case, boundary):
    """Client cancellation must stop work rather than pretending lookup failed."""
    request, course, destination, quotes, provider = lodging_case
    getattr(provider, boundary).side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await attach_lodging(request, course, destination, quotes, provider)


@pytest.mark.asyncio
async def test_graph_localizes_lodging_but_keeps_it_out_of_diversity_history(
    request_data, graph_dependencies, mocker,
):
    """Hotel repetition is deliberate and must not replace visits or affect novelty."""
    provider, history, llm, draft, places = graph_dependencies
    add_second_day(request_data, draft, places)
    request_data.tripCondition.totalDays = 3
    third_candidates = []
    for candidate in draft.days[0].candidates:
        other = candidate.model_copy(update={'name': candidate.name + '-3일차'})
        original = places[candidate.name]
        places[other.name] = VerifiedPlace(original.place.model_copy(update={
            'placeId': original.place.placeId + '-day3', 'placeName': other.name,
        }), periods=original.periods)
        third_candidates.append(other)
    draft.days.append(DraftDay(candidates=third_candidates))
    provider.search_lodging = AsyncMock(return_value=verified_hotel())

    async def route(origin, destination, *args, **kwargs):
        fare = 1250 if destination.place.placeId == 'places/verified-hotel' else 0
        return InternalTransportSchema(type='transit', distance=1000, minutes=12, cost=fare, memo='이동 안내')

    provider.route.side_effect = route
    quotes = mocker.Mock(spec=MyRealTripQuoteProvider)
    quotes.quote = AsyncMock(return_value=hotel_quote())
    localized_ids = []

    async def localize(request, course, **kwargs):
        localized_ids.extend(stop.place.placeId for day in course.itinerary.days for stop in day.stops)
        return course

    localizer = mocker.patch('app.agent.course_nodes.localize_course', side_effect=localize)
    updates = [update async for update in build_course_graph(provider, history, quotes=quotes).astream(
        {'request': request_data, 'attempt': 0}, stream_mode='updates',
    )]
    stages = [node for update in updates for node in update]
    assert stages.index('finalize') < stages.index('attach_lodging') < stages.index('localize_display') < stages.index('finalize_course')
    result = updates[-1]['finalize_course']['course']
    assert [len(day.stops) for day in result.itinerary.days] == [6, 6, 5]
    visit_ids = [stop.place.placeId for day in result.itinerary.days for stop in day.stops if stop.place.placeId != 'places/verified-hotel']
    assert len(visit_ids) == len(set(visit_ids)) == 15
    assert localized_ids.count('places/verified-hotel') == 2
    assert result.totalCost == 105000 + 100001 + 2 * 1250
    recorded = await history.recent(history_key(request_data))
    assert recorded == [set(visit_ids)]
    assert all('places/verified-hotel' not in entry['attraction_ids'] for entry in await history.recent_profiles(history_key(request_data)))
    assert provider.search.await_count == 15
    provider.search_lodging.assert_awaited_once()
    llm.assert_awaited_once()
    localizer.assert_awaited_once()

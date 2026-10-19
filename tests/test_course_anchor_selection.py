"""Verify jointly drafted airports/hotels with offline provider boundaries.

The selection tests protect bounded optional lookups and price identity. The
graph case exercises real scheduling, attachment, totals and diversity history.
"""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.agent.course_graph import build_course_graph
from app.agent.course_nodes import CourseGraphNodes
from app.agent.course_state import DraftDay
from app.agent.tools.verified_maps import (
    Destination,
    InternalPlaceSchema,
    InternalTransportSchema,
    VerifiedPlace,
)
from app.core.config import settings
from app.schemas.course_anchors import AnchorCandidates
from app.services.course_anchor_selection import resolve_course_anchors
from app.services.course_history import history_key
from app.services.myrealtrip_quotes import (
    LodgingIdentity,
    MyRealTripQuoteProvider,
    QuoteItem,
    TripQuotes,
)
from app.services.travel_cost_baselines import baseline_trip_quotes
from tests.test_course_airports import airport_identity, verified_airport
from tests.test_course_contract_v2 import add_second_day, use_overseas_destination
from tests.test_course_graph import graph_dependencies as _graph_dependencies
from tests.test_course_graph import request_data as _request_data

graph_dependencies = _graph_dependencies
request_data = _request_data


def generated_candidates():
    """Offer only names and IATA hints, without LLM prices or coordinates."""
    return AnchorCandidates.model_validate({
        'airports': [
            {'name': '하네다 공항', 'english_name': 'Haneda Airport', 'iata_code': 'HND'},
            {'name': '나리타 국제공항', 'english_name': 'Narita International Airport', 'iata_code': 'NRT'},
        ],
        'lodgings': [
            {'name': '도쿄 후보 호텔', 'english_name': 'Tokyo Candidate Hotel'},
            {'name': '도쿄 대안 호텔', 'english_name': 'Tokyo Alternative Hotel'},
        ],
    })


def candidate_hotel():
    """Return a Maps-verified property without claiming rooms are available."""
    return VerifiedPlace(InternalPlaceSchema(
        placeId='places/generated-hotel', placeName='도쿄 후보 호텔', category='hotel',
        providerTypes=['hotel', 'lodging'], address='Japan, Tokyo',
        latitude=35.68, longitude=139.76,
    ))


@pytest.fixture
def selection_case(request_data, mocker):
    """Keep travel policy real while isolating provider identity lookups."""
    request_data.tripCondition.destinationCountry = '일본'
    request_data.tripCondition.destinationCity = '도쿄'
    request_data.tripCondition.totalDays = 3
    destination = Destination('JP', 35.5, 139.5, 35.9, 139.95, verified_city_name='Tokyo')
    quotes = TripQuotes(
        QuoteItem('available', 410123, airport_identity=airport_identity()),
        QuoteItem('available', 211987, lodging_identity=LodgingIdentity(12345, '공급자 호텔')),
    )
    provider = mocker.Mock()
    provider.search_airport = AsyncMock(return_value=verified_airport())
    provider.search_airport_candidate = AsyncMock(side_effect=lambda candidate, destination: (
        verified_airport() if candidate.iata_code == 'NRT' else VerifiedPlace(
            verified_airport().place.model_copy(update={
                'placeId': 'places/verified-hnd', 'placeName': '하네다 공항',
            }),
        )
    ))
    provider.search_lodging = AsyncMock(return_value=candidate_hotel())
    return request_data, destination, quotes, generated_candidates(), provider


@pytest.mark.asyncio
async def test_verified_supplier_identities_keep_their_exact_prices(selection_case):
    """Existing verified supplier identities win without candidate lookups."""
    request, destination, quotes, candidates, provider = selection_case
    result = await resolve_course_anchors(request, destination, quotes, candidates, provider)
    assert result.airport is provider.search_airport.return_value
    assert result.lodging is provider.search_lodging.return_value
    assert result.quotes == quotes
    provider.search_airport.assert_awaited_once_with(quotes.flight.airport_identity, destination)
    provider.search_airport_candidate.assert_not_awaited()
    provider.search_lodging.assert_awaited_once()
    assert provider.search_lodging.await_args.args[0].name == '공급자 호텔'


@pytest.mark.asyncio
async def test_oversized_supplier_name_does_not_skip_valid_generated_hotel(selection_case):
    """Unusable supplier metadata cannot erase bounded, independently valid alternatives."""
    request, destination, quotes, candidates, provider = selection_case
    supplier_name = '가' * 161
    quotes = replace(quotes, lodging=QuoteItem(
        'available', 211987, lodging_identity=LodgingIdentity(12345, supplier_name),
    ))
    result = await resolve_course_anchors(request, destination, quotes, candidates, provider)
    baseline = baseline_trip_quotes(request, destination, adults=settings.MYREALTRIP_ADULTS)
    assert result.lodging is provider.search_lodging.return_value
    assert result.quotes.lodging.status == 'estimated'
    assert result.quotes.lodging.amount_krw == baseline.lodging.amount_krw
    assert result.quotes.lodging.lodging_identity is None
    provider.search_lodging.assert_awaited_once()
    searched, region = provider.search_lodging.await_args.args
    assert searched.name == candidates.lodgings[0].name
    assert searched.name != supplier_name[:160]
    assert region == destination


@pytest.mark.asyncio
@pytest.mark.parametrize('same_airport', [True, False])
async def test_generated_fallback_does_not_reuse_an_unrelated_supplier_price(selection_case, same_airport):
    """Changing hotels always resets its quote; matching IATA can retain airfare."""
    request, destination, quotes, candidates, provider = selection_case
    provider.search_airport.return_value = None
    provider.search_lodging.side_effect = [None, candidate_hotel()]
    candidates.airports = [candidates.airports[1 if same_airport else 0]]
    if same_airport:
        # A new real-language alias justifies verification; identical supplier
        # names are intentionally skipped by the separate deduplication test.
        candidates.airports[0] = candidates.airports[0].model_copy(update={'name': '成田国際空港'})
    result = await resolve_course_anchors(request, destination, quotes, candidates, provider)
    baseline = baseline_trip_quotes(request, destination, adults=settings.MYREALTRIP_ADULTS)
    assert result.airport is not None and result.lodging is not None
    assert result.quotes.lodging.status == 'estimated'
    assert result.quotes.lodging.amount_krw == baseline.lodging.amount_krw
    assert result.quotes.lodging.lodging_identity is None
    if same_airport:
        assert result.quotes.flight == quotes.flight
    else:
        assert result.quotes.flight.status == 'estimated'
        assert result.quotes.flight.amount_krw == baseline.flight.amount_krw
        assert result.quotes.flight.airport_identity != quotes.flight.airport_identity
    assert provider.search_airport.await_count + provider.search_airport_candidate.await_count == 2
    assert provider.search_lodging.await_count == 2


@pytest.mark.asyncio
async def test_unidentified_supplier_prices_are_not_attached_to_generated_properties(selection_case):
    """A positive amount alone is not evidence for the newly verified hotel/airport."""
    request, destination, _, candidates, provider = selection_case
    quotes = TripQuotes(QuoteItem('available', 987654), QuoteItem('available', 876543))
    result = await resolve_course_anchors(request, destination, quotes, candidates, provider)
    baseline = baseline_trip_quotes(request, destination, adults=settings.MYREALTRIP_ADULTS)
    assert result.quotes.flight.status == result.quotes.lodging.status == 'estimated'
    assert result.quotes.flight.amount_krw == baseline.flight.amount_krw
    assert result.quotes.lodging.amount_krw == baseline.lodging.amount_krw
    assert result.airport is not None and result.lodging is not None
    provider.search_airport.assert_not_awaited()
    provider.search_airport_candidate.assert_awaited_once()
    provider.search_lodging.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_candidates_are_bounded_and_leave_existing_quotes_unchanged(selection_case):
    """At most two Maps attempts per role cannot consume the venue retry budget."""
    request, destination, _, candidates, provider = selection_case
    quotes = baseline_trip_quotes(request, destination)
    provider.search_airport_candidate.side_effect = [RuntimeError('offline failure'), None]
    provider.search_lodging.side_effect = [None, RuntimeError('offline failure')]
    result = await resolve_course_anchors(request, destination, quotes, candidates, provider)
    assert result.airport is result.lodging is None
    assert result.quotes == quotes
    assert provider.search_airport_candidate.await_count == provider.search_lodging.await_count == 2
    provider.search_airport.assert_not_awaited()


@pytest.mark.asyncio
async def test_supplier_attempt_counts_towards_role_limit(selection_case):
    """Two failed supplier-plus-candidate checks must not try a third alternative."""
    request, destination, quotes, candidates, provider = selection_case
    provider.search_airport.return_value = None
    provider.search_airport_candidate.side_effect = None
    provider.search_airport_candidate.return_value = None
    provider.search_lodging.return_value = None
    result = await resolve_course_anchors(request, destination, quotes, candidates, provider)
    assert result.airport is result.lodging is None
    provider.search_airport.assert_awaited_once()
    provider.search_airport_candidate.assert_awaited_once()
    assert provider.search_lodging.await_count == 2
    assert result.quotes == quotes


@pytest.mark.asyncio
async def test_different_airport_precedes_repeat_supplier_candidate(selection_case):
    """NRT lookup failure must leave the second slot for HND, even when NRT is listed first."""
    request, destination, quotes, candidates, provider = selection_case
    candidates.airports.reverse()  # Generated order is NRT, HND; supplier is NRT.
    provider.search_airport.return_value = None

    result = await resolve_course_anchors(request, destination, quotes, candidates, provider)

    assert result.airport.place.placeId == 'places/verified-hnd'
    assert result.airport_attempts == ('NRT', 'HND')
    baseline = baseline_trip_quotes(request, destination, adults=settings.MYREALTRIP_ADULTS)
    assert result.quotes.flight == baseline.flight
    assert result.quotes.lodging == quotes.lodging
    provider.search_airport.assert_awaited_once()
    provider.search_airport_candidate.assert_awaited_once()
    assert provider.search_airport_candidate.await_args.args[0].iata_code == 'HND'


@pytest.mark.asyncio
async def test_identical_supplier_airport_candidate_does_not_repeat_lookup(selection_case):
    """The same IATA, name and English alias are already covered by supplier verification."""
    request, destination, quotes, candidates, provider = selection_case
    provider.search_airport.return_value = None
    candidates.airports = candidates.airports[1:]
    result = await resolve_course_anchors(request, destination, quotes, candidates, provider)
    assert result.airport is None
    assert result.airport_attempts == ('NRT',)
    assert result.quotes == quotes
    provider.search_airport.assert_awaited_once()
    provider.search_airport_candidate.assert_not_awaited()


@pytest.mark.asyncio
async def test_node_recovers_new_airport_without_relooking_up_completed_hotel(selection_case, mocker):
    """A missing proposal is not a permanent failure when an existing repair adds it."""
    request, destination, quotes, candidates, provider = selection_case
    quotes = replace(quotes, flight=baseline_trip_quotes(request, destination).flight)
    nodes = CourseGraphNodes(provider, mocker.Mock())
    mocker.patch('app.agent.course_nodes._progress')
    llm = mocker.patch('app.agent.course_nodes.draft_candidates', new_callable=AsyncMock)
    state = {
        'request': request, 'destination': destination, 'trip_quotes': quotes,
        'anchor_candidates': AnchorCandidates(lodgings=candidates.lodgings),
    }
    state.update(await nodes.resolve_anchors(state))
    initial = state['anchor_selection']
    assert initial.airport is None
    assert initial.airport_attempts == ()
    assert initial.lodging is provider.search_lodging.return_value

    state['anchor_candidates'] = AnchorCandidates(airports=candidates.airports[:1])
    state.update(await nodes.resolve_anchors(state))
    recovered = state['anchor_selection']
    assert recovered.airport.place.placeId == 'places/verified-hnd'
    assert recovered.airport_attempts == ('HND',)
    assert recovered.lodging is initial.lodging
    assert recovered.quotes.lodging == initial.quotes.lodging == quotes.lodging
    assert await nodes.resolve_anchors(state) == {}
    provider.search_airport.assert_not_awaited()
    provider.search_airport_candidate.assert_awaited_once()
    provider.search_lodging.assert_awaited_once()
    llm.assert_not_awaited()


@pytest.mark.asyncio
async def test_airport_attempt_limit_and_failed_codes_survive_later_resolutions(selection_case):
    """Revisiting the node cannot spend another lookup on the same code or reset its cap."""
    request, destination, _, candidates, provider = selection_case
    quotes = baseline_trip_quotes(request, destination)
    provider.search_airport_candidate.side_effect = None
    provider.search_airport_candidate.return_value = None
    first_candidates = AnchorCandidates(airports=candidates.airports[:1], lodgings=candidates.lodgings)
    first = await resolve_course_anchors(request, destination, quotes, first_candidates, provider)
    assert first.airport_attempts == ('HND',)

    repeated = await resolve_course_anchors(
        request, destination, first.quotes, first_candidates, provider, previous=first,
    )
    assert repeated.airport_attempts == first.airport_attempts
    provider.search_airport_candidate.assert_awaited_once()

    second = await resolve_course_anchors(
        request, destination, repeated.quotes, candidates, provider, previous=repeated,
    )
    assert second.airport_attempts == ('HND', 'NRT')
    assert [call.args[0].iata_code for call in provider.search_airport_candidate.await_args_list] == ['HND', 'NRT']
    third_candidate = AnchorCandidates.model_validate({
        'airports': [{'name': '간사이 국제공항', 'iata_code': 'KIX'}],
    })
    capped = await resolve_course_anchors(
        request, destination, second.quotes, third_candidate, provider, previous=second,
    )
    assert capped.airport is None
    assert capped.airport_attempts == ('HND', 'NRT')
    assert capped.lodging is first.lodging
    assert capped.quotes == first.quotes
    assert provider.search_airport_candidate.await_count == 2
    provider.search_lodging.assert_awaited_once()


@pytest.mark.asyncio
async def test_ground_same_day_trip_ignores_generated_airport_and_hotel(selection_case):
    """Optional proposals cannot change the transport mode or create a hotel night."""
    request, destination, _, candidates, provider = selection_case
    request.tripCondition.totalDays = 1
    quotes = TripQuotes(QuoteItem('available', 120000), QuoteItem('not_applicable'), 'ground')
    result = await resolve_course_anchors(request, destination, quotes, candidates, provider)
    assert result.airport is result.lodging is None
    assert result.quotes == quotes
    provider.search_airport.assert_not_awaited()
    provider.search_airport_candidate.assert_not_awaited()
    provider.search_lodging.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_selection_budget_never_starts_optional_requests(selection_case):
    """Expired optional work leaves the verified daily course budget intact."""
    request, destination, quotes, candidates, provider = selection_case
    result = await resolve_course_anchors(
        request, destination, quotes, candidates, provider,
        deadline=asyncio.get_running_loop().time() - 1,
    )
    assert result.airport is result.lodging is None
    assert result.quotes == quotes
    provider.search_airport.assert_not_awaited()
    provider.search_airport_candidate.assert_not_awaited()
    provider.search_lodging.assert_not_awaited()


@pytest.mark.asyncio
async def test_timeout_keeps_completed_peer_and_joins_stalled_lookup(selection_case):
    """A stuck airport provider cannot erase a successfully verified candidate hotel."""
    request, destination, _, candidates, provider = selection_case
    quotes = baseline_trip_quotes(request, destination)
    stopped = asyncio.Event()

    async def stalled(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    provider.search_airport_candidate.side_effect = stalled
    result = await asyncio.wait_for(resolve_course_anchors(
        request, destination, quotes, candidates, provider,
        deadline=asyncio.get_running_loop().time() + .1,
    ), timeout=1)
    assert stopped.is_set()
    assert result.airport is None
    assert result.lodging is provider.search_lodging.return_value
    provider.search_airport_candidate.assert_awaited_once()


@pytest.mark.asyncio
async def test_client_cancellation_propagates_after_optional_tasks_join(selection_case):
    """Cancelling generation must not leave optional provider calls in the background."""
    request, destination, _, candidates, provider = selection_case
    quotes = baseline_trip_quotes(request, destination)
    started = []
    finished = []
    entered = asyncio.Event()

    async def stalled(candidate, *args, **kwargs):
        started.append(candidate.name)
        if len(started) == 2:
            entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.append(candidate.name)

    provider.search_airport_candidate.side_effect = stalled
    provider.search_lodging.side_effect = stalled
    task = asyncio.create_task(resolve_course_anchors(request, destination, quotes, candidates, provider))
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert len(started) == len(finished) == 2
    assert set(finished) == set(started)


@pytest.mark.asyncio
@pytest.mark.parametrize('quote_failure', ['unavailable', 'exception'])
async def test_real_graph_uses_joint_candidates_without_supplier_inventory(
    request_data, graph_dependencies, mocker, quote_failure,
):
    """Failed quotes retain estimated fare displays, once-only totals and the activity allowance."""
    provider, history, llm, draft, places = graph_dependencies
    use_overseas_destination(request_data, provider)
    add_second_day(request_data, draft, places)
    request_data.tripCondition.totalDays = 3
    request_data.tripCondition.budget = 3000000
    third = []
    for candidate in draft.days[0].candidates:
        other = candidate.model_copy(update={'name': candidate.name + '-3일차'})
        original = places[candidate.name]
        places[other.name] = VerifiedPlace(original.place.model_copy(update={
            'placeId': original.place.placeId + '-day3', 'placeName': other.name,
        }), periods=original.periods)
        third.append(other)
    draft.days.append(DraftDay(candidates=third))
    draft.anchor_candidates = generated_candidates()
    draft.anchor_candidates.airports = [draft.anchor_candidates.airports[1]]
    provider.search_airport = AsyncMock(return_value=None)
    provider.search_airport_candidate = AsyncMock(return_value=verified_airport())
    hotel = candidate_hotel()
    hotel = replace(hotel, place=hotel.place.model_copy(update={'latitude': 37.552, 'longitude': 126.98}))
    provider.search_lodging = AsyncMock(return_value=hotel)
    quotes = mocker.Mock(spec=MyRealTripQuoteProvider)
    quotes.quote = AsyncMock(return_value=TripQuotes(
        QuoteItem('unavailable'), QuoteItem('unavailable'),
    ))
    if quote_failure == 'exception':
        quotes.quote.side_effect = RuntimeError('offline supplier unavailable')
    baseline = baseline_trip_quotes(request_data, provider.resolve_destination.return_value, adults=settings.MYREALTRIP_ADULTS)

    async def propose(*args, **kwargs):
        allowance = (request_data.tripCondition.budget - baseline.total_krw) / 3
        assert f'왕복 이동·전체 숙박 비용 합계(KRW): {baseline.total_krw}' in args[2]
        assert f'일별 장소·식사 예산 상한 목표(KRW): {allowance:.0f}' in args[2]
        return draft

    llm.side_effect = propose

    async def route(origin, target, *args, **kwargs):
        ids = {origin.place.placeId, target.place.placeId}
        fare = 2300 if 'places/verified-nrt' in ids else 1000 if 'places/generated-hotel' in ids else 0
        return InternalTransportSchema(type='transit', distance=1000, minutes=12, cost=fare, memo='확인된 이동 안내')

    provider.route.side_effect = route
    localizer = mocker.patch('app.agent.course_nodes.localize_course', side_effect=lambda request, course, **kwargs: course)
    updates = [update async for update in build_course_graph(provider, history, quotes=quotes).astream(
        {'request': request_data, 'attempt': 0}, stream_mode='updates',
    )]
    stages = [node for update in updates for node in update]
    assert stages.index('draft') < stages.index('resolve_anchors') < stages.index('verify_places')
    assert stages.count('resolve_anchors') == 1
    course = updates[-1]['finalize_course']['course']
    assert [len(day.stops) for day in course.itinerary.days] == [7, 6, 6]
    assert course.itinerary.days[0].stops[0].place.placeId == course.itinerary.days[-1].stops[-1].place.placeId == 'places/verified-nrt'
    assert {day.stops[-1].place.placeId for day in course.itinerary.days[:-1]} == {'places/generated-hotel'}
    anchors = {'places/verified-nrt', 'places/generated-hotel'}
    visits = [stop for day in course.itinerary.days for stop in day.stops if stop.place.placeId not in anchors]
    assert len(visits) == len({stop.place.placeId for stop in visits}) == 15
    assert course.totalCost == 105000 + baseline.total_krw + 2 * 1000 + 2 * 2300
    assert sum(day.stops[-1].cost for day in course.itinerary.days[:-1]) == baseline.lodging.amount_krw
    assert await history.recent(history_key(request_data)) == [{stop.place.placeId for stop in visits}]
    assert all(not anchors.intersection(entry['attraction_ids']) for entry in await history.recent_profiles(history_key(request_data)))
    provider.search_airport.assert_not_awaited()
    provider.search_airport_candidate.assert_awaited_once()
    provider.search_lodging.assert_awaited_once()
    assert provider.search.await_count == 15
    llm.assert_awaited_once()
    quotes.quote.assert_awaited_once()
    localizer.assert_awaited_once()
    public = course.model_dump(context={'public_response': True})
    assert public['itinerary']['days'][0]['stops'][0]['place']['category'] == 'AIRPORT'
    assert public['itinerary']['days'][0]['stops'][-1]['place']['category'] == 'LODGING'
    assert public['itinerary']['days'][-1]['stops'][-1]['place']['category'] == 'AIRPORT'

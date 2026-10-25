"""Verify MRT ISO namespaces, exact airport recovery and asymmetric one-way fares."""

import json
from dataclasses import replace

import httpx
import pytest

from app.schemas.course_anchors import AirportCandidate
from app.services.myrealtrip_quotes import (
    PROTOCOL_VERSION,
    AirportIdentity,
    MyRealTripQuoteProvider,
    QuoteItem,
)
from tests.test_course_airports import airport_case as _airport_case
from tests.test_course_graph import request_data as _request_data
from tests.test_myrealtrip_quotes import (
    airport,
    flight_result,
    partner_response,
    rpc_response,
    trip,
)

airport_case = _airport_case
request_data = _request_data

def new_york_airport(code='JFK', *, iso='US', country='USA'):
    """Replay the observed ISO2/ISO3 distinction without recording credentials."""
    names = {'JFK': ('존에프케네디', 'Johnfkennedy'), 'NYC': ('뉴욕', 'New York'),
             'EWR': ('뉴왁', 'Newark')}
    korean, english = names[code]
    return {'airport': {'code': code, 'koName': korean, 'enName': english},
            'city': {'code': 'NYC', 'koName': '뉴욕', 'enName': 'NewYork'},
            'country': {'code': country, 'koName': '미국', 'enName': 'U.S.A'}, 'isoCode': iso}


def directional_result(arguments, amount):
    """Use the actual one-way item shape, which omits returnDate."""
    data = flight_result(amount, destination=arguments['destination'], origin=arguments['origin'])
    item = data['result']['items'][0]
    item['tripType'] = 'ONE_WAY'
    item['travelInfo'] = {'departDate': arguments['departDate']}
    return data


@pytest.mark.asyncio
@pytest.mark.parametrize('explicit_airport', [False, True])
async def test_iso3_country_and_generated_iata_reach_lowest_directional_search(explicit_airport):
    calls, keywords = [], []
    proposal = AirportCandidate(name='존 F. 케네디 국제공항', english_name='John F. Kennedy International Airport', iata_code='JFK')
    request = replace(trip(country_code='US', city='뉴욕'), verified_city_name='New York',
                      separate_flights=True, destination_airport=proposal if explicit_airport else None)

    def respond(outgoing):
        payload = json.loads(outgoing.content)
        if outgoing.url.host == 'partner-ext-api.myrealtrip.com':
            keywords.append(payload['keyword'])
            return partner_response([new_york_airport('NYC'), new_york_airport(), new_york_airport('EWR')])
        if payload['method'] == 'initialize':
            return rpc_response(payload, {'protocolVersion': PROTOCOL_VERSION})
        if payload['method'] == 'notifications/initialized':
            return httpx.Response(202)
        arguments = payload['params']['arguments']
        calls.append(arguments)
        assert payload['params']['name'] == 'searchInternationalFlights'
        assert arguments['tripType'] == 'ONE_WAY' and arguments['cabinClass'] == 'ECONOMY'
        assert 'returnDate' not in arguments
        assert arguments['passengers'] == {'adults': 1, 'children': 0, 'infants': 0}
        outbound = arguments['origin'] == 'ICN'
        code = arguments['destination'] if outbound else arguments['origin']
        amount = (781000 if outbound else 856500) if code == 'JFK' else 1200000
        data = directional_result(arguments, amount)
        # Unrelated summaries cannot become the accepted fare.
        data['result']['summary']['cheapestTotal'] = 1
        return rpc_response(payload, {'structuredContent': data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key='offline', origin_airport='ICN') as provider:
        result = await provider.quote_flight(request)
    assert result.status == 'available' and result.amount_krw == 1637500
    assert (result.outbound_amount_krw, result.return_amount_krw) == (781000, 856500)
    assert result.airport_identity.code == 'JFK'
    assert keywords == (['JFK'] if explicit_airport else ['New York'])
    assert len(calls) == (2 if explicit_airport else 4)
    assert all('NYC' not in (call['origin'], call['destination']) for call in calls)
    assert {(call['origin'], call['destination'], call['departDate']) for call in calls if 'JFK' in (call['origin'], call['destination'])} == {
        ('ICN', 'JFK', '2027-01-10'), ('JFK', 'ICN', '2027-01-12'),
    }


@pytest.mark.asyncio
@pytest.mark.parametrize('iso,country', [('JP', 'USA'), ('US', 'JP'), (None, 'USA')])
async def test_explicit_generated_code_never_bypasses_country_verification(iso, country):
    def respond(outgoing):
        assert outgoing.url.host == 'partner-ext-api.myrealtrip.com'
        return partner_response([new_york_airport(iso=iso, country=country)])

    request = replace(trip(country_code='US', city='뉴욕'), separate_flights=True,
                      destination_airport=AirportCandidate(name='케네디 공항', iata_code='JFK'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key='offline', origin_airport='ICN') as provider:
        result = await provider.quote_flight(request)
    assert result.status == 'unavailable' and result.amount_krw is None


@pytest.mark.asyncio
async def test_missing_or_wrong_direction_cannot_become_a_complete_two_leg_quote():
    def respond(outgoing):
        payload = json.loads(outgoing.content)
        if outgoing.url.host == 'partner-ext-api.myrealtrip.com':
            return partner_response([airport()])
        if payload['method'] == 'initialize':
            return rpc_response(payload, {'protocolVersion': PROTOCOL_VERSION})
        if payload['method'] == 'notifications/initialized':
            return httpx.Response(202)
        arguments = payload['params']['arguments']
        data = directional_result(arguments, 123456)
        if arguments['origin'] != 'ICN':
            data['result']['items'][0]['route'] = {'origin': 'ICN', 'destination': 'NRT'}
        return rpc_response(payload, {'structuredContent': data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key='offline', origin_airport='ICN') as provider:
        result = await provider.quote_flight(replace(trip(), separate_flights=True))
    assert result.status == 'unavailable' and result.amount_krw is None
    assert result.outbound_amount_krw is result.return_amount_krw is None


@pytest.mark.parametrize('outbound,inbound,total,status', [
    (781000, None, 781000, 'available'), (781000, 856500, 1637501, 'available'),
    (781000, 856500, 1637500, 'estimated'), (True, 856500, 856501, 'available'),
])
def test_directional_fares_cannot_misrepresent_total_or_provenance(outbound, inbound, total, status):
    with pytest.raises(ValueError, match='separate flight amounts'):
        QuoteItem(status, total, source='offline', reference_date='2026-10-25',
                  outbound_amount_krw=outbound, return_amount_krw=inbound)


@pytest.mark.asyncio
async def test_airport_endpoints_keep_unequal_real_fares_without_halving(airport_case):
    from app.services.course_airports import attach_airports
    from app.services.myrealtrip_quotes import TripQuotes

    request, course, destination, _, provider = airport_case
    identity = AirportIdentity('NRT', '나리타 국제공항', 'JP', 'Narita International Airport')
    quotes = TripQuotes(QuoteItem('available', 1637500, airport_identity=identity,
                                  outbound_amount_krw=781000, return_amount_krw=856500), QuoteItem('not_applicable'))
    result = await attach_airports(request, course, destination, quotes, provider)
    assert result.itinerary.days[0].stops[0].cost == 781000
    assert result.itinerary.days[-1].stops[-1].cost == 856500

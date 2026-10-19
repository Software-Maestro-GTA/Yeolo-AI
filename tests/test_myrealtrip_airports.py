"""Verify live airport identity, bounded fare alternatives and lookup reuse."""

import asyncio
import json
from collections import Counter
from dataclasses import replace

import httpx
import pytest

from app.services.myrealtrip_quotes import (
    PROTOCOL_VERSION,
    AirportIdentity,
    MyRealTripQuoteProvider,
)
from tests.test_myrealtrip_quotes import (
    airport,
    flight_result,
    partner_response,
    rpc_response,
    trip,
)


@pytest.mark.asyncio
async def test_airportless_city_leaves_flight_unknown_without_catalog_or_country_fallback():
    calls = []

    async def transport(request):
        assert request.url.host == 'partner-ext-api.myrealtrip.com'
        calls.append(json.loads(request.content))
        assert request.headers['Authorization'] == 'Bearer offline-key'
        return partner_response([])

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key='offline-key', origin_airport='ICN') as provider:
        result = await provider.quote(trip(days=1, city='교토'))
    assert calls == [{'keyword': '교토', 'size': 20}]
    assert result.flight.status == 'unavailable' and result.flight.amount_krw is None
    assert result.flight.reason == 'unverified_airport'
    assert result.lodging.status == 'not_applicable'


@pytest.mark.asyncio
async def test_concurrent_airport_requests_reuse_keyword_lookups():
    calls = Counter()

    async def transport(request):
        keyword = json.loads(request.content)['keyword']
        calls[keyword] += 1
        await asyncio.sleep(0)  # Let another caller reach the same request-local lock.
        return partner_response([airport(), airport(code='HND')])

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key='offline-key', origin_airport='ICN') as provider:
        first, second = await asyncio.gather(provider._airports(trip()), provider._airports(trip()))
    assert first == second == {
        'NRT': AirportIdentity('NRT', '나리타 국제공항', 'JP', 'Narita International Airport'),
        'HND': AirportIdentity('HND', '하네다 공항', 'JP', 'Haneda Airport'),
    }
    assert calls == Counter({'도쿄': 1})


@pytest.mark.asyncio
async def test_fares_use_only_three_distinct_live_same_city_airports():
    searches = []

    async def transport(request):
        if request.url.host == 'partner-ext-api.myrealtrip.com':
            return partner_response([airport(code=code) for code in ('AAA', 'AAA', 'BBB', 'CCC', 'DDD', 'ICN')]
                                    + [airport(code='LAX', country='US'), airport(code='KIX', city='오사카')])
        payload = json.loads(request.content)
        if payload['method'] == 'initialize':
            return rpc_response(payload, {'protocolVersion': PROTOCOL_VERSION})
        if payload['method'] == 'notifications/initialized':
            return httpx.Response(202)
        arguments = payload['params']['arguments']
        searches.append(arguments)
        return rpc_response(payload, {'structuredContent': flight_result(
            {'AAA': 350000, 'BBB': 300000, 'CCC': 320000}[arguments['destination']],
            destination=arguments['destination'],
        )})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key='offline-key', origin_airport='ICN') as provider:
        result = await provider.quote(trip(days=1))
    assert {arguments['destination'] for arguments in searches} == {'AAA', 'BBB', 'CCC'}
    assert all(arguments['origin'] == 'ICN' and arguments['departDate'] == arguments['returnDate'] == '2027-01-10'
               for arguments in searches)
    assert result.flight.status == 'available' and result.flight.amount_krw == 300000


@pytest.mark.asyncio
async def test_airport_lookup_uses_verified_english_city_for_other_input_scripts():
    calls = []

    async def transport(request):
        calls.append(json.loads(request.content))
        return partner_response([airport(), airport(code='KIX', city='오사카')])

    request = replace(trip(city='東京'), verified_country_name='일본', verified_city_name='Tokyo')
    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key='offline-key', origin_airport='ICN') as provider:
        identities = await provider._airports(request)
    assert identities == {'NRT': AirportIdentity('NRT', '나리타 국제공항', 'JP', 'Narita International Airport')}
    assert calls == [{'keyword': 'Tokyo', 'size': 20}]

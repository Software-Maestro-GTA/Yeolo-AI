"""Offline HTTP contract and optional-failure tests for MyRealTrip quotes."""

import asyncio
import copy
import json
from dataclasses import replace
from datetime import date

import httpx
import pytest

from app.services.myrealtrip_quotes import (
    PARTNER_BASE_URL,
    PROTOCOL_VERSION,
    MyRealTripQuoteProvider,
    QuoteItem,
    QuoteRequest,
)


def trip(*, days: int = 3, country_code: str = 'JP', city: str = '도쿄', flight_required: bool = True) -> QuoteRequest:
    return QuoteRequest(country_code, '일본' if country_code == 'JP' else '대한민국', city, date(2027, 1, 10), date(2027, 1, 9 + days), flight_required)


def airport(*, code: str = 'NRT', city: str = '도쿄', country: str = 'JP', iso: str | None = None) -> dict:
    return {'airport': {'code': code, 'koName': '나리타', 'enName': 'Narita'},
            'city': {'code': 'TYO', 'koName': city, 'enName': 'Tokyo' if city == '도쿄' else 'Other'},
            'country': {'code': country, 'koName': '일본', 'enName': 'Japan'}, 'isoCode': country if iso is None else iso}


def partner_response(airports: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={'data': {'airports': airports}, 'meta': {}, 'result': {'status': 200, 'message': 'SUCCESS', 'code': 'success'}})


def rpc_response(payload: dict, result: dict, *, sse: bool = False, headers: dict | None = None) -> httpx.Response:
    message = {'jsonrpc': '2.0', 'id': payload['id'], 'result': result}
    if sse:
        notification = json.dumps({'jsonrpc': '2.0', 'method': 'notifications/progress', 'params': {'progress': 1}})
        body = f': heartbeat\nevent: message\ndata: {notification}\n\nevent: message\ndata: {json.dumps(message)}\n\n'
        return httpx.Response(200, text=body, headers={'Content-Type': 'text/event-stream', **(headers or {})})
    return httpx.Response(200, json=message, headers=headers)


def flight_result(amount: object = 375000, *, return_date='2027-01-10', destination='NRT', origin='ICN', currency='KRW') -> dict:
    return {'success': True, 'meta': {'status': 200, 'contentType': 'application/json'},
            'presentation': {}, 'result': {'summary': {'totalCandidates': 1, 'totalRoutes': 1, 'returned': 1, 'cheapestTotal': amount},
                                           'items': [{
                                               'tripType': 'ROUND_TRIP',
                                               'route': {'origin': origin, 'destination': destination},
                                               'travelInfo': {'departDate': '2027-01-10', 'returnDate': return_date},
                                               'price': {'total': amount, 'currency': currency},
                                           }], 'promotionAirlines': []}}


@pytest.mark.parametrize('status,amount', [('available', None), ('available', True), ('available', 0), ('unavailable', 0), ('not_applicable', 0)])
def test_quote_status_cannot_disguise_missing_amount_as_free(status, amount):
    with pytest.raises(ValueError):
        QuoteItem(status, amount)


@pytest.mark.asyncio
@pytest.mark.parametrize('sse', [False, True])
async def test_real_http_contract_dates_guests_session_and_unknown_whole_stay_price(sse):
    calls = []

    async def transport(request):
        calls.append(request)
        if request.url.host == 'partner-ext-api.myrealtrip.com':
            return partner_response([airport(), airport(code='HND')])
        if request.method == 'DELETE':
            return httpx.Response(204)
        payload = json.loads(request.content)
        if payload['method'] == 'initialize':
            return rpc_response(payload, {'protocolVersion': PROTOCOL_VERSION, 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'mcp-servers', 'version': '1.0.0'}}, sse=sse, headers={'Mcp-Session-Id': 'offline-session'})
        if payload['method'] == 'notifications/initialized':
            return httpx.Response(202)
        name = payload['params']['name']
        if name == 'searchStays':
            data = {'stays': [{'gid': 1234, 'name': 'Test hotel', 'price': '90,000원', 'additionalPrice': '1박 기준'}],
                    'searchParams': {}, 'pagination': {'currentPage': 1, 'hasNextPage': False, 'totalCount': 1, 'itemsInThisPage': 1}}
        elif name == 'getStayDetail':
            # A plausible field name is not a published whole-stay price contract.
            data = {'totalPrice': 180000, 'currency': 'KRW', 'noRoomsAvailable': False}
        else:
            destination = payload['params']['arguments']['destination']
            data = flight_result(375000 if destination == 'NRT' else 365000, destination=destination, return_date='2027-01-12')
        return rpc_response(payload, {'structuredContent': data, 'content': [], 'isError': False}, sse=sse)

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
        async with MyRealTripQuoteProvider(client, partner_api_key='offline-key', origin_airport='ICN', adults=2, children=1) as provider:
            result = await provider.quote(trip())
        assert not client.is_closed

    assert result.flight.amount_krw == 365000 and result.flight.status == 'available'
    assert result.lodging.amount_krw is None and result.lodging.reason == 'price_not_verifiable'
    assert result.known_total_krw == 365000 and not result.complete and result.missing_items == ('lodging',)
    rest = calls[0]
    assert str(rest.url) == PARTNER_BASE_URL + '/v1/products/flight/airport-autocomplete'
    assert json.loads(rest.content) == {'keyword': '도쿄', 'size': 20}
    assert rest.headers['Authorization'] == 'Bearer offline-key'
    mcp_calls = [request for request in calls if request.url.host == 'mcp-servers.myrealtrip.com']
    assert all('Authorization' not in request.headers for request in mcp_calls)
    payloads = [json.loads(request.content) for request in mcp_calls if request.method == 'POST']
    assert sum(payload['method'] == 'initialize' for payload in payloads) == 1
    assert sum(payload['method'] == 'notifications/initialized' for payload in payloads) == 1
    for request in mcp_calls[1:]:
        assert request.headers['Mcp-Session-Id'] == 'offline-session'
        assert request.headers['MCP-Protocol-Version'] == PROTOCOL_VERSION
    searches = [payload['params'] for payload in payloads if payload['method'] == 'tools/call']
    for search in searches:
        arguments = search['arguments']
        if search['name'].startswith('searchInternational'):
            assert arguments['departDate'] == '2027-01-10' and arguments['returnDate'] == '2027-01-12'
            assert arguments['tripType'] == 'ROUND_TRIP' and arguments['origin'] == 'ICN'
            assert arguments['passengers'] == {'adults': 2, 'children': 1, 'infants': 0}
        else:
            assert arguments['checkIn'] == '2027-01-10' and arguments['checkOut'] == '2027-01-12'
            assert arguments['adultCount'] == 2 and arguments['childCount'] == 1
    assert mcp_calls[-1].method == 'DELETE'


@pytest.mark.asyncio
@pytest.mark.parametrize('record', [airport(city='오사카'), airport(country='US'), airport(iso='US'), airport(code='TYOO')])
async def test_no_guessed_or_wrong_country_airport_is_used(record):
    calls = []

    async def transport(request):
        calls.append(request)
        return partner_response([record])

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key='offline-key', origin_airport='ICN') as provider:
        result = await provider.quote(trip(days=1))
    assert result.flight.status == 'unavailable' and result.flight.reason == 'unverified_airport'
    assert result.flight.amount_krw is None and result.lodging.status == 'not_applicable'
    assert len(calls) == 1 and calls[0].url.host == 'partner-ext-api.myrealtrip.com'


@pytest.mark.asyncio
@pytest.mark.parametrize('reason,kwargs', [('missing_partner_key', {'origin_airport': 'ICN'}), ('missing_origin', {'partner_api_key': 'offline-key'})])
async def test_missing_credentials_or_origin_is_optional_and_never_starts_guessed_search(reason, kwargs):
    def transport(request):
        pytest.fail('No HTTP call should be needed without verified airport inputs')

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client, \
            MyRealTripQuoteProvider(client, **kwargs) as provider:
        result = await provider.quote(trip(days=1))
    assert result.flight.status == 'unavailable' and result.flight.reason == reason
    assert result.flight.amount_krw is None and not result.complete


@pytest.mark.asyncio
async def test_explicit_no_flight_and_single_day_are_inapplicable_and_owned_client_closes(mocker):
    def transport(request):
        pytest.fail('An explicitly local one-day trip needs no flight/stay lookup')

    client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    mocker.patch('app.services.myrealtrip_quotes.httpx.AsyncClient', return_value=client)
    async with MyRealTripQuoteProvider() as provider:
        result = await provider.quote(trip(days=1, flight_required=False))
    assert result.complete and result.known_total_krw == 0 and result.missing_items == ()
    assert result.flight.amount_krw is None and result.lodging.amount_krw is None
    assert client.is_closed


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [True, 0, -100, 375000.5, '375,000원', None])
async def test_summary_price_must_be_a_positive_whole_numeric_amount(value):
    async def transport(request):
        if request.url.host == 'partner-ext-api.myrealtrip.com':
            return partner_response([airport()])
        payload = json.loads(request.content)
        if payload['method'] == 'initialize':
            return rpc_response(payload, {'protocolVersion': PROTOCOL_VERSION})
        if payload['method'] == 'notifications/initialized':
            return httpx.Response(202)
        return rpc_response(payload, {'structuredContent': flight_result(value), 'content': [], 'isError': False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key='offline-key', origin_airport='ICN') as provider:
        result = await provider.quote(trip(days=1))
    assert result.flight.status == 'unavailable' and result.flight.amount_krw is None
    assert result.flight.reason == 'price_not_verifiable'


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 429, 503])
async def test_partner_failures_do_not_fail_quote_or_expose_credentials(status, caplog):
    def transport(request):
        return httpx.Response(status, json={'error': 'private provider error'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key='offline-secret', origin_airport='ICN') as provider:
        result = await provider.quote(trip(days=1))
    assert result.flight.status == 'unavailable' and result.flight.reason == 'provider_error'
    assert 'offline-secret' not in caplog.text and 'private provider error' not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('currency', ['KRW', 'USD'])
async def test_domestic_search_uses_explicit_origin_passengers_and_only_krw_prices(currency):
    calls = []

    async def transport(request):
        if request.url.host == 'partner-ext-api.myrealtrip.com':
            return partner_response([airport(code='CJU', city='제주', country='KR')])
        payload = json.loads(request.content)
        if payload['method'] == 'initialize':
            return rpc_response(payload, {'protocolVersion': PROTOCOL_VERSION})
        if payload['method'] == 'notifications/initialized':
            return httpx.Response(202)
        calls.append(payload['params'])
        data = flight_result(95000, destination='CJU', origin='GMP', currency=currency)
        return rpc_response(payload, {'structuredContent': data, 'content': [], 'isError': False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key='offline-key', origin_airport='GMP', adults=1, children=2) as provider:
        result = await provider.quote(trip(days=1, country_code='KR', city='제주'))
    assert calls[0]['name'] == 'searchDomesticFlights'
    assert calls[0]['arguments']['origin'] == 'GMP' and calls[0]['arguments']['destination'] == 'CJU'
    assert calls[0]['arguments']['passengers'] == {'adults': 1, 'children': 2}
    if currency == 'KRW':
        assert result.flight.status == 'available' and result.flight.amount_krw == 95000
    else:
        assert result.flight.status == 'unavailable' and result.flight.reason == 'unsupported_currency'
        assert result.flight.amount_krw is None


@pytest.mark.asyncio
async def test_slow_stay_lookup_keeps_completed_flight_quote_and_joins_cancelled_requests():
    stay_cancelled = asyncio.Event()

    async def transport(request):
        if request.url.host == 'partner-ext-api.myrealtrip.com':
            return partner_response([airport()])
        payload = json.loads(request.content)
        if payload['method'] == 'initialize':
            return rpc_response(payload, {'protocolVersion': PROTOCOL_VERSION})
        if payload['method'] == 'notifications/initialized':
            return httpx.Response(202)
        if payload['params']['name'] == 'searchStays':
            try:
                await asyncio.Event().wait()
            finally:
                stay_cancelled.set()
        return rpc_response(payload, {'structuredContent': flight_result(return_date='2027-01-12'), 'content': [], 'isError': False})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key='offline-key', origin_airport='ICN', timeout_seconds=.03) as provider:
        result = await provider.quote(trip())
    assert result.flight.status == 'available' and result.flight.amount_krw == 375000
    assert result.lodging.status == 'unavailable' and result.lodging.reason == 'timeout'
    assert stay_cancelled.is_set()


@pytest.mark.asyncio
async def test_caller_cancellation_propagates_after_all_http_requests_join():
    started = [asyncio.Event(), asyncio.Event()]
    cancelled = [asyncio.Event(), asyncio.Event()]

    async def transport(request):
        index = 0 if request.url.host == 'partner-ext-api.myrealtrip.com' else 1
        started[index].set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled[index].set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key='offline-key', origin_airport='ICN') as provider:
        task = asyncio.create_task(provider.quote(trip()))
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(event.is_set() for event in cancelled)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['json_text', 'html', 'wrong_id', 'rpc_error'])
async def test_only_matching_structured_json_prices_are_read(mode):
    async def transport(request):
        if request.url.host == 'partner-ext-api.myrealtrip.com':
            return partner_response([airport()])
        payload = json.loads(request.content)
        if payload['method'] == 'initialize':
            return rpc_response(payload, {'protocolVersion': PROTOCOL_VERSION})
        if payload['method'] == 'notifications/initialized':
            return httpx.Response(202)
        if mode == 'json_text':
            return rpc_response(payload, {'content': [{'type': 'text', 'text': json.dumps(flight_result())}], 'isError': False})
        if mode == 'html':
            return httpx.Response(200, text='<p>항공권 375,000원</p>', headers={'Content-Type': 'text/html'})
        if mode == 'wrong_id':
            return httpx.Response(200, json={'jsonrpc': '2.0', 'id': str(payload['id']), 'result': {'structuredContent': flight_result()}})
        return httpx.Response(200, json={'jsonrpc': '2.0', 'id': payload['id'], 'error': {'code': -32000, 'message': 'private error'}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key='offline-key', origin_airport='ICN') as provider:
        result = await provider.quote(trip(days=1))
    assert result.flight.status == ('available' if mode == 'json_text' else 'unavailable')
    assert result.flight.amount_krw == (375000 if mode == 'json_text' else None)


def stay_detail(arguments, amount=475374):
    return {
        'success': True, 'searchParams': dict(arguments),
        'property': {'gid': arguments['gid'], 'region': {'country': '일본', 'city': '도쿄', 'isDomestic': False}},
        'pricing': {'isSoldOut': False, 'priceText': '194,155원/박', 'averagePrice': 194155, 'totalPrice': amount},
        'rooms': [{'roomName': '싱글룸', 'ratePlan': {'status': 'ON_SALE', 'averagePrice': 194155,
                    'averagePriceText': '194,155원/박', 'totalPrice': amount, 'priceDescription': '세금포함 237,687원/박'}}],
        'noRoomsAvailable': False,
    }


def lodging_transport(make_detail, gids=(5008360,), calls=None):
    async def transport(request):
        payload = json.loads(request.content)
        if payload['method'] == 'initialize':
            return rpc_response(payload, {'protocolVersion': PROTOCOL_VERSION})
        if payload['method'] == 'notifications/initialized':
            return httpx.Response(202)
        params = payload['params']
        if calls is not None:
            calls.append(params)
        if params['name'] == 'searchStays':
            data = {'stays': [{'gid': gid} for gid in gids]}
        else:
            data = await make_detail(params['arguments'])
        return rpc_response(payload, {'structuredContent': data, 'content': [], 'isError': False})
    return httpx.MockTransport(transport)


@pytest.mark.asyncio
async def test_verified_whole_stay_total_is_not_average_price_times_nights():
    calls = []

    async def detail(arguments):
        assert arguments == {'gid': 5008360, 'checkIn': '2027-01-10', 'checkOut': '2027-01-12', 'adultCount': 2, 'childCount': 1}
        return stay_detail(arguments)

    async with httpx.AsyncClient(transport=lodging_transport(detail, calls=calls)) as client, \
            MyRealTripQuoteProvider(client, adults=2, children=1) as provider:
        result = await provider.quote(trip(flight_required=False))
    assert result.lodging.status == 'available' and result.lodging.amount_krw == 475374
    assert result.known_total_krw == 475374 and result.complete
    assert result.lodging.amount_krw != 194155 * 2
    assert [call['name'] for call in calls] == ['searchStays', 'getStayDetail']


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', [
    'wrong_checkin', 'wrong_checkout', 'wrong_guests', 'wrong_children', 'bool_guests',
    'wrong_gid', 'wrong_property_gid', 'wrong_country', 'wrong_city', 'wrong_domestic',
    'sold_out', 'no_rooms', 'empty_rooms', 'not_on_sale', 'missing_room_name',
    'missing_echo', 'missing_pricing', 'foreign_currency', 'no_krw_unit',
    'missing_total', 'negative_total', 'bool_total', 'fractional_total', 'nightly_string',
])
async def test_unverified_lodging_details_remain_unknown_instead_of_inventing_total(mutation):
    async def detail(arguments):
        data = stay_detail(arguments)
        if mutation in {'wrong_checkin', 'wrong_checkout'}:
            data['searchParams']['checkIn' if mutation == 'wrong_checkin' else 'checkOut'] = '2027-02-01'
        elif mutation in {'wrong_guests', 'wrong_children', 'bool_guests'}:
            data['searchParams']['childCount' if mutation == 'wrong_children' else 'adultCount'] = True if mutation == 'bool_guests' else 9
        elif mutation == 'wrong_gid':
            data['searchParams']['gid'] = 5008361
        elif mutation == 'wrong_property_gid':
            data['property']['gid'] = 5008361
        elif mutation in {'wrong_country', 'wrong_city', 'wrong_domestic'}:
            data['property']['region'][{'wrong_country': 'country', 'wrong_city': 'city', 'wrong_domestic': 'isDomestic'}[mutation]] = {'wrong_country': '미국', 'wrong_city': '오사카', 'wrong_domestic': True}[mutation]
        elif mutation == 'sold_out':
            data['pricing']['isSoldOut'] = True
        elif mutation == 'no_rooms':
            data['noRoomsAvailable'] = True
        elif mutation == 'empty_rooms':
            data['rooms'] = []
        elif mutation == 'not_on_sale':
            data['rooms'][0]['ratePlan']['status'] = 'SOLD_OUT'
        elif mutation == 'missing_room_name':
            data['rooms'][0].pop('roomName')
        elif mutation in {'missing_echo', 'missing_pricing'}:
            data.pop('searchParams' if mutation == 'missing_echo' else 'pricing')
        elif mutation == 'foreign_currency':
            data['rooms'][0]['ratePlan']['currency'] = 'USD'
        elif mutation == 'no_krw_unit':
            data['pricing']['priceText'] = '$200/night'
            data['rooms'][0]['ratePlan']['averagePriceText'] = '$200/night'
            data['rooms'][0]['ratePlan']['priceDescription'] = 'Taxes included'
        else:
            data['rooms'][0]['ratePlan']['totalPrice'] = {
                'missing_total': None, 'negative_total': -1, 'bool_total': True,
                'fractional_total': 475374.5, 'nightly_string': '194,155원/박',
            }[mutation]
        return data

    async with httpx.AsyncClient(transport=lodging_transport(detail)) as client, \
            MyRealTripQuoteProvider(client) as provider:
        result = await provider.quote(trip(flight_required=False))
    assert result.lodging.status == 'unavailable' and result.lodging.amount_krw is None
    assert not result.complete and result.missing_items == ('lodging',)


@pytest.mark.asyncio
async def test_lodging_compares_two_hotels_and_available_rooms_with_one_bounded_lookup_batch():
    calls = []

    async def detail(arguments):
        data = stay_detail(arguments, 475374 if arguments['gid'] == 5008360 else 450000)
        cheap_room = copy.deepcopy(data['rooms'][0])
        cheap_room['ratePlan']['totalPrice'] = 430000
        sold_out = copy.deepcopy(cheap_room)
        sold_out['ratePlan'].update(status='SOLD_OUT', totalPrice=1)
        if arguments['gid'] == 5008361:
            data['rooms'] += [cheap_room, sold_out]
        return data

    async with httpx.AsyncClient(transport=lodging_transport(detail, gids=(5008360, 5008361, 5008362), calls=calls)) as client, \
            MyRealTripQuoteProvider(client) as provider:
        result = await provider.quote(trip(flight_required=False))
    assert result.lodging.amount_krw == 430000
    assert sum(call['name'] == 'getStayDetail' for call in calls) == 2


@pytest.mark.asyncio
async def test_slow_second_hotel_keeps_completed_verified_price_and_joins_cancelled_lookup():
    cancelled = asyncio.Event()

    async def detail(arguments):
        if arguments['gid'] == 5008361:
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return stay_detail(arguments)

    async with httpx.AsyncClient(transport=lodging_transport(detail, gids=(5008360, 5008361))) as client, \
            MyRealTripQuoteProvider(client, timeout_seconds=.05) as provider:
        result = await provider.quote(trip(flight_required=False))
    assert result.lodging.status == 'available' and result.lodging.amount_krw == 475374
    assert cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['wrong_origin', 'wrong_destination', 'wrong_depart_date', 'wrong_return_date', 'one_way', 'missing_price', 'wrong_currency', 'missing_currency'])
async def test_flight_item_must_match_returned_route_dates_type_and_numeric_krw_price(mutation):
    async def transport(request):
        if request.url.host == 'partner-ext-api.myrealtrip.com':
            return partner_response([airport()])
        payload = json.loads(request.content)
        if payload['method'] == 'initialize':
            return rpc_response(payload, {'protocolVersion': PROTOCOL_VERSION})
        if payload['method'] == 'notifications/initialized':
            return httpx.Response(202)
        data = flight_result()
        item = data['result']['items'][0]
        if mutation in {'wrong_origin', 'wrong_destination'}:
            item['route']['origin' if mutation == 'wrong_origin' else 'destination'] = 'LAX'
        elif mutation in {'wrong_depart_date', 'wrong_return_date'}:
            item['travelInfo']['departDate' if mutation == 'wrong_depart_date' else 'returnDate'] = '2027-02-01'
        elif mutation == 'one_way':
            item['tripType'] = 'ONE_WAY'
        elif mutation == 'missing_price':
            item['price'].pop('total')
        elif mutation == 'wrong_currency':
            item['price']['currency'] = 'USD'
        else:
            item['price'].pop('currency')
        return rpc_response(payload, {'structuredContent': data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key='offline-key', origin_airport='ICN') as provider:
        result = await provider.quote(trip(days=1))
    assert result.flight.status == 'unavailable' and result.flight.amount_krw is None


@pytest.mark.asyncio
async def test_flight_uses_matching_item_total_and_ignores_cheaper_unrelated_summary_or_route():
    async def transport(request):
        if request.url.host == 'partner-ext-api.myrealtrip.com':
            return partner_response([airport()])
        payload = json.loads(request.content)
        if payload['method'] == 'initialize':
            return rpc_response(payload, {'protocolVersion': PROTOCOL_VERSION})
        if payload['method'] == 'notifications/initialized':
            return httpx.Response(202)
        data = flight_result()
        data['result']['summary']['cheapestTotal'] = 1000
        unrelated = copy.deepcopy(data['result']['items'][0])
        unrelated['route']['destination'] = 'LAX'
        unrelated['price']['total'] = 500
        data['result']['items'].append(unrelated)
        return rpc_response(payload, {'structuredContent': data})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key='offline-key', origin_airport='ICN') as provider:
        result = await provider.quote(trip(days=1))
    assert result.flight.status == 'available' and result.flight.amount_krw == 375000


@pytest.mark.asyncio
@pytest.mark.parametrize('country,city', [('Japan', 'Tokyo'), ('Japon', '東京')])
async def test_lodging_region_accepts_verified_geographic_names_across_input_languages(country, city):
    """MRT's Korean region still matches the already verified real destination."""
    async def detail(arguments):
        return stay_detail(arguments)

    request = replace(trip(flight_required=False), country=country, city=city,
                      verified_country_name='일본', verified_city_name='도쿄')
    async with httpx.AsyncClient(transport=lodging_transport(detail)) as client, \
            MyRealTripQuoteProvider(client) as provider:
        result = await provider.quote(request)
    assert result.lodging.status == 'available' and result.lodging.amount_krw == 475374
    assert result.complete


@pytest.mark.asyncio
@pytest.mark.parametrize('country,city', [('미국', '도쿄'), ('일본', '오사카'), ('일본', '도쿄 근교')])
async def test_verified_region_names_never_accept_same_name_abroad_or_a_different_city(country, city):
    async def detail(arguments):
        data = stay_detail(arguments)
        data['property']['region'].update(country=country, city=city)
        return data

    request = replace(trip(flight_required=False), country='Japan', city='Tokyo',
                      verified_country_name='일본', verified_city_name='도쿄')
    async with httpx.AsyncClient(transport=lodging_transport(detail)) as client, \
            MyRealTripQuoteProvider(client) as provider:
        result = await provider.quote(request)
    assert result.lodging.status == 'unavailable' and result.lodging.amount_krw is None
    assert result.lodging.reason == 'unverified_property_region'


@pytest.mark.asyncio
async def test_missing_verified_names_preserves_exact_region_validation():
    async def detail(arguments):
        return stay_detail(arguments)

    request = replace(trip(flight_required=False), country='Japan', city='Tokyo')
    async with httpx.AsyncClient(transport=lodging_transport(detail)) as client, \
            MyRealTripQuoteProvider(client) as provider:
        result = await provider.quote(request)
    assert result.lodging.status == 'unavailable' and result.lodging.amount_krw is None
    assert result.lodging.reason == 'unverified_property_region'


@pytest.mark.parametrize('field,value', [('verified_country_name', ''), ('verified_city_name', ' '),
                                        ('verified_country_name', True), ('verified_city_name', 123)])
def test_quote_request_rejects_invalid_verified_region_names(field, value):
    with pytest.raises(ValueError, match='verified quote destination'):
        replace(trip(), **{field: value})


def supplier_city(*, identifier=2734, name='도쿄', english='Tokyo', country='일본', kind='CITY'):
    return {'regionId': identifier, 'name': name, 'enName': english, 'subName': country, 'type': kind}


def lodging_region_transport(regions, calls, *, make_detail=None, gids=(5008360, 5008361), status=200):
    async def default_detail(arguments):
        return stay_detail(arguments)

    mcp_transport = lodging_transport(make_detail or default_detail, gids=gids)

    async def transport(request):
        if request.url.host == 'partner-ext-api.myrealtrip.com':
            calls.append(request)
            return httpx.Response(status, json={'data': {'regions': regions}, 'result': {'status': 200}})
        return await mcp_transport.handle_async_request(request)

    return httpx.MockTransport(transport)


@pytest.mark.asyncio
@pytest.mark.parametrize('city', ['Tokyo', '東京'])
async def test_live_region_id_bridges_english_verified_city_to_korean_stay_name_once(city):
    calls = []
    request = replace(trip(flight_required=False), country='Japan', city=city,
                      verified_country_name='일본', verified_city_name='Tokyo')
    async with httpx.AsyncClient(transport=lodging_region_transport([supplier_city()], calls)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key='offline-region-key') as provider:
        result = await provider.quote(request)
    assert result.lodging.status == 'available' and result.lodging.amount_krw == 475374
    assert len(calls) == 1  # Two concurrent details share the actual provider lookup.
    assert str(calls[0].url) == PARTNER_BASE_URL + '/v1/products/accommodation/region-autocomplete'
    assert calls[0].headers['Authorization'] == 'Bearer offline-region-key'
    assert json.loads(calls[0].content) == {'keyword': 'Tokyo', 'isDomestic': False}


@pytest.mark.asyncio
@pytest.mark.parametrize('regions', [
    [supplier_city(country='미국')],
    [supplier_city(country='일본 근교')],
    [supplier_city(english='Osaka')],
    [supplier_city(kind='NEIGHBORHOOD')],
    [supplier_city(identifier=True)],
    [supplier_city(identifier=0)],
    [supplier_city(), supplier_city(identifier=2735)],
    [],
])
async def test_lodging_name_bridge_rejects_wrong_country_city_type_id_and_ambiguity(regions):
    calls = []
    request = replace(trip(flight_required=False), country='Japan', city='東京',
                      verified_country_name='일본', verified_city_name='Tokyo')
    async with httpx.AsyncClient(transport=lodging_region_transport(regions, calls)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key='offline-key') as provider:
        result = await provider.quote(request)
    assert result.lodging.status == 'unavailable' and result.lodging.amount_krw is None
    assert result.lodging.reason == 'unverified_property_region'
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_region_bridge_accepts_exact_last_country_component_and_duplicate_same_identity():
    calls = []
    region = supplier_city(country='간토, 일본')
    request = replace(trip(flight_required=False), country='Japan', city='Tokyo',
                      verified_country_name='일본', verified_city_name='Tokyo')
    async with httpx.AsyncClient(transport=lodging_region_transport([region, dict(region)], calls)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key='offline-key') as provider:
        result = await provider.quote(request)
    assert result.lodging.status == 'available' and result.lodging.amount_krw == 475374
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('key,country_name', [('', '일본'), ('offline-key', None)])
async def test_region_bridge_without_key_or_verified_country_retains_strict_unknown_quote(key, country_name):
    calls = []
    request = replace(trip(flight_required=False), country='Japan', city='Tokyo',
                      verified_country_name=country_name, verified_city_name='Tokyo')
    async with httpx.AsyncClient(transport=lodging_region_transport([supplier_city()], calls)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key=key) as provider:
        result = await provider.quote(request)
    assert result.lodging.status == 'unavailable' and result.lodging.amount_krw is None
    assert result.lodging.reason == 'unverified_property_region'
    assert calls == []


@pytest.mark.asyncio
async def test_matching_stay_needs_no_region_bridge_even_with_key_configured():
    calls = []
    request = replace(trip(flight_required=False), verified_country_name='일본', verified_city_name='Tokyo')
    async with httpx.AsyncClient(transport=lodging_region_transport([], calls, status=503)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key='offline-key') as provider:
        result = await provider.quote(request)
    assert result.lodging.status == 'available' and result.lodging.amount_krw == 475374
    assert calls == []


@pytest.mark.asyncio
async def test_optional_region_outage_is_not_retried_per_hotel_or_fabricated_as_a_quote():
    calls = []
    request = replace(trip(flight_required=False), country='Japan', city='Tokyo',
                      verified_country_name='일본', verified_city_name='Tokyo')
    async with httpx.AsyncClient(transport=lodging_region_transport([], calls, status=503)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key='offline-key') as provider:
        result = await provider.quote(request)
    assert result.lodging.status == 'unavailable' and result.lodging.amount_krw is None
    assert result.lodging.reason == 'unverified_property_region'
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_verified_name_bridge_cannot_override_domestic_or_overseas_identity():
    calls = []

    async def detail(arguments):
        data = stay_detail(arguments)
        data['property']['region']['isDomestic'] = True
        return data

    request = replace(trip(flight_required=False), country='Japan', city='Tokyo',
                      verified_country_name='일본', verified_city_name='Tokyo')
    async with httpx.AsyncClient(transport=lodging_region_transport([supplier_city()], calls, make_detail=detail)) as client, \
            MyRealTripQuoteProvider(client, partner_api_key='offline-key') as provider:
        result = await provider.quote(request)
    assert result.lodging.status == 'unavailable' and result.lodging.amount_krw is None
    assert result.lodging.reason == 'unverified_property_region'

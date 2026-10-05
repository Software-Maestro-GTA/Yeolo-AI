"""Exercise optional navigation guidance without weakening verified route facts."""

import asyncio
import copy
import json
from datetime import UTC, datetime

import httpx
import pytest

from app.agent.tools.verified_maps import (
    MapsProviderError,
    VerifiedMapsProvider,
    VerifiedPlace,
)
from app.schemas.course import PlaceSchema
from app.services.maps_cost import classify_maps_sku


@pytest.fixture
def endpoints():
    return tuple(VerifiedPlace(PlaceSchema(placeId=f'places/{name}', placeName=name, category='museum', latitude=37.55 + index * .001, longitude=126.98)) for index, name in enumerate(('출발지', '목적지')))


def walking(instruction):
    return {'travelMode': 'WALK', 'navigationInstruction': {'instructions': instruction}}


def transit(start, end, line, direction):
    return {'travelMode': 'TRANSIT', 'transitDetails': {
        'stopDetails': {'departureStop': {'name': start}, 'arrivalStop': {'name': end}},
        'transitLine': {'nameShort': line, 'name': f'{line} 노선', 'vehicle': {'type': 'SUBWAY'}},
        'headsign': direction,
    }}


def response(steps):
    return {'routes': [{'duration': '1201s', 'distanceMeters': 1250, 'legs': [{'steps': steps}]}]}


@pytest.mark.asyncio
async def test_same_route_request_produces_actionable_walking_guidance_without_extra_calls(endpoints):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json=response([walking('세종대로를 따라 이동하세요'), walking('율곡로에서 오른쪽으로 도세요')]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await VerifiedMapsProvider(client=client, api_key='offline').route(*endpoints)
    assert len(calls) == 1
    assert json.loads(calls[0].content)['languageCode'] == 'ko'
    mask = calls[0].headers['X-Goog-FieldMask'].split(',')
    assert 'routes.legs.steps.navigationInstruction.instructions' in mask
    assert 'routes.legs.steps.travelMode' in mask
    assert any('transitDetails.stopDetails.departureStop.name' in field for field in mask)
    assert (result.distance, result.minutes) == (1250, 21)
    assert '세종대로' in result.memo and '율곡로' in result.memo and '오른쪽' in result.memo
    assert '1250' not in result.memo and '21분' not in result.memo
    assert not any(unsupported in result.memo for unsupported in ('횡단보도', '출구', '승강장'))


@pytest.mark.asyncio
async def test_transit_memo_connects_boarding_transfer_alighting_and_final_walk(endpoints):
    payload = response([
        walking('시청역까지 세종대로를 따라 이동하세요'),
        transit('시청역', '종로3가역', '1호선', '소요산'),
        walking('3호선 환승 통로를 따라 이동하세요'),
        transit('종로3가역', '안국역', '3호선', '대화'),
        walking('북촌로를 따라 목적지까지 이동하세요'),
    ])
    departure = datetime(2026, 10, 5, 1, 0, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
        result = await VerifiedMapsProvider(client=client, api_key='offline').route(*endpoints, mode='transit', departure_time=departure)
    for fact in ('시청역', '종로3가역', '1호선', '소요산', '3호선', '대화', '안국역', '북촌로'):
        assert fact in result.memo
    assert any(word in result.memo for word in ('승차', '탑승', '타세요'))
    assert any(word in result.memo for word in ('하차', '내리', '내려'))
    assert '환승' in result.memo
    assert not any(repeated in result.memo for repeated in ('목적지까지 이동 안내', '2026-10-05', '출발 기준', '조회 시점', '전체 이동은 지도'))
    assert len(result.memo) <= 240 and len(result.memo.splitlines()) <= 3
    assert (result.minutes, result.distance, result.cost) == (21, 1250, None)
    assert '출구' not in result.memo and '승강장' not in result.memo


@pytest.mark.asyncio
@pytest.mark.parametrize('steps', [None, {}, 'broken', [None, 42, {}], [{'travelMode': 'TRANSIT', 'transitDetails': {'stopDetails': 'bad'}}]])
async def test_malformed_optional_steps_preserve_metrics_and_generic_action(endpoints, steps):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response(steps)))) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        route = await provider.route(*endpoints, mode='transit')
        cached = await provider.route(*endpoints, mode='transit')
        assert provider.metrics.snapshot()['skus']['routes_essentials']['requests'] == 1
    assert route == cached
    assert (route.minutes, route.distance) == (21, 1250)
    assert route.memo and '1250' not in route.memo and '21분' not in route.memo
    assert len(route.memo) <= 120 and len(route.memo.splitlines()) <= 3
    assert not any(word in route.memo for word in ('1호선', '출구', '승강장', '재확인이 필요'))


@pytest.mark.asyncio
async def test_formatter_exception_is_optional_but_cancellation_is_not(endpoints, mocker):
    formatter = mocker.patch('app.agent.tools.verified_maps.format_route_guidance', side_effect=RuntimeError('optional guidance shape'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response([walking('직진')])))) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        route = await provider.route(*endpoints)
        assert (route.minutes, route.distance) == (21, 1250)
        assert 'optional guidance' not in route.memo
        assert len(route.memo) <= 120 and '조회 시점' not in route.memo
        formatter.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await provider.route(*endpoints)


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_guidance', [None, 42, {}])
async def test_invalid_optional_formatter_return_cannot_break_verified_route(endpoints, mocker, bad_guidance):
    mocker.patch('app.agent.tools.verified_maps.format_route_guidance', return_value=bad_guidance)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=response([walking('직진하세요')]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        route = await VerifiedMapsProvider(client=client, api_key='offline').route(*endpoints)
    assert (route.minutes, route.distance, route.cost) == (21, 1250, 0)
    assert isinstance(route.memo, str) and route.memo.strip()
    assert len(route.memo) <= 120 and len(route.memo.splitlines()) <= 3
    assert not any(repeated in route.memo for repeated in ('목적지까지', '조회 시점', '출발 기준'))
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_incomplete_transit_details_are_an_honest_summary_not_invented_connection(endpoints):
    payload = response([walking('세종대로에서 시청역으로 이동하세요'), {'travelMode': 'TRANSIT'}, walking('북촌로에서 목적지로 이동하세요')])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
        route = await VerifiedMapsProvider(client=client, api_key='offline').route(*endpoints, mode='transit')
    assert len(route.memo) <= 120
    assert not any(fragment in route.memo for fragment in ('세종대로', '북촌로', '1호선', '3호선', '몇 번 출구'))
    assert (route.minutes, route.distance) == (21, 1250)


@pytest.mark.asyncio
async def test_external_instruction_markup_controls_and_long_steps_are_sanitized_and_bounded(endpoints):
    text = '<b>세종대로</b>에서 &amp; 오른쪽으로 이동\x00\x1b[31m'
    steps = [walking(text)] + [walking(f'{index}번 길에서 직진하세요 ' + '긴 설명 ' * 200) for index in range(40)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response(steps)))) as client:
        route = await VerifiedMapsProvider(client=client, api_key='offline').route(*endpoints)
    assert '세종대로' in route.memo and '오른쪽' in route.memo
    assert '<b>' not in route.memo and '&amp;' not in route.memo
    assert not any(ord(char) < 32 and char not in '\n\t' for char in route.memo)
    assert len(route.memo) <= 240 and len(route.memo.splitlines()) <= 3
    assert '주요 이동' in route.memo


@pytest.mark.asyncio
async def test_long_transit_route_keeps_connections_or_identifies_navigation_as_partial(endpoints):
    steps = [walking(f'출발지에서 {index}번째 골목을 지나 이동하세요') for index in range(30)] + [
        transit('시청역', '종로3가역', '1호선', '소요산'),
        walking('3호선 환승 통로를 따라 이동하세요'),
        transit('종로3가역', '안국역', '3호선', '대화'),
        walking('북촌로를 따라 목적지까지 이동하세요'),
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response(steps)))) as client:
        route = await VerifiedMapsProvider(client=client, api_key='offline').route(*endpoints, mode='transit')
    assert (route.minutes, route.distance) == (21, 1250)
    assert len(route.memo) <= 240 and len(route.memo.splitlines()) <= 3
    connections = ('시청역', '종로3가역', '1호선', '소요산', '안국역', '3호선', '대화')
    assert all(fact in route.memo for fact in connections)
    assert '환승' in route.memo
    assert '출구' not in route.memo and '승강장' not in route.memo


@pytest.mark.asyncio
@pytest.mark.parametrize(('arrival', 'line', 'headsign'), [
    ('후암약수터', '402', '장지공영차고지'),
    ('청와대', '472', '신촌역'),
])
async def test_screenshot_direct_bus_is_two_short_actions_without_card_duplicates(endpoints, arrival, line, headsign):
    payload = response([transit('시청앞', arrival, line, headsign)])
    departure = datetime(2026, 10, 10, 4, 0, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
        route = await VerifiedMapsProvider(client=client, api_key='offline').route(*endpoints, mode='transit', departure_time=departure)
    assert all(fact in route.memo for fact in ('시청앞', arrival, line, headsign))
    assert '타세요' in route.memo and '내리세요' in route.memo
    assert len(route.memo) <= 120 and len(route.memo.splitlines()) <= 2
    assert not any(repeated in route.memo for repeated in ('목적지', '1250', '21분', '2026-10-10', 'UTC', '+00:00', '조회', '출발 기준', '상세 안내 생략', '지도'))


@pytest.mark.asyncio
async def test_compact_guidance_preserves_long_but_fitting_stop_identifiers_without_slicing(endpoints):
    start = '서울특별시시립문화전시관중앙정문앞정류장'
    end = '서울역사박물관국제문화교육센터동쪽정문정류장'
    line, headsign = '문화관광순환버스', '서울역사박물관국제문화교육센터'
    payload = response([transit(start, end, line, headsign)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
        route = await VerifiedMapsProvider(client=client, api_key='offline').route(*endpoints, mode='transit')
    assert all(identifier in route.memo for identifier in (start, end, line, headsign))
    assert len(route.memo) <= 240 and len(route.memo.splitlines()) <= 2
    assert route.memo.rstrip().endswith(('내리세요.', '하차하세요.'))


@pytest.mark.asyncio
async def test_unfittable_essential_identifiers_use_generic_action_instead_of_truncated_stop(endpoints):
    start = '가나다라마바사' * 20 + '시청앞'
    end = '아자차카타파하' * 20 + '최종정류장'
    payload = response([transit(start, end, '402', '장지공영차고지')])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
        route = await VerifiedMapsProvider(client=client, api_key='offline').route(*endpoints, mode='transit')
    assert len(route.memo) <= 120
    assert not any(fragment in route.memo for fragment in ('가나다', '아자차', '402', '장지공영차고지'))
    assert (route.minutes, route.distance) == (21, 1250)


@pytest.mark.asyncio
async def test_four_required_transit_connections_cannot_be_presented_as_three_line_complete_route(endpoints):
    payload = response([transit(f'{index}번승차역', f'{index}번하차역', f'{index}호선', f'{index}번방면') for index in range(1, 5)])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
        route = await VerifiedMapsProvider(client=client, api_key='offline').route(*endpoints, mode='transit')
    assert len(route.memo) <= 120 and len(route.memo.splitlines()) <= 3
    assert not any(partial in route.memo for partial in ('승차역', '하차역', '1호선', '2호선', '3호선', '4호선'))
    assert (route.minutes, route.distance) == (21, 1250)


@pytest.mark.asyncio
async def test_selected_walking_instruction_is_complete_not_cut_to_fit_card(endpoints):
    too_long = '세종대로를 따라 ' + '문화시설 입구를 지나 ' * 30 + '오른쪽으로 도세요'
    payload = response([walking(too_long), walking('율곡로에서 왼쪽으로 도세요')])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
        route = await VerifiedMapsProvider(client=client, api_key='offline').route(*endpoints)
    assert '율곡로에서 왼쪽으로 도세요' in route.memo
    assert '문화시설 입구' not in route.memo
    assert '주요 이동' in route.memo and len(route.memo) <= 240


@pytest.mark.asyncio
async def test_step_specific_distance_remains_useful_without_repeating_total_route_metrics(endpoints):
    payload = response([walking('세종대로를 따라 50m 이동한 뒤 오른쪽으로 도세요')])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
        route = await VerifiedMapsProvider(client=client, api_key='offline').route(*endpoints)
    assert '50m' in route.memo and '오른쪽으로 도세요' in route.memo
    assert '1250' not in route.memo and '21분' not in route.memo
    assert len(route.memo) <= 240 and len(route.memo.splitlines()) <= 3


@pytest.mark.asyncio
async def test_missing_optional_headsign_does_not_discard_known_boarding_and_alighting(endpoints):
    payload = response([transit('시청역', '안국역', '3호선', '')])
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))) as client:
        route = await VerifiedMapsProvider(client=client, api_key='offline').route(*endpoints, mode='transit')
    assert all(fact in route.memo for fact in ('시청역', '안국역', '3호선'))
    assert '방면' not in route.memo and '방향' not in route.memo
    assert len(route.memo) <= 120 and len(route.memo.splitlines()) <= 2


@pytest.mark.asyncio
async def test_invalid_metrics_are_route_data_failure_and_not_cached_as_success(endpoints):
    calls = 0

    def respond(request):
        nonlocal calls
        calls += 1
        payload = response([walking('정상 안내')])
        if calls == 1:
            payload['routes'][0]['duration'] = 'bad'
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        with pytest.raises(MapsProviderError) as error:
            await provider.route(*endpoints)
        assert error.value.kind == 'route_data' and not error.value.transient
        assert (await provider.route(*endpoints)).minutes == 21
    assert calls == 2


@pytest.mark.asyncio
async def test_navigation_fields_keep_supported_sku_but_advanced_options_stay_unknown(endpoints):
    options = None

    def respond(request):
        nonlocal options
        options = {'headers': {'X-Goog-FieldMask': request.headers['X-Goog-FieldMask']}, 'json': json.loads(request.content)}
        return httpx.Response(200, json=response([walking('직진')]))

    url = 'https://routes.googleapis.com/directions/v2:computeRoutes'
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        await provider.route(*endpoints)
        metrics = provider.metrics.snapshot()
    assert metrics['estimate_complete'] and metrics['skus']['routes_essentials']['requests'] == 1
    assert classify_maps_sku('POST', url, options) == 'routes_essentials'
    for extra in ({'routingPreference': 'TRAFFIC_AWARE_OPTIMAL'}, {'extraComputations': ['TRAFFIC_ON_POLYLINE']}, {'computeAlternativeRoutes': True}):
        modified = copy.deepcopy(options)
        modified['json'].update(extra)
        assert classify_maps_sku('POST', url, modified) == 'unknown'
    modified = copy.deepcopy(options)
    modified['headers']['X-Goog-FieldMask'] += ',routes.travelAdvisory'
    assert classify_maps_sku('POST', url, modified) == 'unknown'

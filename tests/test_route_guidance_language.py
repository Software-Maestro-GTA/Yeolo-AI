"""Reject mixed-language navigation without changing verified route metrics."""

import copy
import json
from typing import Any

import httpx
import pytest

from app.agent.tools.verified_maps import (
    InternalPlaceSchema,
    VerifiedMapsProvider,
    VerifiedPlace,
)
from app.services.maps_cost import classify_maps_sku
from app.services.route_guidance import fallback_route_guidance


def _walking(instruction: str) -> dict[str, Any]:
    return {'travelMode': 'WALK', 'navigationInstruction': {'instructions': instruction}}


def _transit(start: str, end: str, line: str, direction: str = '', *, instruction: str = '') -> dict[str, Any]:
    return {
        'travelMode': 'TRANSIT',
        'navigationInstruction': {'instructions': instruction},
        'transitDetails': {
            'stopDetails': {'departureStop': {'name': start}, 'arrivalStop': {'name': end}},
            'transitLine': {'nameShort': line, 'name': '버스 노선'},
            'headsign': direction,
        },
    }


async def _route_memo(steps: list[dict[str, Any]], *, language: str = 'ko', mode: str = 'transit') -> str | None:
    """Exercise the real adapter, serializer, cost counter and request cache offline."""
    payload = {'routes': [{'duration': '1201s', 'distanceMeters': 1250, 'legs': [{'steps': steps}]}]}
    original = copy.deepcopy(payload)
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=payload)

    endpoints = tuple(VerifiedPlace(InternalPlaceSchema(
        placeId=f'places/offline-{index}', placeName=name, category='museum',
        latitude=37.55 + index * .001, longitude=126.98,
    )) for index, name in enumerate(('출발지', '목적지')))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline', language=language)
        route = await provider.route(*endpoints, mode=mode)
        cached = await provider.route(*endpoints, mode=mode)
        metrics = provider.metrics.snapshot()
    assert route == cached and len(calls) == 1
    assert (route.type, route.minutes, route.distance) == (mode, 21, 1250)
    assert route.cost == (0 if mode == 'walking' else None)
    body = json.loads(calls[0].content)
    assert body['languageCode'] == language
    assert classify_maps_sku('POST', str(calls[0].url), {
        'headers': {'X-Goog-FieldMask': calls[0].headers['X-Goog-FieldMask']}, 'json': body,
    }) == 'routes_essentials'
    assert metrics['estimate_complete']
    assert set(metrics['skus']) == {'routes_essentials'}
    assert metrics['skus']['routes_essentials']['requests'] == 1
    assert metrics['skus']['routes_essentials']['cache_hits'] == 1
    assert payload == original
    assert route.memo is None or len(route.memo) <= 240 and '\n' not in route.memo
    return route.memo


@pytest.mark.asyncio
async def test_vietnam_screenshot_keeps_verified_line_code_without_mixed_stop_directions():
    memo = await _route_memo([
        _walking('북동쪽으로 걷기 사용이 제한된 도로 계단 이용.'),
        _transit('Số 131 Hoàng Diệu', 'Đối Diện 18-20', '07', 'Xuân Diệu - DN-HC0039'),
        _walking('Bạch Đằng에서 Lý Thường Kiệt 방면 북쪽으로 걷기 뒤 루츠 부티크 호텔 통과 (왼쪽 33m) 목적지는 길의 왼쪽에 있습니다.'),
    ])
    assert memo and '07' in memo and '지도' in memo
    assert not any(fragment in memo for fragment in (
        'Hoàng', 'Số', 'Diệu', 'Đối', 'Xuân', 'Bạch', 'Lý', 'Thường', 'DN-HC0039', '33m',
    ))


@pytest.mark.asyncio
async def test_localized_structured_transit_survives_unlocalized_optional_walking():
    memo = await _route_memo([
        _walking('Walk northeast toward City Hall.'),
        _transit('시청역', '종로3가역', '1호선', '소요산'),
        _walking('Transfer through the passage.'),
        _transit('종로3가역', '안국역', '3호선', '대화'),
        _walking('Head north along Bukchon-ro.'),
    ])
    assert memo and all(fact in memo for fact in ('시청역', '종로3가역', '1호선', '소요산', '3호선', '안국역', '대화'))
    assert '환승' in memo
    assert not any(fragment in memo for fragment in ('Walk', 'Transfer', 'Head', '지도'))


@pytest.mark.asyncio
async def test_complete_korean_instruction_precedes_foreign_structured_stop_names():
    instruction = '중앙역에서 07번 버스를 타고 박물관 앞에서 내리세요.'
    memo = await _route_memo([
        _transit('Số 131 Hoàng Diệu', 'Đối Diện 18-20', '07', 'Xuân Diệu', instruction=instruction),
        _walking('Walk east on Bạch Đằng.'),
    ])
    assert memo == instruction


@pytest.mark.asyncio
async def test_one_mixed_transit_connection_falls_back_for_whole_chain_with_all_line_codes():
    memo = await _route_memo([
        _transit('시청역', '중앙역', '07', '북쪽'),
        _transit('Central Station', 'Museum Station', 'M2.1', 'North'),
    ])
    assert memo and all(code in memo for code in ('07', 'M2.1')) and '지도' in memo
    assert memo.index('07') < memo.index('M2.1')
    assert not any(fragment in memo for fragment in ('시청역', '중앙역', 'Central', 'Museum', 'North'))


@pytest.mark.asyncio
@pytest.mark.parametrize('line', ['07', 'M2.1', 'JR', 'G09'])
async def test_verified_short_line_identifiers_survive_as_codes_without_translating_stops(line):
    memo = await _route_memo([_transit('Central Station', 'Museum Station', line, 'North')])
    assert memo and line in memo and '지도' in memo
    assert not any(fragment in memo for fragment in ('Central', 'Museum', 'North'))


@pytest.mark.asyncio
async def test_foreign_names_without_a_short_line_code_use_existing_localized_fallback():
    step = _transit('Hoàng Diệu', 'Bạch Đằng', '', 'Xuân Diệu')
    step['transitDetails']['transitLine']['name'] = 'Tuyến xe buýt số bảy'
    assert await _route_memo([step]) == fallback_route_guidance('transit', 'ko')


@pytest.mark.asyncio
@pytest.mark.parametrize(('language', 'instruction'), [
    ('ko', 'Turn right onto Museum Street.'),
    ('ko', 'GO TO 박물관.'),
    ('en', '박물관 거리에서 오른쪽으로 도세요.'),
    ('ja', 'Turn right onto Museum Street.'),
    ('zh-TW', 'Turn right onto Museum Street.'),
    ('fr', '박물관 거리에서 오른쪽으로 도세요.'),
    ('es', '박물관 거리에서 오른쪽으로 도세요.'),
    ('de', '박물관 거리에서 오른쪽으로 도세요.'),
    ('pt', '박물관 거리에서 오른쪽으로 도세요.'),
])
async def test_clearly_wrong_script_navigation_uses_requested_language_without_metric_changes(language, instruction):
    memo = await _route_memo([_walking(instruction)], language=language, mode='walking')
    assert memo == fallback_route_guidance('walking', language)
    assert memo is None or instruction not in memo


@pytest.mark.asyncio
async def test_english_transit_instruction_for_japanese_request_uses_japanese_with_verified_code():
    memo = await _route_memo([
        _transit('Central Station', 'Museum Station', 'G09', 'North', instruction='Take G09 to Museum Station.'),
    ], language='ja')
    assert memo and any('\u3040' <= char <= '\u30ff' for char in memo)
    assert not any(fragment in memo for fragment in ('Take', 'Central', 'Museum', 'North'))


@pytest.mark.asyncio
@pytest.mark.parametrize('unclassified_step', [{}, {'navigationInstruction': {'instructions': '걸어서 환승하세요.'}}])
async def test_missing_step_mode_does_not_claim_complete_connection_with_partial_line_codes(unclassified_step):
    memo = await _route_memo([
        _transit('Central Station', 'East Station', '07', 'East'),
        unclassified_step,
        _transit('East Station', 'Museum Station', 'G09', 'North'),
    ])
    assert memo == fallback_route_guidance('transit', 'ko')
    assert '07' not in memo and 'G09' not in memo

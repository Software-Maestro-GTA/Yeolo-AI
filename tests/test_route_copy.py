"""Exercise optional navigation translation over real Maps/Gateway wire formats."""

import asyncio
import json
from copy import deepcopy

import httpx
import pytest

from app.agent.tools.verified_maps import (
    InternalTransportSchema,
    VerifiedMapsProvider,
    VerifiedPlace,
)
from app.services.course_localization import localize_course
from app.services.route_copy import PROTECTED, NavigationText, RouteCopyPlan
from app.services.route_guidance import navigation_source
from tests.test_course_localization import (
    completion,
    course,
    facts,
    request,
    translated,
)
from tests.test_course_localization import wire as _wire

wire = _wire


def source_payload():
    return {'routes': [{'duration': '1201s', 'distanceMeters': 1250, 'legs': [{'steps': [
        {'travelMode': 'WALK', 'navigationInstruction': {'instructions': '북동쪽으로 걷기 사용이 제한된 도로 계단 이용.'}},
        {'travelMode': 'TRANSIT', 'transitDetails': {
            'stopDetails': {'departureStop': {'name': 'Số 131 Hoàng Diệu'}, 'arrivalStop': {'name': 'Đối Diện 18-20'}},
            'transitLine': {'nameShort': '07'}, 'headsign': 'Xuân Diệu - DN-HC0039',
        }},
        {'travelMode': 'WALK', 'navigationInstruction': {'instructions': 'Bạch Đằng에서 Lý Thường Kiệt 방면 북쪽으로 걷기 뒤 루츠 부티크 호텔 통과 (왼쪽 33m) 목적지는 길의 왼쪽에 있습니다.'}},
    ]}]}]}


def source_course():
    value = course()
    value.itinerary.days[0].stops[0].transportToNext = InternalTransportSchema(
        type='transit', distance=1250, minutes=21, cost=None,
        memo='지도 앱의 대중교통 길찾기를 열고 노선과 행선지를 확인해 타세요.',
        navigationSource=navigation_source(source_payload()),
    )
    return value


def korean_translation(outgoing):
    payload = json.loads(outgoing.content)
    source = json.loads(payload['messages'][-1]['content'].split('Verified source copy:\n')[1])
    assert len(source['navigation']) == 1  # One whole transfer memo, never text fragments.
    return {'navigation': [{'id': source['navigation'][0]['id'], 'text':
        '북동쪽으로 걸어가세요. 통행 제한 도로와 계단이 있는 구간입니다. '
        '호앙지에우 [[F0]]번지 정류장에서 [[F1]]번(쑤언지에우 - [[F2]] 방면)을 타고 [[F3]]~[[F4]]번지 맞은편 정류장에서 내리세요. '
        '박당 거리에서 리트엉끼엣 거리 쪽으로 북쪽을 향해 걸어가세요. 왼쪽 [[F5]]에 있는 루츠 부티크 호텔을 지나면 목적지는 길 왼쪽에 있습니다.'}]}


@pytest.mark.asyncio
async def test_provider_preserves_original_navigation_without_public_fields_or_extra_maps_calls():
    value = course()
    payload = source_payload()
    before = deepcopy(payload)
    calls = []

    def respond(outgoing):
        calls.append(outgoing)
        return httpx.Response(200, json=payload)

    venues = [VerifiedPlace(stop.place) for stop in value.itinerary.days[0].stops]
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline', language='ko')
        route = await provider.route(*venues, mode='transit')
        await provider.route(*venues, mode='transit')
        assert route.navigationSource == navigation_source(payload)
        value.itinerary.days[0].stops[0].transportToNext = route
        assert 'navigationSource' not in json.dumps(value.model_dump(context={'public_response': True}))
    assert len(calls) == 1 and payload == before
    assert (route.type, route.distance, route.minutes, route.cost) == ('transit', 1250, 21, None)


@pytest.mark.asyncio
async def test_korean_screenshot_is_naturally_translated_in_one_existing_gateway_call(wire):
    calls = []

    def respond(outgoing):
        calls.append(outgoing)
        return completion(korean_translation(outgoing))

    clients = wire(respond)
    value = source_course()
    before = facts(value)
    old_copy = (value.title, list(value.tags), value.itinerary.days[0].stops[0].reason)
    result = await localize_course(request('ko'), value)
    memo = result.itinerary.days[0].stops[0].transportToNext.memo
    assert all(text in memo for text in ('호앙지에우 131번지', '07번', '18~20번지', 'DN-HC0039', '33m', '북동쪽', '북쪽', '계단', '통행 제한', '목적지는 길 왼쪽'))
    assert not any(text in memo for text in ('Hoàng', 'Đối', 'Bạch', '지도 앱'))
    assert len(memo) <= 240 and '\n' not in memo
    assert facts(result) == before
    assert (value.title, value.tags, value.itinerary.days[0].stops[0].reason) == old_copy
    assert result.itinerary.days[0].stops[-1].transportToNext.memo is None
    assert len(calls) == 1 and all(client.is_closed for client in clients)
    assert calls[0].headers['x-abto-feature-id'] == 'course.candidates.generate'
    assert calls[0].headers['x-abto-device-id'] == str(request().userId)
    body = json.loads(calls[0].content)
    assert body['response_format']['json_schema']['strict'] is False
    schema = body['response_format']['json_schema']['schema']
    assert set(schema['properties']) == {'navigation'}
    assert set(schema['$defs']['NavigationText']['properties']) == {'id', 'text'}
    assert '1250' not in body['messages'][-1]['content']


@pytest.mark.asyncio
@pytest.mark.parametrize('damage', ['missing_number', 'new_number', 'wrong_script', 'missing_slot', 'duplicate_slot'])
async def test_invalid_translations_preserve_original_route_facts_and_still_return_course(wire, damage):
    def respond(outgoing):
        output = korean_translation(outgoing)
        slot = output['navigation'][0]
        if damage == 'missing_number':
            slot['text'] = slot['text'].replace('[[F0]]', '999')
        elif damage == 'new_number':
            slot['text'] += ' 3번 출구'
        elif damage == 'wrong_script':
            slot['text'] += ' Hoàng Diệu'
        elif damage == 'missing_slot':
            output['navigation'].pop(0)
        else:
            output['navigation'].append(dict(slot))
        return completion(output)

    clients = wire(respond)
    value = source_course()
    before = facts(value)
    result = await localize_course(request('ko'), value)
    memo = result.itinerary.days[0].stops[0].transportToNext.memo
    assert result is value and facts(result) == before
    assert 'Số 131 Hoàng Diệu' in memo and '07번' in memo and 'Đối Diện 18-20' in memo
    assert '999' not in memo and '3번 출구' not in memo
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['503', 'invalid_json', 'timeout'])
async def test_translation_failure_never_discards_verified_itinerary(wire, failure):
    calls = []

    async def respond(outgoing):
        calls.append(outgoing)
        if failure == '503':
            return httpx.Response(503, json={'error': {'message': 'offline failure'}})
        if failure == 'invalid_json':
            return completion({'invalid': True})
        await asyncio.Event().wait()

    clients = wire(respond)
    value = source_course()
    before = facts(value)
    result = await asyncio.wait_for(localize_course(request('ko'), value, timeout_seconds=.2), timeout=2)
    assert result is value and facts(result) == before
    assert 'Số 131 Hoàng Diệu' in result.itinerary.days[0].stops[0].transportToNext.memo
    assert len(calls) == 1 and all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_no_remaining_budget_preserves_source_without_model_setup(mocker):
    constructor = mocker.patch('app.services.course_localization.get_abto', side_effect=AssertionError('No budget'))
    value = source_course()
    result = await localize_course(request('ko'), value, timeout_seconds=0)
    assert result is value and '07번' in result.itinerary.days[0].stops[0].transportToNext.memo
    constructor.assert_not_called()


@pytest.mark.asyncio
async def test_real_caller_cancellation_propagates_and_closes_translation_clients(wire):
    entered = asyncio.Event()

    async def respond(outgoing):
        entered.set()
        await asyncio.Event().wait()

    clients = wire(respond)
    value = source_course()
    before = deepcopy(value.model_dump())
    task = asyncio.create_task(localize_course(request('ko'), value))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert value.model_dump() == before and all(client.is_closed for client in clients)


def test_repeated_complete_memos_are_deduplicated_and_source_transfer_order_is_kept():
    value = source_course()
    first = value.itinerary.days[0].stops[0]
    second = value.itinerary.days[0].stops[1]
    second.transportToNext = first.transportToNext.model_copy(deep=True)
    second.transportToNext.navigationSource = [
        first.transportToNext.navigationSource[1],
        {'kind': 'transit', 'start': 'Đối Diện 18-20', 'end': 'Số 131 Hoàng Diệu', 'line': 'G09', 'direction': ''},
    ]
    duplicate = first.model_copy(deep=True)
    duplicate.sequence = 3
    value.itinerary.days[0].stops.append(duplicate)
    plan = RouteCopyPlan(value, 'ko')
    assert len(plan.units) == 2  # Repeated full memos are sent only once.
    plan.apply()
    memo = second.transportToNext.memo
    assert memo.index('07번') < memo.index('G09') and '환승' in memo
    assert 'Số 131 Hoàng Diệu' in memo and 'Đối Diện 18-20' in memo


def test_invalid_fragment_does_not_discard_valid_peer_route_translation():
    value = source_course()
    second = value.itinerary.days[0].stops[1]
    second.transportToNext = InternalTransportSchema(
        type='walking', distance=700, minutes=12,
        navigationSource=[{'kind': 'instruction', 'text': 'Turn right.'}],
    )
    plan = RouteCopyPlan(value, 'ko')
    slot = next(unit['id'] for unit in plan.units if unit['text'] == 'Turn right.')
    plan.apply([NavigationText(id=slot, text='오른쪽으로 돌아가세요.')])
    assert 'Số 131 Hoàng Diệu' in value.itinerary.days[0].stops[0].transportToNext.memo
    assert second.transportToNext.memo == '오른쪽으로 돌아가세요.'


def test_long_source_is_preserved_whole_instead_of_generic_fallback_or_slicing():
    value = source_course()
    route = value.itinerary.days[0].stops[0].transportToNext
    text = 'Turn right along ' + 'Museum Street ' * 20 + 'and use the stairs.'
    route.navigationSource = [{'kind': 'instruction', 'text': text}]
    plan = RouteCopyPlan(value, 'ko')
    plan.apply()
    assert route.memo == text and len(route.memo) > 240
    plan.apply([NavigationText(id='n0', text='박물관 거리에서 오른쪽으로 돌아 계단을 이용하세요.')])
    assert route.memo == '박물관 거리에서 오른쪽으로 돌아 계단을 이용하세요.'


@pytest.mark.asyncio
async def test_other_locale_shares_existing_course_copy_call_with_navigation(wire):
    calls = []

    def respond(outgoing):
        calls.append(outgoing)
        return completion({
            'title': '서울の旅', 'tags': ['文化', '自然'],
            'stops': [
                {'day': 1, 'sequence': i + 1, 'placeId': stop.place.placeId,
                 'reason': f'{stop.place.placeName}で見学を楽しんでください。', 'memo': '現地の案内を確認してください。'}
                for i, stop in enumerate(value.itinerary.days[0].stops)
            ],
            'navigation': [
                {'id': 'n0', 'text': '博物館通りで右に曲がってください。'},
                {'id': 'n1', 'text': '博物館'},
                {'id': 'n2', 'text': '公園'},
            ],
        })

    value = source_course()
    value.itinerary.days[0].stops[0].transportToNext.navigationSource = [
        {'kind': 'instruction', 'text': 'Turn right onto Museum Street.'},
    ]
    before = facts(value)
    wire(respond)
    result = await localize_course(request('ja'), value)
    assert len(calls) == 1 and facts(result) == before
    assert result.itinerary.days[0].stops[0].transportToNext.memo == '博物館通りで右に曲がってください。'
    body = json.loads(calls[0].content)
    assert set(body['response_format']['json_schema']['schema']['properties']) == {'title', 'tags', 'stops', 'navigation'}


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ko', 'ko-KR'])
async def test_foreign_place_names_and_legacy_memo_are_localized_together_on_public_output(wire, language):
    value = course()
    first, second = value.itinerary.days[0].stops
    first.place.placeName = 'Roots Boutique Hotel'
    second.place.placeName = 'Bạch Đằng'
    first.reason = 'Roots Boutique Hotel에서 여행을 시작해 보세요.'
    first.memo = 'Bạch Đằng 입구에서 현장 안내를 확인하세요.'
    first.transportToNext.memo = 'Roots Boutique Hotel에서 Số 131 Hoàng Diệu까지 07번을 타고 이동하세요.'
    before = facts(value)
    calls = []

    def respond(outgoing):
        calls.append(outgoing)
        body = json.loads(outgoing.content)
        source = json.loads(body['messages'][-1]['content'].split('Verified source copy:\n')[1])
        mapping = {
            'Roots Boutique Hotel': '루츠 부티크 호텔',
            'Bạch Đằng': '박당 거리',
            'Bạch Đằng 입구에서 현장 안내를 확인하세요.': '박당 거리 입구에서 현장 안내를 확인하세요.',
            'Roots Boutique Hotel에서 Số [[F0]] Hoàng Diệu까지 [[F1]]번을 타고 이동하세요.':
                '루츠 부티크 호텔에서 호앙지에우 [[F0]]번지까지 [[F1]]번을 타고 이동하세요.',
        }
        return completion({'navigation': [{'id': slot['id'], 'text': mapping[slot['text']]} for slot in source['navigation']]})

    wire(respond)
    result = await localize_course(request(language), value)
    public = result.model_dump(context={'public_response': True})
    stops = public['itinerary']['days'][0]['stops']
    assert facts(result) == before and len(calls) == 1
    assert stops[0]['place']['placeName'] == '루츠 부티크 호텔'
    assert stops[1]['place']['placeName'] == '박당 거리'
    assert stops[0]['reason'] == '루츠 부티크 호텔에서 여행을 시작해 보세요.'
    assert stops[0]['memo'] == '박당 거리 입구에서 현장 안내를 확인하세요.'
    assert stops[0]['transportToNext']['memo'] == '루츠 부티크 호텔에서 호앙지에우 131번지까지 07번을 타고 이동하세요.'
    assert not any(text in json.dumps(public, ensure_ascii=False) for text in ('Roots Boutique Hotel', 'Bạch Đằng', 'Hoàng Diệu'))


@pytest.mark.asyncio
async def test_name_translation_outage_preserves_original_names_and_legacy_memo(wire):
    wire(lambda _: httpx.Response(503, json={'error': {'message': 'Offline outage'}}))
    value = course()
    stop = value.itinerary.days[0].stops[0]
    stop.place.placeName = 'Roots Boutique Hotel'
    stop.transportToNext.memo = 'Roots Boutique Hotel에서 북쪽으로 걸어가세요.'
    before = facts(value)
    result = await localize_course(request('ko'), value)
    assert result is value and facts(result) == before
    assert stop.place.placeName == 'Roots Boutique Hotel'
    assert stop.transportToNext.memo == 'Roots Boutique Hotel에서 북쪽으로 걸어가세요.'


@pytest.mark.asyncio
async def test_same_latin_script_names_and_memo_are_still_translated_to_english(wire):
    value = course()
    first, second = value.itinerary.days[0].stops
    first.place.placeName = 'Bảo tàng Đà Nẵng'
    second.place.placeName = 'Sông Hàn'
    first.reason = 'Bảo tàng Đà Nẵng에서 전시를 감상해 보세요.'
    second.reason = 'Sông Hàn에서 산책을 즐겨 보세요.'
    first.transportToNext.memo = 'Đi về phía bắc trên đường Bạch Đằng.'
    before = facts(value)

    def respond(outgoing):
        body = json.loads(outgoing.content)
        source = json.loads(body['messages'][-1]['content'].split('Verified source copy:\n')[1])
        mapping = {
            'Đi về phía bắc trên đường Bạch Đằng.': 'Head north along Bach Dang Street.',
            'Bảo tàng Đà Nẵng': 'Da Nang Museum',
            'Sông Hàn': 'Han River',
        }
        return completion({
            'title': 'A day in Da Nang', 'tags': ['Culture', 'Nature'],
            'stops': [
                {'day': 1, 'sequence': stop.sequence, 'placeId': stop.place.placeId,
                 'reason': f'Explore {stop.place.placeName} during your visit.', 'memo': 'Follow the on-site visitor guidance.'}
                for stop in (first, second)
            ],
            'navigation': [{'id': slot['id'], 'text': mapping[slot['text']]} for slot in source['navigation']],
        })

    wire(respond)
    result = await localize_course(request('en'), value)
    assert facts(result) == before
    assert first.place.placeName == 'Da Nang Museum' and second.place.placeName == 'Han River'
    assert first.reason == 'Explore Da Nang Museum during your visit.'
    assert first.transportToNext.memo == 'Head north along Bach Dang Street.'


JAPANESE_WALK = [
    '劇場通り에서 トキワ通り 방면 남쪽으로 걷기.',
    'カラオケパセラ池袋西口店(오른쪽) 지나서 좌회전.',
    '좌회전.',
]
KOREAN_WALK = '게키조 거리에서 남쪽의 도키와 거리 방향으로 걸어가세요. 오른쪽에 있는 가라오케 파세라 이케부쿠로 서쪽 출구점을 지나 좌회전한 뒤, 다시 한 번 좌회전하세요.'


@pytest.mark.asyncio
async def test_reported_japanese_walking_memo_is_repaired_without_losing_translated_place_name(wire):
    value = course()
    stop = value.itinerary.days[0].stops[0]
    stop.place.placeName = 'カラオケパセラ池袋西口店'
    stop.transportToNext = InternalTransportSchema(
        type='walking', distance=700, minutes=12, cost=0,
        memo=' '.join(JAPANESE_WALK),
        navigationSource=[{'kind': 'instruction', 'text': text} for text in JAPANESE_WALK],
    )
    before = facts(value)
    calls = []

    def respond(outgoing):
        body = json.loads(outgoing.content)
        source = json.loads(body['messages'][-1]['content'].split('Verified source copy:\n')[1])
        calls.append(source)
        if len(calls) == 1:
            return completion({'navigation': [
                {'id': slot['id'], 'text': '가라오케 파세라 이케부쿠로 서쪽 출구점' if slot['kind'] == 'name' else slot['text']}
                for slot in source['navigation']
            ]})
        assert len(source['navigation']) == 1  # Only the rejected walk needs repair.
        return completion({'navigation': [{'id': source['navigation'][0]['id'], 'text': KOREAN_WALK}]})

    clients = wire(respond)
    result = await localize_course(request('ko'), value)
    assert len(calls) == 2 and all(client.is_closed for client in clients)
    assert calls[0]['navigation'][0]['text'] == ' '.join(JAPANESE_WALK)
    assert stop.place.placeName == '가라오케 파세라 이케부쿠로 서쪽 출구점'
    assert stop.transportToNext.memo == KOREAN_WALK and facts(result) == before
    assert not any(char in stop.transportToNext.memo for char in ('劇', 'ト', 'カ'))


def test_valid_long_translation_survives_missing_peer_memo_and_repair_failure():
    value = source_course()
    value.itinerary.days[0].stops[0].transportToNext.navigationSource = [
        {'kind': 'instruction', 'text': 'Head northeast via the restricted road and stairs.'},
    ]
    value.itinerary.days[0].stops[1].transportToNext = InternalTransportSchema(
        type='walking', distance=100, minutes=2,
        navigationSource=[{'kind': 'instruction', 'text': 'Turn right onto Museum Street.'}],
    )
    plan = RouteCopyPlan(value, 'ko')
    long_text = '북동쪽으로 걸어가세요. ' + '통행이 제한된 도로와 계단을 이용하는 구간을 따라 이동하세요. ' * 9
    plan.apply([NavigationText(id='n0', text=long_text)])
    route = value.itinerary.days[0].stops[0].transportToNext
    assert len(route.memo) > 240 and route.memo.startswith(long_text.strip())
    assert value.itinerary.days[0].stops[1].transportToNext.memo == 'Turn right onto Museum Street.'
    plan.apply()  # Repair failure must not erase an accepted translation.
    assert route.memo.startswith(long_text.strip())


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['503', 'timeout'])
async def test_failed_repair_keeps_successfully_localized_copy_and_course(wire, failure):
    calls = []

    async def respond(outgoing):
        calls.append(outgoing)
        if len(calls) == 1:
            output = translated()
            output['navigation'].pop()  # Only the park name needs repair.
            return completion(output)
        if failure == '503':
            return httpx.Response(503, json={'error': {'message': 'Offline repair unavailable'}})
        await asyncio.Event().wait()

    clients = wire(respond)
    value = course()
    before = facts(value)
    result = await asyncio.wait_for(localize_course(request('en'), value, timeout_seconds=.2), timeout=2)
    assert len(calls) == 2 and all(client.is_closed for client in clients)
    assert result is value and facts(result) == before
    stop = result.itinerary.days[0].stops[0]
    assert stop.place.placeName == 'Verified Museum'
    assert stop.reason == 'Visit Verified Museum for an exhibition experience.'
    assert result.title == translated()['title']


@pytest.mark.asyncio
@pytest.mark.parametrize('structured', [True, False])
async def test_chinese_report_is_translated_as_one_complete_memo_over_existing_gateway_route(wire, structured):
    original = '雁荡路에서 南昌路 방면 북쪽으로 걷기. 우회전하여 南昌路에 진입. 좌회전하여 黄陂南路에 진입 목적지는 길의 왼쪽에 있습니다.'
    # Recorded from the real Gemini response after correcting the feature route.
    localized = '옌당루에서 난창루 방면 북쪽으로 걷다가 우회전하여 난창루로 진입한 후, 좌회전하여 황피난루로 들어가세요. 목적지는 길 왼쪽에 있습니다.'
    value = course()
    first, second = value.itinerary.days[0].stops
    first.place.placeName, second.place.placeName = '雁荡路', '黄陂南路'
    first.transportToNext = InternalTransportSchema(
        type='walking', distance=700, minutes=12, cost=0, memo=original,
        navigationSource=[{'kind': 'instruction', 'text': part + '.'} for part in original.rstrip('.').split('. ')] if structured else [],
    )
    before = facts(value)
    calls = []

    def respond(outgoing):
        calls.append(outgoing)
        if outgoing.headers['x-abto-feature-id'] != 'course.candidates.generate':
            return httpx.Response(400, json={'error': {'message': 'missing outbound provider key'}})
        body = json.loads(outgoing.content)
        source = json.loads(body['messages'][-1]['content'].split('Verified source copy:\n')[1])
        memos = [unit for unit in source['navigation'] if unit['kind'] == 'instruction']
        assert len(memos) == 1 and memos[0]['text'] == original
        names = {'雁荡路': '옌당루', '黄陂南路': '황피난루'}
        return completion({'navigation': [{'id': unit['id'], 'text': localized if unit['kind'] == 'instruction' else names[unit['text']]}
                                          for unit in source['navigation']]})

    wire(respond)
    result = await localize_course(request('ko'), value)
    assert len(calls) == 1 and facts(result) == before
    assert first.transportToNext.memo == localized
    assert (first.place.placeName, second.place.placeName) == ('옌당루', '황피난루')


def test_complete_transit_memo_protects_alphabetic_line_codes_and_all_numeric_tokens():
    value = source_course()
    route = value.itinerary.days[0].stops[0].transportToNext
    route.navigationSource = [{'kind': 'transit', 'start': '新宿', 'end': '東京', 'line': 'JR', 'direction': '中央線'}]
    plan = RouteCopyPlan(value, 'ko')
    assert len(plan.units) == 1 and 'JR' not in plan.units[0]['text']
    plan.apply([NavigationText(id='n0', text='신주쿠에서 [[F0]] 주오선 방면 열차를 타고 도쿄에서 내리세요.')])
    assert route.memo == '신주쿠에서 JR 주오선 방면 열차를 타고 도쿄에서 내리세요.'


@pytest.mark.parametrize('source,expected', [
    ('Tempelhofer Damm/B96 방면', ['B96']),
    ('Tempelhofer Damm/B96에 진입', ['B96']),
    ('Str. des 17. Juni/B2/B5에 진입', ['17', 'B2/B5']),
    ('M19(U Mehringdamm 방면)', ['M19']),
    ('G09에서 33m를 걸어가세요.', ['G09', '33m']),
])
def test_identifiers_next_to_korean_particles_and_separators_remain_complete(source, expected):
    assert [match.group() for match in PROTECTED.finditer(source)] == expected


def test_berlin_road_code_translation_survives_korean_script_validation():
    value = course()
    route = value.itinerary.days[0].stops[0].transportToNext
    route.memo = 'Methfesselstraße 방면 동쪽으로 걷기. 좌회전하여 Methfesselstraße에 진입. 좌회전하여 Mehringdamm/B96에 진입 목적지는 길의 오른쪽에 있습니다.'
    plan = RouteCopyPlan(value, 'ko')
    assert plan.protected['n0'] == ['B96']
    assert 'B[[F0]]' not in plan.units[0]['text']
    plan.apply([NavigationText(id='n0', text='메트페셀슈트라세 방면 동쪽으로 걸어 좌회전한 뒤, 다시 좌회전하여 메링담/[[F0]]에 진입하세요. 목적지는 도로 오른쪽에 있습니다.')])
    assert '메링담/B96에' in route.memo and 'Methfesselstraße' not in route.memo


@pytest.mark.parametrize('code,particle', [('B96', '으로'), ('M19', '로'), ('B2/B5', '로')])
def test_hidden_road_identifiers_resolve_unfinished_korean_particles(code, particle):
    value = course()
    route = value.itinerary.days[0].stops[0].transportToNext
    route.memo = f'우회전하여 Road/{code}에 진입.'
    plan = RouteCopyPlan(value, 'ko')
    plan.apply([NavigationText(id='n0', text='우회전하여 도로/[[F0]](으)로 들어서세요.')])
    assert route.memo == f'우회전하여 도로/{code}{particle} 들어서세요.'

"""Verify Gemini-through-ABTO requests using real LangChain and offline transports."""

import asyncio
import json
from uuid import UUID

import httpx
import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.exceptions import OutputParserException
from langchain_core.runnables import RunnableLambda
from openai import APIStatusError

from app.agent import course_drafting, taste_profile_chains
from app.core.config import settings
from app.core.llm import get_abto
from app.schemas.course import CourseRequestSchema
from app.schemas.taste_profile import TasteProfileAnalysisOutput
from app.schemas.taste_summary import TasteSummaryDraft

USER_ID = UUID('550e8400-e29b-41d4-a716-446655440000')
FEATURES = {'taste': 'taste.profile.analysis', 'summary': 'taste.profile.analysis', 'candidates': 'course.candidates.generate'}


def output_for(operation):
    if operation == 'summary':
        return {
            'intro': '카페와 디저트를 즐기는 여행자예요.', 'hashtags': ['#카페', '#디저트', '#여행'],
            'types': [{'typeCode': code, 'typeName': code} for code in ('CAFE_DESSERT', 'CULTURE', 'SHOPPING')],
            'recommendCity': {'countryName': '대한민국', 'cityName': '서울', 'reason': '카페에 대한 관심을 바탕으로 제안해요.'},
        }
    if operation == 'candidates':
        return {'title': '서울 여행', 'days': [
            {'candidates': [{'name': '국립중앙박물관'}, {'name': '서울숲'}]},
        ]}
    output = {
        name: dict.fromkeys(TasteProfileAnalysisOutput.model_fields[name].annotation.model_fields, 3)
        for name in ('travelPurpose', 'preferredLocationType', 'activityPreference', 'foodPreference')
    }
    output.update(travelPaceDensity='balanced', spendingTendency='moderate',
                  companionType='solo', seasonalEnvironmentPreference=['warm_region'])
    return output


def completion(output):
    return httpx.Response(200, headers={'x-abto-request-id': 'offline-request'}, json={
        'id': 'chatcmpl-offline', 'object': 'chat.completion', 'created': 0,
        'model': 'gemini-3.8-flash',
        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': json.dumps(output)}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15},
    })


async def invoke(operation, user_id=USER_ID):
    if operation == 'taste':
        return await taste_profile_chains.generate_taste_profile('{}', user_id=user_id)
    if operation == 'summary':
        return await taste_profile_chains.generate_taste_summary('{}', user_id=user_id)
    request = CourseRequestSchema.model_validate({
        'userId': str(user_id), 'language': 'en-US', 'mbti': 'INTJ',
        'tripCondition': {'destinationCountry': '대한민국', 'destinationCity': '서울',
                          'startDate': '2026-10-17', 'totalDays': 1, 'budget': 900000},
    })
    return await course_drafting._draft_day_candidates(request, [], day_index=0, seed='offline')


@pytest.fixture
def offline_wire(mocker):
    """Keep SDK-owned clients and auth injection real; replace only network I/O."""
    clients = []
    abto = get_abto()
    for method in ('openai_options', 'async_openai_options'):
        original = getattr(abto, method)
        def record(*args, _original=original, **kwargs):
            options = _original(*args, **kwargs)
            clients.append(options['http_client'])
            return options
        mocker.patch.object(abto, method, side_effect=record)

    def install(respond):
        mocker.patch('httpx._client.HTTPTransport', side_effect=lambda **kwargs: httpx.MockTransport(
            lambda request: pytest.fail('Async model invocation must not perform sync I/O'),
        ))
        mocker.patch('httpx._client.AsyncHTTPTransport', side_effect=lambda **kwargs: httpx.MockTransport(respond))
        return clients
    return install


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['taste', 'summary', 'candidates'])
async def test_gemini_gateway_request_preserves_schema_parameters_and_identity(operation, offline_wire, mocker):
    requests = []
    mocker.patch.object(settings, 'GEMINI_MODEL_NAME', 'gemini-3.8-flash')
    def respond(request):
        requests.append(request)
        return completion(output_for(operation))
    clients = offline_wire(respond)
    result = await invoke(operation)
    output_schema = {'taste': TasteProfileAnalysisOutput, 'summary': TasteSummaryDraft, 'candidates': course_drafting.CourseDraft}[operation]
    assert result.model_dump() == output_schema.model_validate(output_for(operation)).model_dump()
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == 'https://gateway.abto.app/v1/chat/completions'
    assert request.headers['authorization'] == 'Bearer ck-abto-offline'
    assert request.headers['x-abto-key-gemini'] == 'offline-gemini-key'
    assert 'x-abto-key-openai' not in request.headers
    assert request.headers['x-abto-device-id'] == str(USER_ID)
    assert request.headers['x-abto-feature-id'] == FEATURES[operation]
    assert request.extensions['timeout']['read'] == 60.
    body = json.loads(request.content)
    assert body['model'] == 'gemini-3.8-flash'
    assert body['stream'] is False
    assert body['response_format']['type'] == 'json_schema'
    assert body['response_format']['json_schema']['schema']['properties']
    if operation == 'summary':
        assert '항상 한국어로 작성하세요' in body['messages'][0]['content']
        summary_schema = body['response_format']['json_schema']['schema']
        assert summary_schema['properties']['hashtags']['minItems'] == 3
        assert summary_schema['properties']['hashtags']['maxItems'] == 4
        assert summary_schema['properties']['types']['minItems'] == 3
        assert summary_schema['properties']['types']['maxItems'] == 3
        assert 'score' not in summary_schema['$defs']['SummaryTypeCopy']['properties']
        assert 'lowName' not in summary_schema['$defs']['SummaryTypeCopy']['properties']
        city_schema = summary_schema['$defs']['CityRecommendation']
        assert set(city_schema['required']) == {'countryName', 'cityName', 'reason'}
        assert set(city_schema['properties']) == {'countryName', 'cityName', 'reason'}
        assert all('enum' not in field for field in city_schema['properties'].values())
        assert city_schema['properties']['reason']['maxLength'] == 60
        assert '최대 60자의 한 문장' in body['messages'][0]['content']
        assert '줄바꿈 없이 작성하세요' in body['messages'][0]['content']
    assert not {'temperature', 'top_p', 'top_k', 'candidate_count', 'generation_config', 'thinking_budget',
                'parallel_tool_calls', 'tools', 'stream_options', 'metadata', 'store'} & body.keys()
    allowed = {'model', 'messages', 'stream', 'response_format', 'max_completion_tokens', 'reasoning_effort'}
    assert set(body) <= allowed
    if operation == 'candidates':
        assert body['reasoning_effort'] == 'low'
        assert body['max_completion_tokens'] == 5000
        candidate_schema = body['response_format']['json_schema']['schema']
        assert 'reason' not in candidate_schema['properties']
        assert 'reason' not in candidate_schema['$defs']['Candidate']['properties']
        messages = ' '.join(message['content'] for message in body['messages'])
        assert 'reason은 25자' not in messages and '코드에서 재구성' not in messages
        assert 'reason이나 memo를 작성하지 마세요' in messages
        assert 'language: ko' in messages and 'language: en-US' not in messages
        assert '제목, 태그는 항상 한국어로 작성하세요' in messages
        anchor_schema = candidate_schema['$defs']['AnchorCandidates']
        assert set(anchor_schema['properties']) == {'airports', 'lodgings'}
        assert all(field['maxItems'] == 2 for field in anchor_schema['properties'].values())
        assert set(candidate_schema['$defs']['AirportCandidate']['properties']) == {
            'name', 'english_name', 'iata_code',
        }
        assert set(candidate_schema['$defs']['NamedLodgingCandidate']['properties']) == {
            'name', 'english_name',
        }
    else:
        assert 'reasoning_effort' not in body
        assert 'max_completion_tokens' not in body
    assert get_abto().get_headers() == {}
    assert len(clients) == 2 and all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_airport_and_lodging_proposals_share_the_existing_candidate_wire_request(offline_wire):
    requests = []
    output = output_for('candidates')
    output['anchor_candidates'] = {
        'airports': [{'name': '하네다 공항', 'english_name': 'Haneda Airport', 'iata_code': 'HND'}],
        'lodgings': [{'name': '검증할 숙소', 'english_name': 'Unverified Property'}],
    }

    def respond(request):
        requests.append(request)
        return completion(output)

    clients = offline_wire(respond)
    request = CourseRequestSchema.model_validate({
        'userId': str(USER_ID), 'language': 'ko', 'mbti': 'INTJ',
        'tripCondition': {'destinationCountry': '일본', 'destinationCity': '도쿄',
                          'startDate': '2026-10-17', 'totalDays': 3, 'budget': 900000},
    })
    result = await course_drafting._draft_day_candidates(
        request, [], day_index=0, seed='offline',
        generate_airports=True, generate_lodgings=True,
        day_hints=['1일차 동쪽', '2일차 서쪽', '3일차 도심'],
    )
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    prompt = '\n'.join(message['content'] for message in body['messages'])
    assert '전체 여행의 anchor_candidates도 함께 제안' in prompt
    assert '3일차 도심' in prompt and '900000' in prompt and '도쿄' in prompt
    assert '관광·식사 후보 9~10개와 최소 방문 5곳' in prompt
    assert result.anchor_candidates.airports[0].iata_code == 'HND'
    assert result.anchor_candidates.lodgings[0].name == '검증할 숙소'
    assert body['max_completion_tokens'] == 5000
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['taste', 'summary', 'candidates'])
@pytest.mark.parametrize('outcome', ['failure', 'cancellation'])
async def test_gateway_failure_has_no_direct_fallback_and_closes_clients(operation, outcome, offline_wire):
    entered = asyncio.Event()
    requests = []
    async def respond(request):
        requests.append(request)
        entered.set()
        if outcome == 'cancellation':
            await asyncio.Event().wait()
        return httpx.Response(503, headers={'x-abto-error-source': 'gateway'}, json={
            'error': {'message': 'offline gateway failure', 'type': 'server_error'},
        })
    clients = offline_wire(respond)
    task = asyncio.create_task(invoke(operation))
    await asyncio.wait_for(entered.wait(), timeout=2)
    if outcome == 'cancellation':
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(APIStatusError) as raised:
            await asyncio.wait_for(task, timeout=2)
        assert raised.value.status_code == 503
        # Application error logs use the message; raw HTTP headers are not logged.
        assert 'ck-abto-offline' not in str(raised.value)
        assert 'offline-gemini-key' not in str(raised.value)
    assert len(requests) == 1
    assert len(clients) == 2 and all(client.is_closed for client in clients)
    assert get_abto().get_headers() == {}


@pytest.mark.asyncio
async def test_invalid_daily_output_closes_clients(offline_wire):
    clients = offline_wire(lambda _: completion({'title': '후보', 'days': []}))
    with pytest.raises(OutputParserException, match='assigned day'):
        await invoke('candidates')
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
@pytest.mark.parametrize('key', ['GEMINI_API_KEY', 'ABTO_CALLING_KEY'])
@pytest.mark.parametrize('operation', ['taste', 'summary', 'candidates'])
async def test_missing_credentials_create_no_model(operation, key, mocker):
    mocker.patch.object(settings, key, '')
    module = taste_profile_chains if operation in {'taste', 'summary'} else course_drafting
    constructor = mocker.patch.object(module, 'ChatOpenAI')
    with pytest.raises(ValueError, match='credentials'):
        await invoke(operation)
    constructor.assert_not_called()


@pytest.mark.asyncio
async def test_parallel_features_keep_user_context_isolated(offline_wire):
    requests = []
    ready = asyncio.Event()
    async def respond(request):
        requests.append(request)
        if len(requests) == 2:
            ready.set()
        await ready.wait()
        kind = 'taste' if request.headers['x-abto-feature-id'] == FEATURES['taste'] else 'candidates'
        return completion(output_for(kind))
    clients = offline_wire(respond)
    second_id = UUID('660e8400-e29b-41d4-a716-446655440001')
    await asyncio.wait_for(asyncio.gather(invoke('taste'), invoke('candidates', second_id)), timeout=3)
    assert {(request.headers['x-abto-feature-id'], request.headers['x-abto-device-id']) for request in requests} == {
        (FEATURES['taste'], str(USER_ID)), (FEATURES['candidates'], str(second_id)),
    }
    assert len(clients) == 4 and all(client.is_closed for client in clients)
    assert get_abto().get_headers() == {}


@pytest.mark.asyncio
async def test_langchain_callbacks_keep_metadata_and_hide_keys(offline_wire):
    starts, ends = [], []
    class Capture(BaseCallbackHandler):
        def on_chat_model_start(self, serialized, messages, **kwargs):
            starts.append((serialized, messages, kwargs))
        def on_llm_end(self, response, **kwargs):
            ends.append(response)
    offline_wire(lambda _: completion(output_for('taste')))
    async def run(_):
        return await invoke('taste')
    await RunnableLambda(run).ainvoke(
        {}, config={'callbacks': [Capture()], 'tags': ['existing-trace'], 'metadata': {'test_case': 'existing'}},
    )
    assert len(starts) == len(ends) == 1
    assert 'existing-trace' in starts[0][2]['tags']
    assert starts[0][2]['metadata']['test_case'] == 'existing'
    serialized = json.dumps(starts[0][0], default=str)
    assert 'ck-abto-offline' not in serialized and 'offline-gemini-key' not in serialized
    assert ends[0].generations[0][0].message.usage_metadata['total_tokens'] == 15


@pytest.mark.asyncio
async def test_analysis_then_summary_pipeline_over_real_sdk_wire(offline_wire, mocker):
    from httpx import ASGITransport, AsyncClient

    from app.main import app
    from tests.test_behavior_evidence import (
        full_profile,
        repeated_photos,
        request,
        sse_events,
    )
    from tests.test_taste_summary import draft_for

    requests = []
    summary_contexts = []
    mocker.patch.object(settings, 'INTERNAL_API_KEY', 'offline-api-key')
    mocker.patch.object(settings, 'GOOGLE_MAPS_API_KEY', '')

    def respond(http_request):
        requests.append(http_request)
        body = json.loads(http_request.content)
        schema_name = body['response_format']['json_schema']['name']
        if schema_name == 'TasteProfileAnalysisOutput':
            assert len(requests) == 1
            return completion(full_profile(['warm_region']).model_dump())
        assert schema_name == 'TasteSummaryDraft'
        assert len(requests) == 2
        city_schema = body['response_format']['json_schema']['schema']['$defs']['CityRecommendation']['properties']
        assert set(city_schema) == {'countryName', 'cityName', 'reason'}
        assert all('enum' not in field for field in city_schema.values())
        context = body['messages'][-1]['content'].split('\n', 1)[1]
        summary_contexts.append(json.loads(context))
        draft = draft_for(context)
        return completion(draft.model_dump())

    clients = offline_wire(respond)
    payload = request(repeated_photos()).model_dump(mode='json')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/internal/ai/taste-profile/analysis', json=payload,
                                     headers={'X-Internal-Api-Key': 'offline-api-key'})
    events = sse_events(response)
    assert [kind for kind, _ in events] == ['progress', 'progress', 'complete']
    assert [payload['step'] for kind, payload in events if kind == 'progress'] == ['ANALYZING_PREFERENCE', 'GENERATING_SUMMARY']
    result = events[-1][1]
    assert set(result) == {'tasteProfile', 'summary'}
    assert result['tasteProfile']['companionType'] == 'solo'
    assert result['summary']['types'] == [{
        'typeCode': code, 'typeName': '나만의 ' + code, 'lowName': '약함', 'highName': '강함', 'score': 100,
    } for code in ('CAFE_DESSERT', 'CULTURE', 'SHOPPING')]
    assert result['summary']['recommendCity']['countryName'] == '슬로베니아'
    assert result['summary']['recommendCity']['cityName'] == '류블랴나'
    assert 'cityCode' not in result['summary']['recommendCity']
    assert summary_contexts[0]['types'][0]['visitCount'] == 3
    assert 'cityCandidates' not in summary_contexts[0]
    assert {'CAFE_DESSERT', 'CULTURE', 'SHOPPING'} <= {
        interest['typeCode'] for interest in summary_contexts[0]['recommendationInterests']
    }
    serialized = json.dumps(summary_contexts)
    assert all(key not in serialized for key in ('companionType', 'warm_region', 'sourceImageId', 'capturedAt', 'placeName'))
    assert len(requests) == 2
    assert all(str(req.url) == 'https://gateway.abto.app/v1/chat/completions' for req in requests)
    assert all(req.headers['x-abto-feature-id'] == 'taste.profile.analysis' for req in requests)
    assert all(req.headers['x-abto-device-id'] == str(USER_ID) for req in requests)
    assert len(clients) == 4 and all(client.is_closed for client in clients)

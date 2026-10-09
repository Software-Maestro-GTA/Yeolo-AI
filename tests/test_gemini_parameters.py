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

USER_ID = UUID('550e8400-e29b-41d4-a716-446655440000')
FEATURES = {'taste': 'taste.profile.analysis', 'candidates': 'course.candidates.generate'}


def output_for(operation):
    if operation == 'candidates':
        return {'title': '서울 여행', 'reason': '문화 탐방', 'days': [
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
    request = CourseRequestSchema.model_validate({
        'userId': str(user_id), 'mbti': 'INTJ',
        'tripCondition': {'destinationCountry': '대한민국', 'destinationCity': '서울',
                          'startDate': '2026-10-17', 'totalDays': 1, 'budgetType': 'moderate'},
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
@pytest.mark.parametrize('operation', ['taste', 'candidates'])
async def test_gemini_gateway_request_preserves_schema_parameters_and_identity(operation, offline_wire, mocker):
    requests = []
    mocker.patch.object(settings, 'GEMINI_MODEL_NAME', 'gemini-3.8-flash')
    def respond(request):
        requests.append(request)
        return completion(output_for(operation))
    clients = offline_wire(respond)
    result = await invoke(operation)
    assert result.model_dump() == (TasteProfileAnalysisOutput.model_validate(output_for(operation)).model_dump()
                                   if operation == 'taste' else course_drafting.CourseDraft.model_validate(output_for(operation)).model_dump())
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
    assert not {'temperature', 'top_p', 'top_k', 'candidate_count', 'generation_config', 'thinking_budget',
                'parallel_tool_calls', 'tools', 'stream_options', 'metadata', 'store'} & body.keys()
    allowed = {'model', 'messages', 'stream', 'response_format', 'max_completion_tokens', 'reasoning_effort'}
    assert set(body) <= allowed
    if operation == 'candidates':
        assert body['reasoning_effort'] == 'low'
        assert body['max_completion_tokens'] == 5000
    else:
        assert 'reasoning_effort' not in body
        assert 'max_completion_tokens' not in body
    assert get_abto().get_headers() == {}
    assert len(clients) == 2 and all(client.is_closed for client in clients)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['taste', 'candidates'])
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
    clients = offline_wire(lambda _: completion({'title': '후보', 'reason': '', 'days': []}))
    with pytest.raises(OutputParserException, match='assigned day'):
        await invoke('candidates')
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
@pytest.mark.parametrize('key', ['GEMINI_API_KEY', 'ABTO_CALLING_KEY'])
@pytest.mark.parametrize('operation', ['taste', 'candidates'])
async def test_missing_credentials_create_no_model(operation, key, mocker):
    mocker.patch.object(settings, key, '')
    module = taste_profile_chains if operation == 'taste' else course_drafting
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

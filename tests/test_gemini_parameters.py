"""Check Gemini generation parameters on serialized, offline HTTP requests."""

import asyncio
import json
import warnings

import httpx
import pytest
from google import genai
from google.genai import errors, types

from app.agent import course_graph, taste_profile_chains
from app.schemas.course import CourseRequestSchema
from app.schemas.taste_profile import TasteProfileAnalysisOutput


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['taste', 'candidates'])
async def test_gemini_requests_omit_deprecated_generation_parameters(
    operation, mocker,
):
    """Exercise real LangChain/GenAI serialization without sending network traffic."""
    if operation == 'taste':
        output = {
            name: dict.fromkeys(TasteProfileAnalysisOutput.model_fields[name].annotation.model_fields, 3)
            for name in ('travelPurpose', 'preferredLocationType', 'activityPreference', 'foodPreference')
        }
        output.update(
            travelPaceDensity='balanced', spendingTendency='moderate',
            companionType='solo', seasonalEnvironmentPreference=['warm_region'],
        )
    else:
        output = {'title': '서울 여행', 'reason': '문화 탐방', 'days': [
            {'candidates': [{'name': '국립중앙박물관'}, {'name': '서울숲'}]},
        ]}

    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            'candidates': [{
                'content': {'role': 'model', 'parts': [{'text': json.dumps(output)}]},
                'finishReason': 'STOP',
            }],
        })

    transport = httpx.MockTransport(respond)
    close_transport = mocker.spy(transport, 'aclose')
    provider = genai.Client(
        api_key='offline-gemini-key', vertexai=False,
        http_options=types.HttpOptions(async_client_args={'transport': transport}),
    )
    close_async = mocker.spy(provider.aio, 'aclose')
    close_sync = mocker.spy(provider, 'close')
    mocker.patch('langchain_google_genai.chat_models.Client', return_value=provider)
    mocker.patch.object(course_graph.settings, 'GEMINI_MODEL_NAME', 'gemini-3.8-flash')
    mocker.patch.object(course_graph.settings, 'GEMINI_API_KEY', 'offline-gemini-key')
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            if operation == 'taste':
                result = await taste_profile_chains.generate_taste_profile('{}')
                assert result.seasonalEnvironmentPreference == ['warm_region']
            else:
                request = CourseRequestSchema.model_validate({
                    'userId': '550e8400-e29b-41d4-a716-446655440000', 'mbti': 'INTJ',
                    'tripCondition': {
                        'destinationCountry': '대한민국', 'destinationCity': '서울',
                        'startDate': '2026-10-17', 'totalDays': 1, 'budgetType': 'moderate',
                    },
                })
                result = await course_graph._draft_day_candidates(request, [], day_index=0, seed='offline')
                assert len(result.days) == 1
            close_async.assert_awaited_once()
            # LangChain's destructor also closes the sync client. The
            # awaited async close and closed transport establish cleanup.
            close_sync.assert_called()
            close_transport.assert_awaited()
    finally:
        await provider.aio.aclose()
        provider.close()

    assert len(requests) == 1
    config = requests[0]['generationConfig']
    config_keys = {key.replace('_', '').lower() for key in config}
    assert not {'temperature', 'topp', 'topk', 'candidatecount'} & config_keys
    thinking = config.get('thinkingConfig', {})
    thinking = {key.replace('_', '').lower(): value for key, value in thinking.items()}
    assert 'thinkingbudget' not in thinking
    assert not any('sampling' in str(warning.message) for warning in caught)
    assert config['responseMimeType'] == 'application/json'
    assert config.get('responseJsonSchema') or config.get('responseSchema')
    if operation == 'taste':
        assert 'thinkinglevel' not in thinking
    else:
        assert thinking['thinkinglevel'] == 'LOW'
        assert config['maxOutputTokens'] == 5000


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['failure', 'cancellation'])
async def test_taste_client_has_one_attempt_and_closes_on_failure_or_cancellation(mocker, outcome):
    entered = asyncio.Event()
    requests = []

    async def respond(request):
        requests.append(request)
        entered.set()
        if outcome == 'cancellation':
            await asyncio.Event().wait()
        return httpx.Response(503, json={'error': {'code': 503, 'message': 'offline provider failure'}})

    transport = httpx.MockTransport(respond)
    close_transport = mocker.spy(transport, 'aclose')
    provider = genai.Client(
        api_key='offline-key', vertexai=False,
        http_options=types.HttpOptions(async_client_args={'transport': transport}),
    )
    close_provider = mocker.spy(provider, 'close')
    close_async_provider = mocker.spy(provider.aio, 'aclose')
    try:
        mocker.patch('langchain_google_genai.chat_models.Client', return_value=provider)
        mocker.patch.object(course_graph.settings, 'GEMINI_API_KEY', 'offline-key')
        mocker.patch.object(course_graph.settings, 'TASTE_ANALYSIS_TIMEOUT_SECONDS', 60.)
        task = asyncio.create_task(taste_profile_chains.generate_taste_profile('{}'))
        await asyncio.wait_for(entered.wait(), timeout=2)
        if outcome == 'cancellation':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(errors.ServerError):
                await asyncio.wait_for(task, timeout=2)
        assert len(requests) == 1
        assert requests[0].extensions['timeout']['read'] == 60.
        close_async_provider.assert_awaited_once()
        close_transport.assert_awaited()
        close_provider.assert_called_once()
    finally:
        await provider.aio.aclose()
        provider.close()


@pytest.mark.asyncio
async def test_taste_model_is_not_created_without_credentials(mocker):
    mocker.patch.object(course_graph.settings, 'GEMINI_API_KEY', '')
    constructor = mocker.patch.object(taste_profile_chains, 'ChatGoogleGenerativeAI')
    with pytest.raises(ValueError, match='credentials'):
        await taste_profile_chains.generate_taste_profile('{}')
    constructor.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['provider_failure', 'invalid_days', 'cancellation'])
async def test_daily_course_client_closes_on_failure_and_cancellation(mocker, outcome):
    """Execute the SDK transport, retaining errors and closing both client faces."""
    from langchain_core.exceptions import OutputParserException

    entered = asyncio.Event()
    requests = []
    async def respond(request):
        requests.append(request)
        entered.set()
        if outcome == 'cancellation':
            await asyncio.Event().wait()
        if outcome == 'provider_failure':
            return httpx.Response(503, json={'error': {'code': 503, 'message': 'offline failure'}})
        return httpx.Response(200, json={
            'candidates': [{'content': {'role': 'model', 'parts': [{'text': json.dumps({'title': '후보', 'reason': '', 'days': []})}]}, 'finishReason': 'STOP'}],
        })
    transport = httpx.MockTransport(respond)
    provider = genai.Client(api_key='offline-key', vertexai=False, http_options=types.HttpOptions(async_client_args={'transport': transport}))
    close_async = mocker.spy(provider.aio, 'aclose')
    close_sync = mocker.spy(provider, 'close')
    close_transport = mocker.spy(transport, 'aclose')
    request = CourseRequestSchema.model_validate({
        'userId': '550e8400-e29b-41d4-a716-446655440000', 'mbti': 'INTJ',
        'tripCondition': {'destinationCountry': '일본', 'destinationCity': '도쿄', 'startDate': '2026-10-17', 'totalDays': 1, 'budgetType': 'moderate'},
    })
    try:
        mocker.patch('langchain_google_genai.chat_models.Client', return_value=provider)
        mocker.patch.object(course_graph.settings, 'GEMINI_API_KEY', 'offline-key')
        task = asyncio.create_task(course_graph._draft_day_candidates(request, [], day_index=0, seed='offline'))
        await asyncio.wait_for(entered.wait(), 2)
        if outcome == 'cancellation':
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            expected = errors.ServerError if outcome == 'provider_failure' else OutputParserException
            with pytest.raises(expected):
                await asyncio.wait_for(task, 2)
        assert len(requests) == 1
        assert requests[0].extensions['timeout']['read'] == 60.
        close_async.assert_awaited_once()
        close_sync.assert_called_once()
        close_transport.assert_awaited()
    finally:
        await provider.aio.aclose()
        provider.close()

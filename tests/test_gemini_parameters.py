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
@pytest.mark.parametrize('operation', ['taste', 'candidates', 'place_copy'])
async def test_gemini_requests_omit_deprecated_generation_parameters(
    operation, mocker, offline_place_copy,
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
    elif operation == 'candidates':
        output = {'title': '서울 여행', 'reason': '문화 탐방', 'days': [
            {'candidates': [{'name': '국립중앙박물관'}, {'name': '서울숲'}]},
        ]}
    else:
        output = {'stops': []}

    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            'candidates': [{
                'content': {'role': 'model', 'parts': [{'text': json.dumps(output)}]},
                'finishReason': 'STOP',
            }],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        provider = genai.Client(
            api_key='offline-gemini-key', vertexai=False,
            http_options=types.HttpOptions(httpx_async_client=http_client),
        )
        mocker.patch('langchain_google_genai.chat_models.Client', return_value=provider)
        mocker.patch.object(course_graph.settings, 'GEMINI_MODEL_NAME', 'gemini-3.8-flash')
        mocker.patch.object(course_graph.settings, 'GEMINI_API_KEY', 'offline-gemini-key')
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                if operation == 'taste':
                    result = await taste_profile_chains.generate_taste_profile('{}')
                    assert result.seasonalEnvironmentPreference == ['warm_region']
                elif operation == 'candidates':
                    request = CourseRequestSchema.model_validate({
                        'userId': '550e8400-e29b-41d4-a716-446655440000', 'mbti': 'INTJ',
                        'tripCondition': {
                            'destinationCountry': '대한민국', 'destinationCity': '서울',
                            'startDate': '2026-10-17', 'totalDays': 1, 'budgetType': 'moderate',
                        },
                    })
                    result = await course_graph._draft_day_candidates(request, [], day_index=0, seed='offline')
                    assert len(result.days) == 1
                else:
                    assert await offline_place_copy.original({'stops': [], 'preferences': []}) == output
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
        assert config['maxOutputTokens'] == (5000 if operation == 'candidates' else 12000)


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

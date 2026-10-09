import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from app.main import app

# 테스트용 API Key
TEST_API_KEY = "test_internal_secret_key"

@pytest.fixture
def mock_env(mocker):
    # 환경 변수 및 설정을 모킹하여 인증 키를 설정
    mocker.patch("app.core.config.settings.INTERNAL_API_KEY", TEST_API_KEY)
    mocker.patch("app.core.config.settings.GEMINI_API_KEY", "offline-test-key")

@pytest.fixture
def valid_request_payload():
    return {
        "userId": "550e8400-e29b-41d4-a716-446655440000",
        "items": [
            {
                "sourceImageId": "img_001",
                "location": {
                    "country": "South Korea",
                    "city": "Seoul",
                    "region": "Mapo-gu",
                    "district": "Yeonnam-dong",
                    "placeName": "Gyeongui Line Forest Park",
                    "placeTypes": ["park", "landmark", "tourist_attraction"]
                },
                "timeContext": {
                    "capturedAt": "2026-07-18T15:30:00+09:00",
                    "dayOfWeek": "sat",
                    "isWeekend": True,
                    "timeBucket": "afternoon",
                    "season": "summer"
                }
            },
            {
                "sourceImageId": "img_002",
                "location": {
                    "country": "South Korea",
                    "city": "Gangneung",
                    "region": "Anmok Beach",
                    "district": "Gyeonso-dong",
                    "placeName": "Anmok Coffee Street",
                    "placeTypes": ["cafe", "beach", "food"]
                },
                "timeContext": {
                    "capturedAt": "2026-07-19T10:00:00+09:00",
                    "dayOfWeek": "sun",
                    "isWeekend": True,
                    "timeBucket": "morning",
                    "season": "summer"
                }
            }
        ]
    }

@pytest.mark.asyncio
async def test_behavior_analysis_success(mocker, mock_env, valid_request_payload):
    from tests.test_behavior_evidence import full_profile

    chain = AsyncMock()
    chain.return_value = full_profile()
    mocker.patch("app.services.behavior_service.generate_taste_profile", chain)

    # 2. httpx AsyncClient를 이용해 비동기 API 엔드포인트 호출 (SSE 통신)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-Internal-Api-Key": TEST_API_KEY}
        response = await client.post(
            "/internal/ai/taste-profile/analysis",
            json=valid_request_payload,
            headers=headers
        )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

        # SSE 스트림 파싱 및 검증
        events = []
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event_type = line.split("event:")[1].strip()
                events.append({"event": event_type})
            elif line.startswith("data:"):
                data_content = json.loads(line.split("data:")[1].strip())
                events[-1]["data"] = data_content

        # 최소 2개 이벤트 발생 확인 (progress -> complete)
        assert len(events) >= 2
        assert events[0]["event"] == "progress"
        assert events[0]["data"]["step"] == "ANALYZING_PREFERENCE"
        
        assert events[-1]["event"] == "complete"
        complete_data = events[-1]["data"]
        assert "tasteProfile" in complete_data
        
        profile = complete_data["tasteProfile"]
        assert profile["travelPurpose"]["relaxation"] == 3
        assert profile["travelPaceDensity"] == "balanced"
        assert profile["spendingTendency"] == "moderate"
        assert profile["companionType"] == "solo"  # Compatibility default, not observed companion.
        assert "warm_region" not in profile["seasonalEnvironmentPreference"]
        assert set(complete_data) == {"tasteProfile"}
        chain.assert_awaited_once()
        assert profile["seasonalEnvironmentPreference"] == ["summer_resort"]

@pytest.mark.asyncio
async def test_behavior_analysis_invalid_format(mock_env):
    # 잘못된 UUID 형식 테스트
    invalid_payload = {
        "userId": "invalid-uuid-format",
        "items": []
    }
    
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-Internal-Api-Key": TEST_API_KEY}
        response = await client.post(
            "/internal/ai/taste-profile/analysis",
            json=invalid_payload,
            headers=headers
        )
        assert response.status_code == 400

@pytest.mark.asyncio
async def test_behavior_analysis_unauthorized(valid_request_payload):
    # 잘못된 API Key로 인한 인증 실패 테스트
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-Internal-Api-Key": "wrong_key_123"}
        response = await client.post(
            "/internal/ai/taste-profile/analysis",
            json=valid_request_payload,
            headers=headers
        )
        assert response.status_code == 401
        assert response.json()["message"] == "내부 인증 실패"


@pytest.mark.asyncio
@pytest.mark.parametrize('key', ['', '   '])
@pytest.mark.parametrize('endpoint', ['analysis', 'behavior'])
async def test_missing_gemini_key_rejected_before_streaming(mocker, mock_env, valid_request_payload, key, endpoint):
    mocker.patch('app.core.config.settings.GEMINI_API_KEY', key)
    generate = mocker.patch('app.services.behavior_service.generate_taste_profile', new_callable=AsyncMock)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(f'/internal/ai/taste-profile/{endpoint}', json=valid_request_payload,
                                     headers={'X-Internal-Api-Key': TEST_API_KEY})
    assert response.status_code == 500
    assert response.headers['content-type'] == 'application/json'
    assert response.json() == {'status': 500, 'message': 'AI 취향 분석 설정을 확인할 수 없습니다.', 'data': None}
    generate.assert_not_awaited()


@pytest.mark.asyncio
async def test_analysis_deadline_cancels_inference_and_emits_only_error(mocker, mock_env, valid_request_payload):
    from tests.test_behavior_evidence import sse_events

    cancelled = asyncio.Event()

    async def blocked_analysis(_):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    generate = mocker.patch('app.services.behavior_service.generate_taste_profile', side_effect=blocked_analysis)
    mocker.patch('app.core.config.settings.TASTE_ANALYSIS_TIMEOUT_SECONDS', 0.02)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await asyncio.wait_for(client.post('/internal/ai/taste-profile/analysis',
            json=valid_request_payload, headers={'X-Internal-Api-Key': TEST_API_KEY}), timeout=2)
    events = sse_events(response)
    assert response.status_code == 200
    assert [kind for kind, _ in events] == ['progress', 'error']
    assert events[-1][1] == {'status': 500, 'message': '취향 분석 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요.'}
    assert cancelled.is_set()
    generate.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [RuntimeError('private-provider-detail'),
                                  HTTPException(status_code=500, detail='private-provider-detail')])
async def test_stream_boundary_hides_unexpected_error_details(mocker, mock_env, valid_request_payload, error):
    from tests.test_behavior_evidence import sse_events

    async def broken_stream(_):
        yield {'event': 'progress', 'data': {'step': 'ANALYZING_PREFERENCE'}}
        raise error

    mocker.patch('app.api.taste_profile.analyze_behavior_stream', broken_stream)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/internal/ai/taste-profile/analysis', json=valid_request_payload,
                                     headers={'X-Internal-Api-Key': TEST_API_KEY})
    assert [kind for kind, _ in sse_events(response)] == ['progress', 'error']
    assert 'private-provider-detail' not in response.text


@pytest.mark.asyncio
async def test_analysis_cancellation_is_not_converted_to_error(mocker, mock_env, valid_request_payload):
    from app.schemas.behavior import BehaviorAnalysisRequest
    from app.services.behavior_service import analyze_behavior_stream

    entered = asyncio.Event()
    cleaned_up = asyncio.Event()

    async def blocked_analysis(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned_up.set()

    mocker.patch('app.services.behavior_service.generate_taste_profile', side_effect=blocked_analysis)
    stream = analyze_behavior_stream(BehaviorAnalysisRequest.model_validate(valid_request_payload))
    assert (await anext(stream))['event'] == 'progress'
    task = asyncio.create_task(anext(stream))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned_up.is_set()
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.asyncio
@pytest.mark.parametrize('timeout', [0., -1., float('nan'), float('inf')])
async def test_invalid_deadline_rejected_before_streaming(mocker, mock_env, valid_request_payload, timeout):
    mocker.patch('app.core.config.settings.TASTE_ANALYSIS_TIMEOUT_SECONDS', timeout)
    generate = mocker.patch('app.services.behavior_service.generate_taste_profile', new_callable=AsyncMock)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/internal/ai/taste-profile/analysis', json=valid_request_payload,
                                     headers={'X-Internal-Api-Key': TEST_API_KEY})
    assert response.status_code == 500
    assert response.headers['content-type'] == 'application/json'
    generate.assert_not_awaited()

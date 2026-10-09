import json
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

# 테스트용 API Key
TEST_API_KEY = "test_internal_secret_key"

@pytest.fixture
def mock_env(mocker):
    # 환경 변수 및 설정을 모킹하여 인증 키를 설정
    mocker.patch("app.core.config.settings.INTERNAL_API_KEY", TEST_API_KEY)

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
    chain.ainvoke.return_value = full_profile()
    mocker.patch("app.services.behavior_service.taste_profile_chain", chain)

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
        chain.ainvoke.assert_awaited_once()
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

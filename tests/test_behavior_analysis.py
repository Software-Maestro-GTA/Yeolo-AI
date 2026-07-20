"""
@file test_behavior_analysis.py
@description 위치/시간 전처리 메타데이터 기반 성향 분석 API(API-BA-6)에 대한 인수 및 예외 처리 검증 테스트 모듈
@requirements REQ-11
@functional FUN-1
@api API-BA-6
@author Antigravity Agent
"""

import json
import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, MagicMock
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
    # 1. LangChain LLM 체인 Mocking 설정
    # 1단계 요약 체인(Fact Sheet 요약본)의 결과값 모킹
    mock_fact_sheet = MagicMock()
    mock_fact_sheet.content = "이 여행자는 주말을 이용해 대도시의 공원이나 강릉의 해변 카페 거리 같은 자연 친화적이고 여유로운 장소를 즐겨 찾습니다."
    
    # 2단계 병렬 체인 Structured Output 결과 모킹
    from app.schemas.taste_profile import (
        PurposePaceCompanionOutput,
        LocationEnvironmentOutput,
        ActivityFoodSpendingOutput,
    )
    
    mock_purpose_pace_output = PurposePaceCompanionOutput(
        relaxation=4,
        sightseeing=3,
        culturalExperience=2,
        gourmet=4,
        natureExploration=4,
        activity=2,
        shopping=1,
        festivalEvent=2,
        wellness=3,
        selfDevelopment=1,
        travelPaceDensity="balanced",
        companionType="friends"
    )
    
    mock_location_env_output = LocationEnvironmentOutput(
        bigCity=3,
        smallTownAlley=4,
        natureHinterland=4,
        beachResort=4,
        mountainPlateau=2,
        historicalCity=3,
        themeParkResort=1,
        famousSpotPreferred=3,
        hiddenSpotPreferred=4,
        warm_region=True,
        cold_region=False,
        summer_resort=True,
        winter_sports=False,
        spring_flower_autumn_foliage=False,
        dry_weather=False,
        off_season=True,
        peak_season=False
    )
    
    mock_activity_food_output = ActivityFoodSpendingOutput(
        viewing=3,
        experience=3,
        adventure=2,
        photographyVideo=4,
        gourmetExploration=4,
        nightlife=2,
        shopping=1,
        relaxation=4,
        localInteraction=3,
        spendingTendency="moderate",
        localFoodActive=4,
        famousRestaurantCentered=4,
        streetFood=3,
        cafeDessert=5,
        fineDining=2,
        familiarFoodPreferred=2,
        dietaryRestriction=1,
        sightseeingOverFood=2
    )

    # 1단계 LLM 인보크 모킹
    mock_llm_ainvoke = mocker.patch(
        "langchain_google_genai.ChatGoogleGenerativeAI.ainvoke",
        new_callable=AsyncMock
    )
    mock_llm_ainvoke.return_value = mock_fact_sheet

    # 2단계 Structured Output 체인 자체를 AsyncMock으로 패치하여 Pydantic delattr 이슈를 방지
    # 서비스 모듈 네임스페이스의 참조를 직접 패치하여 임포트 바인딩 문제를 방지합니다.
    mock_purpose_pace_chain = AsyncMock()
    mock_purpose_pace_chain.ainvoke.return_value = mock_purpose_pace_output
    mocker.patch("app.services.behavior_service.purpose_pace_companion_chain", mock_purpose_pace_chain)
    
    mock_location_env_chain = AsyncMock()
    mock_location_env_chain.ainvoke.return_value = mock_location_env_output
    mocker.patch("app.services.behavior_service.location_environment_chain", mock_location_env_chain)
    
    mock_activity_food_chain = AsyncMock()
    mock_activity_food_chain.ainvoke.return_value = mock_activity_food_output
    mocker.patch("app.services.behavior_service.activity_food_spending_chain", mock_activity_food_chain)

    # 2. httpx AsyncClient를 이용해 비동기 API 엔드포인트 호출 (SSE 통신)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-Internal-Api-Key": TEST_API_KEY}
        response = await client.post(
            "/internal/ai/taste-profile/behavior",
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
        assert profile["sourceType"] == "behavior"
        assert profile["travelPurpose"]["relaxation"] == 4
        assert profile["travelPaceDensity"] == "balanced"
        assert profile["spendingTendency"] == "moderate"
        assert profile["companionType"] == "friends"
        assert "warm_region" in profile["seasonalEnvironmentPreference"]

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
            "/internal/ai/taste-profile/behavior",
            json=invalid_payload,
            headers=headers
        )
        assert response.status_code == 400

@pytest.mark.asyncio
async def test_behavior_analysis_insufficient_data(mock_env):
    # 아이템 데이터 부족 테스트 (비어 있음)
    insufficient_payload = {
        "userId": "550e8400-e29b-41d4-a716-446655440000",
        "items": []
    }
    
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-Internal-Api-Key": TEST_API_KEY}
        response = await client.post(
            "/internal/ai/taste-profile/behavior",
            json=insufficient_payload,
            headers=headers
        )
        assert response.status_code == 400
        assert response.json()["message"] == "분석 가능한 전처리 메타데이터가 부족합니다."

@pytest.mark.asyncio
async def test_behavior_analysis_unauthorized(valid_request_payload):
    # 잘못된 API Key로 인한 인증 실패 테스트
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-Internal-Api-Key": "wrong_key_123"}
        response = await client.post(
            "/internal/ai/taste-profile/behavior",
            json=valid_request_payload,
            headers=headers
        )
        assert response.status_code == 401
        assert response.json()["message"] == "내부 인증 실패"

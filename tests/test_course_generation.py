"""
@file test_course_generation.py
@description 성향 프로필 및 여행 조건 기반 맞춤 코스 생성 API(API-BA-1) 및 파이프라인에 대한 단위/E2E 테스트 모듈
@requirements REQ-7
@functional FUN-2
@api API-BA-1
@author Antigravity Agent
"""

import pytest
from httpx import AsyncClient, ASGITransport
from fastapi import HTTPException
from app.main import app
from app.schemas.course import CourseSchema

TEST_API_KEY = "test_internal_secret_key"


@pytest.fixture
def mock_env(mocker):
    mocker.patch("app.core.config.settings.INTERNAL_API_KEY", TEST_API_KEY)


@pytest.fixture
def valid_course_request_payload():
    return {
        "userId": "550e8400-e29b-41d4-a716-446655440000",
        "tasteProfile": {
            "tasteProfileId": "550e8400-e29b-41d4-a716-446655440001",
            "sourceType": "mixed",
            "travelPurpose": {
                "relaxation": 4,
                "sightseeing": 3,
                "culturalExperience": 3,
                "gourmet": 5,
                "natureExploration": 4,
                "activity": 2,
                "shopping": 2,
                "festivalEvent": 1,
                "wellness": 3,
                "selfDevelopment": 1,
            },
            "travelPaceDensity": "balanced",
            "preferredLocationType": {
                "bigCity": 3,
                "smallTownAlley": 4,
                "natureHinterland": 4,
                "beachResort": 5,
                "mountainPlateau": 2,
                "historicalCity": 3,
                "themeParkResort": 1,
                "famousSpotPreferred": 3,
                "hiddenSpotPreferred": 5,
            },
            "activityPreference": {
                "viewing": 3,
                "experience": 4,
                "adventure": 2,
                "photographyVideo": 5,
                "gourmetExploration": 5,
                "nightlife": 2,
                "shopping": 2,
                "relaxation": 4,
                "localInteraction": 3,
            },
            "spendingTendency": "cost_effective",
            "companionType": "friends",
            "foodPreference": {
                "localFoodActive": 5,
                "famousRestaurantCentered": 4,
                "streetFood": 4,
                "cafeDessert": 5,
                "fineDining": 2,
                "familiarFoodPreferred": 2,
                "dietaryRestriction": 1,
                "sightseeingOverFood": 2,
            },
            "seasonalEnvironmentPreference": [
                "warm_region",
                "spring_flower_autumn_foliage",
                "off_season",
            ],
        },
        "tripCondition": {
            "destinationCountry": "South Korea",
            "destinationCity": "Jeju",
            "startDate": "2026-08-01",
            "totalDays": 2,
            "budgetType": "cost_effective",
        },
    }


@pytest.fixture
def sample_course_schema():
    return CourseSchema(
        title="제주 가성비 힐링 & 미식 여행 2일",
        destinationCountry="South Korea",
        destinationCity="Jeju",
        startDate="2026-08-01",
        totalDays=2,
        tags=["미식", "가성비", "힐링"],
        recommendationReason="친구와 함께 즐기는 가성비 높은 제주 미식 코스입니다.",
        itinerary={
            "days": [
                {
                    "day": 1,
                    "date": "2026-08-01",
                    "memo": "1일차: 도착 및 미식 탐방",
                    "stops": [
                        {
                            "sequence": 1,
                            "placeId": "place-101",
                            "placeName": "제주 동문시장",
                            "category": "시장/길거리음식",
                            "arrivalTime": "11:00",
                            "stayMinutes": 90,
                            "memo": "현지 야시장 음식 체험",
                            "transportToNext": "driving",
                            "travelMinutesToNext": 20,
                            "cost": 15000,
                            "reason": "길거리 음식 및 현지 시장 적극 반영",
                        }
                    ],
                }
            ]
        },
    )



@pytest.mark.asyncio
async def test_generate_course_success(mock_env, valid_course_request_payload, sample_course_schema, mocker):
    """
    정상적인 성향 프로필 및 여행 조건 요청 시 SSE 스트리밍 (progress, complete) 응답 검증
    """
    mocker.patch(
        "app.services.course_service.run_course_generation_chain",
        return_value=sample_course_schema,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        response = await ac.post(
            "/internal/ai/courses",
            headers={"X-Internal-Api-Key": TEST_API_KEY},
            json=valid_course_request_payload,
        )

    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    content = response.text
    assert "event: progress" in content
    assert "event: complete" in content
    assert "GENERATING_ROUTE" in content
    assert "제주 가성비 힐링 & 미식 여행 2일" in content


@pytest.mark.asyncio
async def test_generate_course_unauthorized(mock_env, valid_course_request_payload):
    """
    내부 인증 API Key 헤더 누락 또는 잘못된 키 입력 시 401 에러 반환 검증
    """
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        # 헤더 없음
        response = await ac.post(
            "/internal/ai/courses", json=valid_course_request_payload
        )
        assert response.status_code == 401
        assert response.json()["message"] == "내부 인증 실패"

        # 잘못된 헤더
        response_invalid = await ac.post(
            "/internal/ai/courses",
            headers={"X-Internal-Api-Key": "wrong_key"},
            json=valid_course_request_payload,
        )
        assert response_invalid.status_code == 401


@pytest.mark.asyncio
async def test_generate_course_bad_request(mock_env):
    """
    필수 데이터 누락 또는 스키마 미충족 시 400 Bad Request 반환 검증
    """
    invalid_payload = {
        "userId": "invalid-uuid-or-missing-fields",
        # tasteProfile 누락
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        response = await ac.post(
            "/internal/ai/courses",
            headers={"X-Internal-Api-Key": TEST_API_KEY},
            json=invalid_payload,
        )

    assert response.status_code == 400
    assert response.json()["status"] == 400
    assert response.json()["message"] == "코스 생성 조건이 올바르지 않습니다."


@pytest.mark.asyncio
async def test_generate_course_not_found(mock_env, valid_course_request_payload, mocker):
    """
    조건에 맞는 장소 데이터가 부족하여 404 예외 발생 케이스 검증
    """
    mocker.patch(
        "app.services.course_service.run_course_generation_chain",
        return_value=None,
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        response = await ac.post(
            "/internal/ai/courses",
            headers={"X-Internal-Api-Key": TEST_API_KEY},
            json=valid_course_request_payload,
        )

    assert response.status_code == 404
    assert response.json()["status"] == 404
    assert "조건에 맞는 장소" in response.json()["message"]


@pytest.mark.asyncio
async def test_generate_course_ai_error(mock_env, valid_course_request_payload, mocker):
    """
    AI 모델 호출 중 서버 예외 발생 시 500 Internal Error 반환 검증
    """
    mocker.patch(
        "app.services.course_service.run_course_generation_chain",
        side_effect=HTTPException(status_code=500, detail="AI 코스 생성 중 오류가 발생했습니다."),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        response = await ac.post(
            "/internal/ai/courses",
            headers={"X-Internal-Api-Key": TEST_API_KEY},
            json=valid_course_request_payload,
        )

    assert response.status_code == 500
    assert response.json()["status"] == 500
    assert "AI 코스 생성 중 오류" in response.json()["message"]

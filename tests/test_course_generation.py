import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.agent.prompts import COURSE_GENERATION_PROMPT
from app.main import app
from app.schemas.course import (
    CourseSchema,
    PlaceSchema,
    StopSchema,
    TransportToNextSchema,
)

TEST_API_KEY = "test_internal_secret_key"


@pytest.fixture
def mock_env(mocker):
    mocker.patch("app.core.config.settings.INTERNAL_API_KEY", TEST_API_KEY)
    mocker.patch(
        "app.services.course_service.enrich_course_with_google_maps",
        side_effect=lambda c: c,
    )


@pytest.fixture
def valid_taste_profile():
    return {
        "tasteProfileId": "550e8400-e29b-41d4-a716-446655440001",
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
    }


@pytest.fixture
def valid_trip_condition():
    return {
        "destinationCountry": "대한민국",
        "destinationCity": "제주",
        "startDate": "2026-08-01",
        "totalDays": 2,
        "budgetType": "moderate",
    }


@pytest.fixture
def valid_course_request_payload(valid_taste_profile, valid_trip_condition):
    return {
        "userId": "550e8400-e29b-41d4-a716-446655440000",
        "mbti": "ENFP",
        "tasteProfile": valid_taste_profile,
        "tripCondition": valid_trip_condition,
    }


@pytest.fixture
def sample_course_schema():
    return CourseSchema(
        title="제주 가성비 힐링 & 미식 여행 2일",
        destinationCountry="대한민국",
        destinationCity="제주",
        coverImageUrl="https://images.unsplash.com/photo-1508009603885-50cf7c579365",
        startDate="2026-08-01",
        totalDays=2,
        tags=["#제주미식", "#가성비여행", "#힐링", "#친구와함께"],
        recommendationReason="친구와 함께 즐기는 가성비 높은 제주 미식과 힐링 명소 코스입니다.",
        itinerary={
            "days": [
                {
                    "day": 1,
                    "date": "2026-08-01",
                    "memo": "1일차: 동문시장 야시장 먹거리 탐방 후 해안 산책로로 이어지는 미식 힐링 코스",
                    "stops": [
                        {
                            "sequence": 1,
                            "arrivalTime": "11:00",
                            "stayMinutes": 90,
                            "memo": "제주 대표 전통시장으로 오메기떡, 흑돼지말이 등 다양한 로컬 먹거리를 체험할 수 있습니다. 야시장 구역은 오후부터 붐비므로 식사 시간대를 잘 조율하세요.",
                            "reason": "풍성한 길거리 음식과 활기찬 시장 분위기를 경험할 수 있는 대표 미식 명소",
                            "cost": 15000,
                            "place": {
                                "placeId": "places/ChIJN1t_tDeuEmsRUsoyG83frY4",
                                "placeName": "제주 동문시장",
                                "placeEngName": "Jeju Dongmun Traditional Market",
                                "category": "전통시장",
                                "address": "대한민국 제주특별자치도 제주시 관덕로14길 20",
                                "latitude": 33.5126,
                                "longitude": 126.5283,
                                "rating": 4.5,
                                "photoUrl": "https://places.googleapis.com/v1/places/ChIJN1t_tDeuEmsRUsoyG83frY4/photos/photo1",
                                "openingHours": [
                                    "월요일: 오전 8:00 ~ 오후 9:00",
                                    "화요일: 오전 8:00 ~ 오후 9:00",
                                    "수요일: 오전 8:00 ~ 오후 9:00",
                                    "목요일: 오전 8:00 ~ 오후 9:00",
                                    "금요일: 오전 8:00 ~ 오후 9:00",
                                    "토요일: 오전 8:00 ~ 오후 9:00",
                                    "일요일: 오전 8:00 ~ 오후 9:00",
                                ],
                            },
                            "transportToNext": {
                                "type": "transit",
                                "distance": 3200.0,
                                "minutes": 15,
                                "cost": 1400,
                                "memo": "동문시장 정류장에서 312번 버스를 탑승하여 용두암 입구 정류장에서 하차 후 도보 4분 이동합니다.",
                            },
                        },
                        {
                            "sequence": 2,
                            "arrivalTime": "13:00",
                            "stayMinutes": 60,
                            "memo": "용의 머리를 닮은 독특한 화산암 바위와 탁 트인 바다 전망을 감상할 수 있는 무료 관광 명소입니다. 해안 바람이 강할 수 있으니 겉옷을 챙기세요.",
                            "reason": "제주 북부 해안의 대표적인 자연 지형을 감상할 수 있는 힐링 명소",
                            "cost": 0,
                            "place": {
                                "placeId": "places/ChIJy30XyWuuEmsRw_Zq5JbA87w",
                                "placeName": "용두암",
                                "placeEngName": "Yongduam Rock",
                                "category": "자연명소",
                                "address": "대한민국 제주특별자치도 제주시 용두암길 15",
                                "latitude": 33.5163,
                                "longitude": 126.5125,
                                "rating": 4.3,
                                "photoUrl": "https://places.googleapis.com/v1/places/ChIJy30XyWuuEmsRw_Zq5JbA87w/photos/photo2",
                                "openingHours": [],
                            },
                            "transportToNext": {
                                "type": "none",
                                "distance": 0.0,
                                "minutes": 0,
                                "cost": 0,
                                "memo": "오늘의 일정을 마무리하고 숙소로 이동하거나 자유 일정을 즐깁니다.",
                            },
                        },
                    ],
                }
            ]
        },
    )


@pytest.mark.asyncio
async def test_generate_course_success(mock_env, valid_course_request_payload, sample_course_schema, mocker):
    """
    정상적인 성향 프로필, MBTI 및 여행 조건 요청 시 API-AI-2 SSE 스트리밍 (progress, complete) 응답 검증
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
async def test_generate_course_with_mbti_only(mock_env, valid_trip_condition, sample_course_schema, mocker):
    """
    mbti만 전달되고 tasteProfile은 null/누락된 요청 시 200 OK 정상 처리 검증 (API-AI-2 준수)
    """
    mocker.patch(
        "app.services.course_service.run_course_generation_chain",
        return_value=sample_course_schema,
    )

    payload = {
        "userId": "550e8400-e29b-41d4-a716-446655440000",
        "mbti": "INTJ",
        "tasteProfile": None,
        "tripCondition": valid_trip_condition,
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        response = await ac.post(
            "/internal/ai/courses",
            headers={"X-Internal-Api-Key": TEST_API_KEY},
            json=payload,
        )

    assert response.status_code == 200
    assert "event: complete" in response.text


@pytest.mark.asyncio
async def test_generate_course_with_taste_profile_only(mock_env, valid_taste_profile, valid_trip_condition, sample_course_schema, mocker):
    """
    tasteProfile만 전달되고 mbti는 null/누락된 요청 시 200 OK 정상 처리 검증 (API-AI-2 준수)
    """
    mocker.patch(
        "app.services.course_service.run_course_generation_chain",
        return_value=sample_course_schema,
    )

    payload = {
        "userId": "550e8400-e29b-41d4-a716-446655440000",
        "mbti": None,
        "tasteProfile": valid_taste_profile,
        "tripCondition": valid_trip_condition,
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        response = await ac.post(
            "/internal/ai/courses",
            headers={"X-Internal-Api-Key": TEST_API_KEY},
            json=payload,
        )

    assert response.status_code == 200
    assert "event: complete" in response.text


@pytest.mark.asyncio
async def test_generate_course_with_both_mbti_and_taste_profile(mock_env, valid_taste_profile, valid_trip_condition, sample_course_schema, mocker):
    """
    mbti와 tasteProfile이 둘 다 전달되는 요청 시 200 OK 정상 처리 검증
    """
    mocker.patch(
        "app.services.course_service.run_course_generation_chain",
        return_value=sample_course_schema,
    )

    payload = {
        "userId": "550e8400-e29b-41d4-a716-446655440000",
        "mbti": "INFJ",
        "tasteProfile": valid_taste_profile,
        "tripCondition": valid_trip_condition,
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        response = await ac.post(
            "/internal/ai/courses",
            headers={"X-Internal-Api-Key": TEST_API_KEY},
            json=payload,
        )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_generate_course_missing_both_mbti_and_taste_profile(mock_env, valid_trip_condition):
    """
    mbti와 tasteProfile이 둘 다 누락된 요청 시 API-AI-2 400 Bad Request 에러 반환 검증
    """
    payload = {
        "userId": "550e8400-e29b-41d4-a716-446655440000",
        "mbti": None,
        "tasteProfile": None,
        "tripCondition": valid_trip_condition,
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        response = await ac.post(
            "/internal/ai/courses",
            headers={"X-Internal-Api-Key": TEST_API_KEY},
            json=payload,
        )

    assert response.status_code == 400
    assert response.json()["status"] == 400
    assert response.json()["message"] == "코스 생성 조건이 올바르지 않습니다."


@pytest.mark.asyncio
async def test_generate_course_unauthorized(mock_env, valid_course_request_payload):
    """
    내부 인증 API Key 헤더 누락 또는 잘못된 키 입력 시 401 에러 반환 검증
    """
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        response = await ac.post(
            "/internal/ai/courses", json=valid_course_request_payload
        )
        assert response.status_code == 401
        assert response.json()["message"] == "내부 인증 실패"


@pytest.mark.asyncio
async def test_generate_course_bad_request(mock_env):
    """
    필수 데이터 누락 또는 스키마 미충족 시 400 Bad Request 반환 검증
    """
    invalid_payload = {
        "userId": "invalid-uuid-or-missing-fields",
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


def test_course_prompt_requirements():
    """
    COURSE_GENERATION_PROMPT에 MBTI, 아침/점심/저녁 식사, memo 내 장소 설명 및 주의사항, 그리고 스톱별 예상 비용(cost) 산정 지침이 포함되어 있는지 검증
    """
    prompt_str = str(COURSE_GENERATION_PROMPT)
    assert "MBTI" in prompt_str or "mbti" in prompt_str
    assert "아침" in prompt_str
    assert "점심" in prompt_str
    assert "저녁" in prompt_str
    assert "설명" in prompt_str
    assert "주의" in prompt_str or "유의" in prompt_str
    assert "비용" in prompt_str or "cost" in prompt_str


def test_stop_schema_cost_field_validation():
    """
    StopSchema에 cost(예상 비용) 필드가 올바르게 정의되고, 기본값(0) 및 양수 값 처리, 음수 거부 검증
    """
    place = PlaceSchema(
        placeId="places/test1234",
        placeName="테스트 장소",
        category="관광지",
        latitude=37.5,
        longitude=127.0,
    )
    transport = TransportToNextSchema(
        type="walking",
        minutes=10,
    )

    # 1. cost 기본값 검증 (0)
    stop_default = StopSchema(
        sequence=1,
        arrivalTime="10:00",
        stayMinutes=60,
        memo="테스트 메모",
        reason="테스트 추천 이유",
        place=place,
        transportToNext=transport,
    )
    assert stop_default.cost == 0

    # 2. 명시적 cost 지정 검증
    stop_custom_cost = StopSchema(
        sequence=2,
        arrivalTime="12:00",
        stayMinutes=90,
        memo="식사 메모",
        reason="맛집 추천",
        cost=25000,
        place=place,
        transportToNext=transport,
    )
    assert stop_custom_cost.cost == 25000
    stop_dict = stop_custom_cost.model_dump()
    assert stop_dict["cost"] == 25000

    # 3. 음수 cost 거부 검증
    with pytest.raises(ValidationError):
        StopSchema(
            sequence=3,
            arrivalTime="14:00",
            stayMinutes=30,
            memo="잘못된 비용 메모",
            reason="추천",
            cost=-5000,
            place=place,
            transportToNext=transport,
        )


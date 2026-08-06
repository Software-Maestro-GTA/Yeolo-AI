import asyncio
import json
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

# 1. 경로 문제 해결: 프로젝트 루트 경로를 sys.path에 등록하여 'app' 모듈 인식 가능하게 처리
root_dir = Path(__file__).resolve().parent.parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

from app.core.config import settings
from app.main import app
from app.schemas.course import CourseRequestSchema, TripConditionSchema
from app.schemas.taste_profile import TasteProfileSchema
from app.services.course_service import generate_course_service


# 샘플 데이터 정의
def get_sample_taste_profile() -> TasteProfileSchema:
    return TasteProfileSchema(
        travelPurpose={
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
        travelPaceDensity="balanced",
        preferredLocationType={
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
        activityPreference={
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
        spendingTendency="cost_effective",
        companionType="friends",
        foodPreference={
            "localFoodActive": 5,
            "famousRestaurantCentered": 4,
            "streetFood": 4,
            "cafeDessert": 5,
            "fineDining": 2,
            "familiarFoodPreferred": 2,
            "dietaryRestriction": 1,
            "sightseeingOverFood": 2,
        },
        seasonalEnvironmentPreference=[
            "warm_region",
            "spring_flower_autumn_foliage",
            "off_season",
        ],
    )


def get_sample_trip_condition() -> TripConditionSchema:
    return TripConditionSchema(
        destinationCountry="South Korea",
        destinationCity="Jeju",
        startDate="2026-08-01",
        totalDays=2,
        budgetType="moderate",
    )


# ----------------- A. Pytest E2E 테스트 케이스 -----------------
# 3가지 조건 (1. mbti만, 2. tasteProfile만, 3. mbti & tasteProfile 둘 다) 코스 생성 E2E 테스트
@pytest.mark.asyncio if "pytest" in sys.modules else lambda f: f
@pytest.mark.parametrize(
    "case_name, mbti_val, include_taste_profile",
    [
        ("1. MBTI 단독 요청", "ENFP", False),
        ("2. TasteProfile 단독 요청", None, True),
        ("3. MBTI & TasteProfile 동시 요청", "INFJ", True),
    ],
)
async def test_generate_course_e2e_combinations(case_name, mbti_val, include_taste_profile):
    assert settings.GEMINI_API_KEY, "E2E 테스트 실행을 위해 GEMINI_API_KEY가 등록되어야 합니다."

    taste_profile = get_sample_taste_profile() if include_taste_profile else None
    trip_condition = get_sample_trip_condition()

    payload = {
        "userId": "550e8400-e29b-41d4-a716-446655440000",
        "mbti": mbti_val,
        "tasteProfile": taste_profile.model_dump(mode="json") if taste_profile else None,
        "tripCondition": trip_condition.model_dump(mode="json"),
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-Internal-Api-Key": settings.INTERNAL_API_KEY}
        response = await client.post(
            "/internal/ai/courses",
            json=payload,
            headers=headers,
        )

        assert response.status_code == 200, f"[{case_name}] 실패: status={response.status_code}"
        assert "text/event-stream" in response.headers["content-type"]

        events = []
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event_type = line.split("event:")[1].strip()
                events.append({"event": event_type})
            elif line.startswith("data:"):
                data_content = json.loads(line.split("data:")[1].strip())
                events[-1]["data"] = data_content

        assert len(events) >= 2, f"[{case_name}] SSE 이벤트 부족"
        assert events[-1]["event"] == "complete", f"[{case_name}] complete 이벤트 누락"
        course = events[-1]["data"]["course"]
        assert "title" in course
        assert "itinerary" in course


# ----------------- B. 직접 스크립트 실행 제어 -----------------
# 실행 예시: uv run python tests/e2e/generate_course.py
async def run_local_test():
    print("==================================================")
    print("🤖 로컬 Gemini API 기반 맞춤 코스 생성 연동 테스트 시작")
    print(f"- 사용 모델: {settings.GEMINI_MODEL_NAME}")
    print(f"- API 키 설정 여부: {'설정 완료 (Yes)' if settings.GEMINI_API_KEY else '설정 누락 (No)'}")
    print("==================================================")

    if not settings.GEMINI_API_KEY:
        print("❌ 경고: GEMINI_API_KEY 설정이 누락되었습니다. .env를 확인해 주세요.")
        return

    cases = [
        ("1. MBTI 단독 요청 실험 (mbti='ENFP')", "ENFP", None),
        ("2. TasteProfile 단독 요청 실험", None, get_sample_taste_profile()),
        ("3. MBTI & TasteProfile 동시 요청 실험 (mbti='INFJ')", "INFJ", get_sample_taste_profile()),
    ]

    trip_condition = get_sample_trip_condition()

    for title, mbti_val, taste_profile in cases:
        print(f"\n🚀 [실험 케이스]: {title} 시작...\n")
        req_schema = CourseRequestSchema(
            userId="550e8400-e29b-41d4-a716-446655440000",
            mbti=mbti_val,
            tasteProfile=taste_profile,
            tripCondition=trip_condition,
        )

        try:
            sse_generator = await generate_course_service(req_schema)
            async for raw_chunk in sse_generator:
                print("📡 [SSE CHUNK RECEIVED]:")
                print(raw_chunk.strip())
                print("-" * 50)
            print(f"✅ [{title}] 성공 완료!")
        except Exception as e:  # noqa: BLE001
            print(f"❌ [{title}] 처리 도중 에러 발생: {e!s}")


if __name__ == "__main__":
    asyncio.run(run_local_test())

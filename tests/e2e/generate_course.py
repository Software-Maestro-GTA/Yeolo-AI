"""
@file generate_course.py
@description 실제 Gemini API를 호출하여 성향 프로필 및 여행 조건 기반 맞춤 코스 생성의 전체 흐름(API-BA-1)을 검증하는 E2E 테스트 및 실행 스크립트
@requirements REQ-7
@functional FUN-2
@api API-BA-1
@author Antigravity Agent
"""

import sys
import json
import asyncio
from pathlib import Path
import pytest
from httpx import AsyncClient, ASGITransport

# ruff: noqa: E402

# 1. 경로 문제 해결: 프로젝트 루트 경로를 sys.path에 등록하여 'app' 모듈 인식 가능하게 처리
root_dir = Path(__file__).resolve().parent.parent.parent
if str(root_dir) not in sys.path:
    sys.path.append(str(root_dir))

from app.main import app
from app.core.config import settings
from app.schemas.course import CourseRequestSchema, TripConditionSchema
from app.schemas.taste_profile import TasteProfileSchema
from app.services.course_service import generate_course_service


# 공통 테스트 페이로드 정의
def get_test_course_payload() -> CourseRequestSchema:
    return CourseRequestSchema(
        userId="550e8400-e29b-41d4-a716-446655440000",
        tasteProfile=TasteProfileSchema(
            sourceType="mixed",
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
        ),
        tripCondition=TripConditionSchema(
            destinationCountry="South Korea",
            destinationCity="Jeju",
            startDate="2026-08-01",
            totalDays=2,
            budgetType="cost_effective",
        ),
    )


# ----------------- A. Pytest E2E 테스트 케이스 -----------------
# 실행 예시: uv run pytest tests/e2e/generate_course.py
@pytest.mark.asyncio if "pytest" in sys.modules else lambda f: f
async def test_generate_course_e2e():
    assert settings.GEMINI_API_KEY, "E2E 테스트 실행을 위해 GEMINI_API_KEY가 등록되어야 합니다."
    request_payload = get_test_course_payload()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-Internal-Api-Key": settings.INTERNAL_API_KEY}
        response = await client.post(
            "/internal/ai/courses",
            json=request_payload.model_dump(mode="json"),
            headers=headers,
        )

        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]

        events = []
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event_type = line.split("event:")[1].strip()
                events.append({"event": event_type})
            elif line.startswith("data:"):
                data_content = json.loads(line.split("data:")[1].strip())
                if events:
                    events[-1]["data"] = data_content

        assert len(events) >= 2
        assert events[-1]["event"] == "complete"
        course = events[-1]["data"]["course"]

        # 생성된 코스 데이터 구조 및 값 검증
        assert "title" in course
        assert course["destinationCountry"] == "South Korea"
        assert course["destinationCity"] == "Jeju"
        assert course["totalDays"] == 2
        assert "itinerary" in course
        assert len(course["itinerary"]["days"]) == 2


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

    request_data = get_test_course_payload()
    print("\n🚀 맞춤 여행 코스 생성 비동기 파이프라인 스트림 시작...\n")

    try:
        sse_generator = await generate_course_service(request_data)
        async for raw_chunk in sse_generator:
            print("📡 [SSE CHUNK RECEIVED]:")
            print(raw_chunk.strip())
            print("-" * 50)
        print("\n✅ 맞춤 코스 생성 테스트가 성공적으로 완료되었습니다!")
    except Exception as e:
        print(f"\n❌ 분석 도중 에러가 발생했습니다: {str(e)}")


if __name__ == "__main__":
    asyncio.run(run_local_test())

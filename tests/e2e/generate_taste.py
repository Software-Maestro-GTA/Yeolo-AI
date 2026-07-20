"""
@file generate_taste.py
@description 실제 Gemini API를 호출하여 위치/시간 기반 성향 분석의 전체 흐름(API-BA-6)을 검증하는 E2E 테스트 및 실행 스크립트
@requirements REQ-11
@functional FUN-1
@api API-BA-6
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
from app.schemas.behavior import (
    BehaviorAnalysisRequest,
    BehaviorItemSchema,
    LocationSchema,
    TimeContextSchema
)
from app.services.behavior_service import analyze_behavior_stream

# 공통 테스트 페이로드 정의
def get_test_payload() -> BehaviorAnalysisRequest:
    return BehaviorAnalysisRequest(
        userId="550e8400-e29b-41d4-a716-446655440000",
        items=[
            BehaviorItemSchema(
                sourceImageId="img_test_01",
                location=LocationSchema(
                    country="South Korea",
                    city="Seoul",
                    region="Mapo-gu",
                    district="Yeonnam-dong",
                    placeName="Gyeongui Line Forest Park",
                    placeTypes=["park", "landmark", "tourist_attraction"]
                ),
                timeContext=TimeContextSchema(
                    capturedAt="2026-07-18T15:30:00+09:00",
                    dayOfWeek="sat",
                    isWeekend=True,
                    timeBucket="afternoon",
                    season="summer"
                )
            ),
            BehaviorItemSchema(
                sourceImageId="img_test_02",
                location=LocationSchema(
                    country="South Korea",
                    city="Gangneung",
                    region="Anmok Beach",
                    district="Gyeonso-dong",
                    placeName="Anmok Coffee Street",
                    placeTypes=["cafe", "beach", "food"]
                ),
                timeContext=TimeContextSchema(
                    capturedAt="2026-07-19T10:00:00+09:00",
                    dayOfWeek="sun",
                    isWeekend=True,
                    timeBucket="morning",
                    season="summer"
                )
            )
        ]
    )

# ----------------- A. Pytest E2E 테스트 케이스 -----------------
# 파일명이 generate_taste.py로 test_로 시작하지 않기 때문에 일반 pytest 구동 시 자동 실행되지 않습니다.
# 실행하려면 다음과 같이 파일 경로를 명시해야 합니다: uv run pytest tests/e2e/generate_taste.py
@pytest.mark.asyncio if 'pytest' in sys.modules else lambda f: f
async def test_generate_taste_e2e():
    assert settings.GEMINI_API_KEY, "E2E 테스트 실행을 위해 GEMINI_API_KEY가 등록되어야 합니다."
    request_payload = get_test_payload()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-Internal-Api-Key": settings.INTERNAL_API_KEY}
        response = await client.post(
            "/internal/ai/taste-profile/behavior",
            json=request_payload.model_dump(mode="json"),
            headers=headers
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
                events[-1]["data"] = data_content

        assert len(events) >= 2
        assert events[-1]["event"] == "complete"
        profile = events[-1]["data"]["tasteProfile"]
        assert profile["sourceType"] == "behavior"

# ----------------- B. 직접 스크립트 실행 제어 -----------------
# 실행 예시: uv run python tests/e2e/generate_taste.py
async def run_local_test():
    print("==================================================")
    print("🤖 로컬 Gemini API 연동 테스트 시작")
    print(f"- 사용 모델: {settings.GEMINI_MODEL_NAME}")
    print(f"- API 키 설정 여부: {'설정 완료 (Yes)' if settings.GEMINI_API_KEY else '설정 누락 (No)'}")
    print("==================================================")

    if not settings.GEMINI_API_KEY:
        print("❌ 경고: GEMINI_API_KEY 설정이 누락되었습니다. .env를 확인해 주세요.")
        return

    request_data = get_test_payload()
    print("\n🚀 2단계 하이브리드 파이프라인 분석 스트림 시작...\n")

    try:
        async for event in analyze_behavior_stream(request_data):
            print(f"📡 [EVENT]: {event['event']}")
            print(json.dumps(event["data"], indent=2, ensure_ascii=False))
            print("-" * 50)
        print("\n✅ 분석 테스트가 성공적으로 완료되었습니다!")
    except Exception as e:
        print(f"\n❌ 분석 도중 에러가 발생했습니다: {str(e)}")

if __name__ == "__main__":
    asyncio.run(run_local_test())

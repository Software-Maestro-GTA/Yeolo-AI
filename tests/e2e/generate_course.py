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
from app.schemas.course import CourseRequestSchema, CourseSchema, TripConditionSchema
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
        destinationCountry="태국",
        destinationCity="방콕",
        startDate="2026-08-01",
        totalDays=5,
        budgetType="moderate",
    )


def export_course_to_markdown(
    course_data: dict | CourseSchema,
    output_path: str | Path = "tests/e2e/generated_course.md",
) -> Path:
    """
    생성된 여행 코스 데이터 (CourseSchema)로부터 이모티콘이 포함되지 않은 깔끔한 Markdown 문서를 생성하여 파일로 저장합니다.
    """
    if isinstance(course_data, dict):
        course = CourseSchema(**course_data)
    else:
        course = course_data

    md_lines = []
    md_lines.append(f"# {course.title}")
    md_lines.append("")
    md_lines.append(f"> **목적지**: {course.destinationCountry} {course.destinationCity}")
    md_lines.append(f"> **일정**: {course.startDate} 부터 (총 {course.totalDays}일간)")
    if course.tags:
        tags_str = " ".join([f"`#{tag}`" for tag in course.tags])
        md_lines.append(f"> **태그**: {tags_str}")
    md_lines.append("")

    md_lines.append("## 전체 코스 핵심 추천 이유")
    md_lines.append(f"{course.recommendationReason}")
    md_lines.append("")

    md_lines.append("---")
    md_lines.append("## 일자별 상세 여행 일정 (Itinerary)")
    md_lines.append("")

    transport_map = {
        "walking": "도보",
        "transit": "대중교통",
        "driving": "렌터카/차량",
        "taxi": "택시",
        "none": "종료",
    }

    for day_item in course.itinerary.days:
        md_lines.append(f"### Day {day_item.day} ({day_item.date})")
        md_lines.append(f"> *{day_item.memo}*")
        md_lines.append("")

        md_lines.append("| 순서 | 시간 | 장소명 | 카테고리 | 체류시간 | 다음 장소 이동 | 예상 비용 |")
        md_lines.append("| :---: | :---: | :--- | :---: | :---: | :---: | :---: |")

        for stop in day_item.stops:
            transport_str = transport_map.get(stop.transportToNext, stop.transportToNext)
            if stop.travelMinutesToNext > 0:
                transport_info = f"{transport_str} ({stop.travelMinutesToNext}분)"
            else:
                transport_info = transport_str

            cost_str = f"{stop.cost:,}원" if stop.cost > 0 else "무료/기본"

            md_lines.append(
                f"| {stop.sequence} | {stop.arrivalTime} | **{stop.placeName}** | `{stop.category}` | {stop.stayMinutes}분 | {transport_info} | {cost_str} |"
            )

        md_lines.append("")
        md_lines.append("#### 장소별 상세 설명 및 방문 주의사항 (memo)")
        for stop in day_item.stops:
            md_lines.append(f"##### {stop.sequence}. {stop.placeName} (`{stop.category}`)")
            md_lines.append(f"- **도착 시각**: {stop.arrivalTime} (체류 {stop.stayMinutes}분)")
            md_lines.append(f"- **추천 이유**: {stop.reason}")
            md_lines.append(f"- **메모/주의사항**: {stop.memo}")
            md_lines.append("")

        md_lines.append("---")

    out_file = Path(output_path).resolve()
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text("\n".join(md_lines), encoding="utf-8")
    return out_file


# ----------------- A. Pytest E2E 테스트 케이스 -----------------
# 3가지 조건 (1. mbti만, 2. tasteProfile만, 3. mbti & tasteProfile 둘 다) 코스 생성 E2E 테스트
@pytest.mark.asyncio if "pytest" in sys.modules else lambda f: f
@pytest.mark.parametrize(
    "case_name, mbti_val, include_taste_profile, output_filename",
    [
        ("1. MBTI 단독 요청", "ENFP", False, "generated_course_mbti.md"),
        ("2. TasteProfile 단독 요청", None, True, "generated_course_taste.md"),
        ("3. MBTI & TasteProfile 동시 요청", "INFJ", True, "generated_course_mbti_taste.md"),
    ],
)
async def test_generate_course_e2e_combinations(case_name, mbti_val, include_taste_profile, output_filename):
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

        # 생성된 여행 코스를 개별 마크다운 파일로 추출 및 저장 검증
        md_file_path = export_course_to_markdown(
            course_data=course,
            output_path=root_dir / "tests" / "e2e" / output_filename,
        )
        assert md_file_path.exists()
        assert md_file_path.stat().st_size > 0


# ----------------- B. 직접 스크립트 실행 제어 -----------------
# 실행 예시: uv run python tests/e2e/generate_course.py
async def run_local_test():
    print("==================================================")
    print("로컬 Gemini API 기반 맞춤 코스 생성 연동 및 Markdown 추출 테스트 시작")
    print(f"- 사용 모델: {settings.GEMINI_MODEL_NAME}")
    print(f"- API 키 설정 여부: {'설정 완료 (Yes)' if settings.GEMINI_API_KEY else '설정 누락 (No)'}")
    print("==================================================")

    if not settings.GEMINI_API_KEY:
        print("[ERROR] GEMINI_API_KEY 설정이 누락되었습니다. .env를 확인해 주세요.")
        return

    cases = [
        ("1. MBTI 단독 요청 실험 (mbti='ENFP')", "ENFP", None, "generated_course_mbti.md"),
        ("2. TasteProfile 단독 요청 실험", None, get_sample_taste_profile(), "generated_course_taste.md"),
        ("3. MBTI & TasteProfile 동시 요청 실험 (mbti='INFJ')", "INFJ", get_sample_taste_profile(), "generated_course_mbti_taste.md"),
    ]

    trip_condition = get_sample_trip_condition()

    for title, mbti_val, taste_profile, output_filename in cases:
        print(f"\n[CASE] {title} 시작...")
        req_schema = CourseRequestSchema(
            userId="550e8400-e29b-41d4-a716-446655440000",
            mbti=mbti_val,
            tasteProfile=taste_profile,
            tripCondition=trip_condition,
        )

        generated_course = None
        try:
            sse_generator = await generate_course_service(req_schema)
            async for raw_chunk in sse_generator:
                lines = raw_chunk.strip().split("\n")
                for line in lines:
                    if line.startswith("data:"):
                        data_json = json.loads(line.replace("data:", "").strip())
                        if "course" in data_json:
                            generated_course = data_json["course"]

            print(f"[SUCCESS] [{title}] 코스 생성 성공!")

            if generated_course:
                output_file = root_dir / "tests" / "e2e" / output_filename
                saved_path = export_course_to_markdown(
                    course_data=generated_course,
                    output_path=output_file,
                )
                print(f"[SAVE] 마크다운 파일 저장 완료: {saved_path}")

        except Exception as e:  # noqa: BLE001
            print(f"[ERROR] [{title}] 처리 도중 에러 발생: {e!s}")


if __name__ == "__main__":
    asyncio.run(run_local_test())

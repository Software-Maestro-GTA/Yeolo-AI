from langchain_google_genai import ChatGoogleGenerativeAI

from app.agent.prompts import COURSE_GENERATION_PROMPT
from app.agent.tools.google_maps import (
    compute_route_between_places_tool,
    search_place_detail_tool,
)
from app.core.config import settings
from app.schemas.course import CourseRequestSchema, CourseSchema

# API Key가 비어 있으면 테스트/임포트 시 오류를 방지하기 위해 가짜 키로 대체
gemini_api_key = settings.GEMINI_API_KEY or "fake_gemini_api_key_for_testing"

llm = ChatGoogleGenerativeAI(
    model=settings.GEMINI_MODEL_NAME,
    google_api_key=gemini_api_key,
    temperature=0.5,
)

# Google Maps API 도구를 LLM에 바인딩
google_maps_tools = [search_place_detail_tool, compute_route_between_places_tool]
llm_with_tools = llm.bind_tools(google_maps_tools)

# CourseSchema Structured Output 체인
course_generation_chain = COURSE_GENERATION_PROMPT | llm_with_tools.with_structured_output(CourseSchema)


async def run_course_generation_chain(request: CourseRequestSchema) -> CourseSchema:
    """
    MBTI, 성향 프로필 및 여행 조건을 받아 Google Maps Tools가 결합된 LLM Agent 파이프라인을 비동기로 실행하고
    CourseSchema를 반환합니다.
    """
    mbti_str = request.mbti if request.mbti else "제공되지 않음"
    taste_profile_str = (
        request.tasteProfile.model_dump_json(indent=2)
        if request.tasteProfile
        else "제공되지 않음"
    )

    input_data = {
        "mbti": mbti_str,
        "taste_profile": taste_profile_str,
        "trip_condition": request.tripCondition.model_dump_json(indent=2),
        "total_days": request.tripCondition.totalDays,
    }

    result = await course_generation_chain.ainvoke(input_data)
    return result

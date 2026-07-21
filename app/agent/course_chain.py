"""
@file course_chain.py
@description LangChain LCEL과 ChatGoogleGenerativeAI를 활용하여 성향 프로필/여행 조건 기반 여행 코스를 구조화(CourseSchema)된 형태로 비동기 생성하는 체인 모듈
@requirements REQ-7
@functional FUN-2
@api API-BA-1
@author Antigravity Agent
"""

from langchain_google_genai import ChatGoogleGenerativeAI
from app.core.config import settings
from app.schemas.course import CourseSchema, CourseRequestSchema
from app.agent.prompts import COURSE_GENERATION_PROMPT

# API Key가 비어 있으면 테스트/임포트 시 오류를 방지하기 위해 가짜 키로 대체
gemini_api_key = settings.GEMINI_API_KEY or "fake_gemini_api_key_for_testing"

llm = ChatGoogleGenerativeAI(
    model=settings.GEMINI_MODEL_NAME,
    google_api_key=gemini_api_key,
    temperature=0.3
)

# CourseSchema Structured Output 체인
course_generation_chain = COURSE_GENERATION_PROMPT | llm.with_structured_output(CourseSchema)


async def run_course_generation_chain(request: CourseRequestSchema) -> CourseSchema:
    """
    성향 프로필과 여행 조건을 받아 LangChain 체인을 비동기(ainvoke) 실행하고 CourseSchema를 반환합니다.
    """
    input_data = {
        "taste_profile": request.tasteProfile.model_dump_json(indent=2),
        "trip_condition": request.tripCondition.model_dump_json(indent=2),
        "total_days": request.tripCondition.totalDays,
    }
    
    result = await course_generation_chain.ainvoke(input_data)
    return result

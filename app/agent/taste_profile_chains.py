"""
@file taste_profile_chains.py
@description LangChain LCEL과 ChatGoogleGenerativeAI를 활용하여 성향 분석 1, 2단계 체인 및 구조화된 출력을 정의하는 모듈
@requirements REQ-11
@functional FUN-1
@api API-BA-6
@author Antigravity Agent
"""

from langchain_google_genai import ChatGoogleGenerativeAI
from app.core.config import settings
from app.schemas.taste_profile import (
    PurposePaceCompanionOutput,
    LocationEnvironmentOutput,
    ActivityFoodSpendingOutput
)
from app.agent.prompts import (
    FACT_SHEET_PROMPT,
    PURPOSE_PACE_COMPANION_PROMPT,
    LOCATION_ENVIRONMENT_PROMPT,
    ACTIVITY_FOOD_SPENDING_PROMPT
)

# API Key가 비어 있으면 테스트 수집 및 임포트 시 Pydantic 검증 에러를 방지하기 위해 가짜 키로 대체합니다.
gemini_api_key = settings.GEMINI_API_KEY or "fake_gemini_api_key_for_testing"

# ChatGoogleGenerativeAI 모델 초기화
# google_api_key 인자를 명시하여 환경 설정의 API Key를 바인딩
llm = ChatGoogleGenerativeAI(
    model=settings.GEMINI_MODEL_NAME,
    google_api_key=gemini_api_key,
    temperature=0.2
)

# 1단계: 정성적인 여행자 특징 요약(Fact Sheet) 체인
summarize_chain = FACT_SHEET_PROMPT | llm

purpose_pace_companion_chain = PURPOSE_PACE_COMPANION_PROMPT | llm.with_structured_output(PurposePaceCompanionOutput)
location_environment_chain = LOCATION_ENVIRONMENT_PROMPT | llm.with_structured_output(LocationEnvironmentOutput)
activity_food_spending_chain = ACTIVITY_FOOD_SPENDING_PROMPT | llm.with_structured_output(ActivityFoodSpendingOutput)

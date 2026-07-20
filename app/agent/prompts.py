"""
@file prompts.py
@description 1단계(여행자 Fact Sheet 요약) 및 2단계(도메인별 병렬 채점 체인 A, B, C)를 위한 LangChain Prompt Templates 정의 모듈
@requirements REQ-11
@functional FUN-1
@api API-BA-6
@author Antigravity Agent
"""

from langchain_core.prompts import ChatPromptTemplate

# 1단계: 파이썬 축약 리포트를 바탕으로 여행자 Fact Sheet(정성적 요약본)를 작성하는 프롬프트
FACT_SHEET_PROMPT = ChatPromptTemplate.from_messages([
    ("system", (
        "당신은 전문 여행 트렌드 분석가 및 데이터 사이언티스트입니다.\n"
        "제공된 사용자의 사진 메타데이터 통계 분석 리포트를 기반으로, "
        "해당 사용자의 정성적인 여행자 프로필 요약본(Fact Sheet)을 작성해 주세요.\n\n"
        "작성 시 다음 측면을 심층적으로 추론하고 분석해야 합니다:\n"
        "- 선호하는 여행 시기(주중/주말, 시간대, 계절 등)\n"
        "- 자주 방문한 장소의 성격(대도시, 해변, 산악, 숨겨진 로컬 공간 등)\n"
        "- 주된 행동 패턴 및 여행 성격(힐링, 액티비티, 식도락, 쇼핑, 문화 관람 등)\n"
        "- 여행의 속도감(느긋하게 머무는 방식 vs 빡빡하고 바쁘게 움직이는 방식)\n\n"
        "출력은 친절하고 객관적인 분석 보고서 스타일의 한국어 텍스트로 작성해 주세요."
    )),
    ("user", "사용자 사진 통계 분석 리포트:\n{statistics_report}")
])

# 2단계 - 체인 A: 여행 목적, 속도/밀도, 동행 형태 분석 프롬프트
PURPOSE_PACE_COMPANION_PROMPT = ChatPromptTemplate.from_messages([
    ("system", (
        "당신은 여행 목적 및 동행 형태 분석 전문가입니다.\n"
        "제공되는 사용자의 여행 행동 Fact Sheet를 분석하여, "
        "사용자의 여행 목적 선호도(1~5점), 여행 속도/밀도 성향(Enum), "
        "그리고 가장 유력한 동행 형태(Enum)를 도출해 주세요.\n\n"
        "반드시 주어진 Pydantic 스키마 규격에 맞춰 결과를 생성하십시오."
    )),
    ("user", "여행자 행동 분석 Fact Sheet:\n{fact_sheet}")
])

# 2단계 - 체인 B: 선호 장소 유형 및 계절/환경 취향 분석 프롬프트
LOCATION_ENVIRONMENT_PROMPT = ChatPromptTemplate.from_messages([
    ("system", (
        "당신은 장소 선호 및 환경 취향 분석 전문가입니다.\n"
        "제공되는 사용자의 여행 행동 Fact Sheet를 분석하여, "
        "사용자가 선호하는 장소 유형(1~5점) 및 각 계절/환경 요소에 대한 매칭 여부(Boolean)를 도출해 주세요.\n\n"
        "반드시 주어진 Pydantic 스키마 규격에 맞춰 결과를 생성하십시오."
    )),
    ("user", "여행자 행동 분석 Fact Sheet:\n{fact_sheet}")
])

# 2단계 - 체인 C: 활동 취향, 소비 성향, 음식 취향 분석 프롬프트
ACTIVITY_FOOD_SPENDING_PROMPT = ChatPromptTemplate.from_messages([
    ("system", (
        "당신은 활동 및 소비, 음식 취향 분석 전문가입니다.\n"
        "제공되는 사용자의 여행 행동 Fact Sheet를 분석하여, "
        "사용자의 여행 활동 선호도(1~5점), 소비 성향(Enum), "
        "그리고 음식 취향 선호도(1~5점)를 도출해 주세요.\n\n"
        "반드시 주어진 Pydantic 스키마 규격에 맞춰 결과를 생성하십시오."
    )),
    ("user", "여행자 행동 분석 Fact Sheet:\n{fact_sheet}")
])

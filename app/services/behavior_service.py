"""
@file behavior_service.py
@description 위치/시간 전처리 메타데이터 통계 축약 및 2단계 LLM 병렬 분석 파이프라인(SSE 스트림 반환) 비즈니스 로직 구현 모듈
@requirements REQ-11
@functional FUN-1
@api API-BA-6
@author Antigravity Agent
"""

import asyncio
from collections import Counter
from typing import AsyncGenerator, Dict, Any, List

from fastapi import HTTPException
from app.schemas.behavior import BehaviorAnalysisRequest, BehaviorItemSchema
from app.schemas.taste_profile import TasteProfileSchema
from app.agent.taste_profile_chains import (
    summarize_chain,
    purpose_pace_companion_chain,
    location_environment_chain,
    activity_food_spending_chain
)

def summarize_raw_metadata(items: List[BehaviorItemSchema]) -> str:
    """수십~수백 개의 메타데이터 목록을 파이썬 규칙에 따라 1차 통계 축약 처리하여 텍스트 리포트로 생성합니다."""
    total_count = len(items)
    
    # 1. 요일 통계
    day_counter = Counter([item.timeContext.dayOfWeek for item in items])
    weekend_count = sum(1 for item in items if item.timeContext.isWeekend)
    weekday_count = total_count - weekend_count
    
    # 2. 시간대 통계
    time_bucket_counter = Counter([item.timeContext.timeBucket for item in items])
    
    # 3. 계절 통계
    season_counter = Counter([item.timeContext.season for item in items])
    
    # 4. 장소 및 장소 유형 통계
    place_names = [item.location.placeName for item in items if item.location.placeName]
    place_name_counter = Counter(place_names)
    
    place_types = []
    for item in items:
        place_types.extend(item.location.placeTypes)
    place_type_counter = Counter(place_types)
    
    # 5. 자주 간 도시/지역 통계
    cities = [item.location.city for item in items if item.location.city]
    city_counter = Counter(cities)
    
    # 정량 통계 분석 리포트 마크다운 문서 생성
    report_lines = [
        f"- 분석된 총 사진 수: {total_count}장",
        f"- 활동 요일 비율: 평일 {weekday_count}회 ({weekday_count/total_count*100:.1f}%), 주말 {weekend_count}회 ({weekend_count/total_count*100:.1f}%)",
        "- 요일 분포: " + ", ".join([f"{k}({v}회)" for k, v in day_counter.most_common()]),
        "- 시간대 분포: " + ", ".join([f"{k}({v}회)" for k, v in time_bucket_counter.most_common()]),
        "- 계절 분포: " + ", ".join([f"{k}({v}회)" for k, v in season_counter.most_common()]),
        "- 주요 방문 지역(도시): " + ", ".join([f"{k}({v}회)" for k, v in city_counter.most_common(5)]),
        "- 가장 자주 방문한 장소명: " + ", ".join([f"{k}({v}회)" for k, v in place_name_counter.most_common(5)]),
        "- 자주 노출된 장소 유형 카테고리: " + ", ".join([f"{k}({v}회)" for k, v in place_type_counter.most_common(10)])
    ]
    
    return "\n".join(report_lines)

async def analyze_behavior_stream(request: BehaviorAnalysisRequest) -> AsyncGenerator[Dict[str, Any], None]:
    """위치 및 시간 데이터를 통계 축약하고 2단계 LLM 병렬 분석 파이프라인을 거쳐 Taste Profile을 생성 및 SSE 스트리밍으로 반환합니다.

    Args:
        request (BehaviorAnalysisRequest): 분석 요청 데이터.

    Yields:
        Dict[str, Any]: SSE 스트림 이벤트 데이터 (progress 및 complete).

    Raises:
        HTTPException: 입력 메타데이터 개수가 0개이거나 부족한 경우 400 에러 발생.
    """
    # 1. 예외 처리: 입력 메타데이터 목록이 비어 있거나 부족한 경우
    if not request.items:
        raise HTTPException(
            status_code=400,
            detail="분석 가능한 전처리 메타데이터가 부족합니다."
        )

    # 1차 진행 상황 반환
    yield {
        "event": "progress",
        "data": {
            "step": "ANALYZING_PREFERENCE",
            "message": "위치·시간 패턴으로 여행 성향을 분석 중입니다."
        }
    }

    # 2. 1단계: 파이썬 기반 데이터 축약 및 정성적 특징 요약
    statistics_report = summarize_raw_metadata(request.items)
    
    try:
        # LLM을 이용해 행동 맥락 Fact Sheet 요약본 생성
        fact_sheet_response = await summarize_chain.ainvoke({"statistics_report": statistics_report})
        fact_sheet = fact_sheet_response.content
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"1단계 요약본 생성 중 AI 엔진 오류 발생: {str(e)}")

    # 3. 2단계: 도메인별 병렬 채점 (asyncio.gather 활용 병렬 구동)
    try:
        purpose_pace_result, location_env_result, activity_food_result = await asyncio.gather(
            purpose_pace_companion_chain.ainvoke({"fact_sheet": fact_sheet}),
            location_environment_chain.ainvoke({"fact_sheet": fact_sheet}),
            activity_food_spending_chain.ainvoke({"fact_sheet": fact_sheet})
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"2단계 도메인 병렬 채점 중 AI 엔진 오류 발생: {str(e)}")

    # 4. 결과 취합 및 데이터 변환 (Structured Output 가공)
    # location_environment_chain의 Boolean 결과 중 True인 키값들만 추려 seasonalEnvironmentPreference 목록 구성
    seasonal_keys = [
        "warm_region", "cold_region", "summer_resort", "winter_sports",
        "spring_flower_autumn_foliage", "dry_weather", "off_season", "peak_season"
    ]
    seasonal_preferences = [key for key in seasonal_keys if getattr(location_env_result, key, False)]

    # TasteProfileSchema에 맞게 각 컴포넌트 조립
    taste_profile = TasteProfileSchema(
        sourceType="behavior",
        travelPurpose={
            "relaxation": purpose_pace_result.relaxation,
            "sightseeing": purpose_pace_result.sightseeing,
            "culturalExperience": purpose_pace_result.culturalExperience,
            "gourmet": purpose_pace_result.gourmet,
            "natureExploration": purpose_pace_result.natureExploration,
            "activity": purpose_pace_result.activity,
            "shopping": purpose_pace_result.shopping,
            "festivalEvent": purpose_pace_result.festivalEvent,
            "wellness": purpose_pace_result.wellness,
            "selfDevelopment": purpose_pace_result.selfDevelopment
        },
        travelPaceDensity=purpose_pace_result.travelPaceDensity,
        preferredLocationType={
            "bigCity": location_env_result.bigCity,
            "smallTownAlley": location_env_result.smallTownAlley,
            "natureHinterland": location_env_result.natureHinterland,
            "beachResort": location_env_result.beachResort,
            "mountainPlateau": location_env_result.mountainPlateau,
            "historicalCity": location_env_result.historicalCity,
            "themeParkResort": location_env_result.themeParkResort,
            "famousSpotPreferred": location_env_result.famousSpotPreferred,
            "hiddenSpotPreferred": location_env_result.hiddenSpotPreferred
        },
        activityPreference={
            "viewing": activity_food_result.viewing,
            "experience": activity_food_result.experience,
            "adventure": activity_food_result.adventure,
            "photographyVideo": activity_food_result.photographyVideo,
            "gourmetExploration": activity_food_result.gourmetExploration,
            "nightlife": activity_food_result.nightlife,
            "shopping": activity_food_result.shopping,
            "relaxation": activity_food_result.relaxation,
            "localInteraction": activity_food_result.localInteraction
        },
        spendingTendency=activity_food_result.spendingTendency,
        companionType=purpose_pace_result.companionType,
        foodPreference={
            "localFoodActive": activity_food_result.localFoodActive,
            "famousRestaurantCentered": activity_food_result.famousRestaurantCentered,
            "streetFood": activity_food_result.streetFood,
            "cafeDessert": activity_food_result.cafeDessert,
            "fineDining": activity_food_result.fineDining,
            "familiarFoodPreferred": activity_food_result.familiarFoodPreferred,
            "dietaryRestriction": activity_food_result.dietaryRestriction,
            "sightseeingOverFood": activity_food_result.sightseeingOverFood
        },
        seasonalEnvironmentPreference=seasonal_preferences
    )

    # 최종 조립 결과 스트림 반환
    yield {
        "event": "complete",
        "data": {
            "tasteProfile": taste_profile.model_dump()
        }
    }

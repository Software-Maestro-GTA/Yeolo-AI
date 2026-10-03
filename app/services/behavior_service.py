import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import HTTPException

from app.agent.taste_profile_chains import (
    activity_food_spending_chain,
    location_environment_chain,
    purpose_pace_companion_chain,
    summarize_chain,
)
from app.schemas.behavior import BehaviorAnalysisRequest, BehaviorItemSchema
from app.schemas.taste_profile import TasteProfileSchema
from app.services.behavior_evidence import guard_taste_profile
from app.services.behavior_statistics import build_behavior_statistics

logger = logging.getLogger(__name__)


def summarize_raw_metadata(items: list[BehaviorItemSchema]) -> str:
    """Return structured visit statistics JSON for compatibility with callers.

    Args:
        items: Preprocessed photo metadata.
    Returns:
        Deterministic JSON statistics, without user or photo identifiers.
    """
    return json.dumps(
        build_behavior_statistics(items), ensure_ascii=False, sort_keys=True
    )


def _summary_text(content: Any) -> str:
    """Accept provider text or text blocks, ignoring non-text content."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ).strip()
    return ""


async def analyze_behavior_stream(
    request: BehaviorAnalysisRequest,
) -> AsyncGenerator[dict[str, Any]]:
    """위치 및 시간 데이터를 통계 축약하고 2단계 LLM 병렬 분석 파이프라인을 거쳐 Taste Profile을 생성 및 SSE 스트리밍으로 반환합니다.

    Args:
        request (BehaviorAnalysisRequest): 분석 요청 데이터.

    Yields:
        Dict[str, Any]: SSE 스트림 이벤트 데이터 (progress 및 complete).

    Raises:
        HTTPException: 유효한 장소·timezone 촬영 시각 데이터가 없으면 400 에러 발생.
    """
    # 1. 예외 처리: 입력 메타데이터 목록이 비어 있거나 부족한 경우
    if not request.items:
        logger.warning(
            f"[behavior_service] Empty items list for userId={request.userId}"
        )
        raise HTTPException(
            status_code=400, detail="분석 가능한 전처리 메타데이터가 부족합니다."
        )

    statistics = build_behavior_statistics(request.items)
    if not statistics["validPhotoCount"]:
        raise HTTPException(
            status_code=400, detail="분석 가능한 위치·촬영 시각 메타데이터가 없습니다."
        )

    logger.info(
        f"[behavior_service] Starting behavior analysis stream for userId={request.userId}"
    )

    # 1차 진행 상황 반환
    yield {
        "event": "progress",
        "data": {
            "step": "ANALYZING_PREFERENCE",
            "message": "위치·시간 패턴으로 여행 성향을 분석 중입니다.",
        },
    }

    # 2. 1단계: 파이썬 기반 데이터 축약 및 정성적 특징 요약
    logger.info(
        f"[behavior_service] Summarizing raw metadata for userId={request.userId} ({len(request.items)} items)"
    )
    statistics_report = json.dumps(statistics, ensure_ascii=False, sort_keys=True)

    try:
        logger.info(
            f"[behavior_service] Stage 1 LLM invoke (summarize_chain) for userId={request.userId}"
        )
        fact_sheet_response = await summarize_chain.ainvoke(
            {"statistics_report": statistics_report}
        )
        fact_sheet = (
            _summary_text(fact_sheet_response.content)
            or "요약이 없어 구조화 방문 통계를 직접 사용합니다."
        )
        logger.info(
            f"[behavior_service] Stage 1 LLM invoke completed for userId={request.userId}"
        )
    except Exception:  # noqa: BLE001 -- optional provider summary must not abort scoring
        logger.warning("Behavior summary unavailable; continuing from visit statistics")
        fact_sheet = "요약이 없어 구조화 방문 통계를 직접 사용합니다."

    # 3. 2단계: 도메인별 병렬 채점 (asyncio.gather 활용 병렬 구동)
    try:
        logger.info(
            f"[behavior_service] Stage 2 parallel LLM chains invoke for userId={request.userId}"
        )
        (
            purpose_pace_result,
            location_env_result,
            activity_food_result,
        ) = await asyncio.gather(
            purpose_pace_companion_chain.ainvoke(
                {"fact_sheet": fact_sheet, "statistics_report": statistics_report}
            ),
            location_environment_chain.ainvoke(
                {"fact_sheet": fact_sheet, "statistics_report": statistics_report}
            ),
            activity_food_spending_chain.ainvoke(
                {"fact_sheet": fact_sheet, "statistics_report": statistics_report}
            ),
        )
        logger.info(
            f"[behavior_service] Stage 2 parallel LLM chains completed for userId={request.userId}"
        )
    except Exception as e:
        logger.exception(
            f"[behavior_service] Stage 2 LLM error for userId={request.userId}"
        )
        raise HTTPException(
            status_code=500,
            detail=f"2단계 도메인 병렬 채점 중 AI 엔진 오류 발생: {e!s}",
        )

    # 4. 결과 취합 및 데이터 변환 (Structured Output 가공)
    # location_environment_chain의 Boolean 결과 중 True인 키값들만 추려 seasonalEnvironmentPreference 목록 구성
    seasonal_keys = [
        "warm_region",
        "cold_region",
        "summer_resort",
        "winter_sports",
        "spring_flower_autumn_foliage",
        "dry_weather",
        "off_season",
        "peak_season",
    ]
    seasonal_preferences = [
        key for key in seasonal_keys if getattr(location_env_result, key, False)
    ]

    # TasteProfileSchema에 맞게 각 컴포넌트 조립
    taste_profile = TasteProfileSchema(
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
            "selfDevelopment": purpose_pace_result.selfDevelopment,
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
            "hiddenSpotPreferred": location_env_result.hiddenSpotPreferred,
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
            "localInteraction": activity_food_result.localInteraction,
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
            "sightseeingOverFood": activity_food_result.sightseeingOverFood,
        },
        seasonalEnvironmentPreference=seasonal_preferences,
    )

    taste_profile, analysis_metadata = guard_taste_profile(taste_profile, statistics)

    # 최종 조립 결과 스트림 반환
    yield {
        "event": "complete",
        "data": {
            "tasteProfile": taste_profile.model_dump(),
            "analysisMetadata": analysis_metadata.model_dump(),
        },
    }

"""One-call taste inference with deterministic guards and private trace evidence."""

import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import HTTPException
from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree

from app.agent.taste_profile_chains import taste_profile_chain
from app.schemas.behavior import BehaviorAnalysisRequest
from app.schemas.taste_profile import TasteProfileSchema
from app.services.behavior_evidence import guard_taste_profile
from app.services.behavior_statistics import build_behavior_statistics

logger = logging.getLogger(__name__)


@traceable(name="taste_profile_analysis", run_type="chain")
async def _analyze_profile(statistics: dict[str, Any]) -> TasteProfileSchema:
    """Infer one profile and retain evidence only in the active internal trace.

    Args:
        statistics: Sanitized visit counts and distributions.
    Returns:
        Guarded profile with at least one seasonal/environment choice.
    Raises:
        Exception: Provider or structured output errors propagate to the SSE handler.
    """
    output = await taste_profile_chain.ainvoke({
        "statistics_report": json.dumps(statistics, ensure_ascii=False, sort_keys=True),
    })
    profile, metadata = guard_taste_profile(output, statistics)
    run = get_current_run_tree()
    if run is not None:
        try:
            run.add_metadata({"analysisMetadata": metadata.model_dump()})
        except Exception:  # noqa: BLE001 -- observability must not fail valid analysis
            logger.warning("Unable to attach taste analysis evidence to active trace")
    return profile


async def analyze_behavior_stream(
    request: BehaviorAnalysisRequest,
) -> AsyncGenerator[dict[str, Any]]:
    """Generate progress and complete events using one structured LLM request.

    Args:
        request: User metadata for visit-based taste analysis.
    Yields:
        Existing API-AI-1 SSE events, with tasteProfile as the sole complete field.
    Raises:
        HTTPException: 400 for no valid records; 500 for inference failures.
    """
    statistics = build_behavior_statistics(request.items)
    if not statistics["validPhotoCount"]:
        raise HTTPException(
            status_code=400, detail="분석 가능한 위치·촬영 시각 메타데이터가 없습니다.",
        )

    yield {
        "event": "progress",
        "data": {
            "step": "ANALYZING_PREFERENCE",
            "message": "위치·시간 패턴으로 여행 성향을 분석 중입니다.",
        },
    }
    try:
        taste_profile = await _analyze_profile(statistics)
    except Exception as exc:
        logger.exception("Single-call taste profile analysis failed")
        raise HTTPException(
            status_code=500, detail=f"취향 분석 중 AI 엔진 오류 발생: {exc!s}",
        ) from exc

    yield {
        "event": "complete",
        "data": {"tasteProfile": taste_profile.model_dump()},
    }

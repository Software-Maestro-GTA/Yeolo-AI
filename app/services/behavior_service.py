"""Sequential taste analysis and summary with guarded evidence and SSE output."""

import asyncio
import json
import logging
import math
from collections.abc import AsyncGenerator
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree

from app.agent.taste_profile_chains import (
    generate_taste_profile,
    generate_taste_summary,
)
from app.core.config import settings
from app.schemas.behavior import BehaviorAnalysisRequest
from app.schemas.taste_profile import AnalysisMetadataSchema, TasteProfileSchema
from app.schemas.taste_summary import (
    BehaviorAnalysisResponse,
    TasteSummary,
    TasteSummaryDraft,
)
from app.services.behavior_evidence import guard_taste_profile
from app.services.behavior_statistics import build_behavior_statistics
from app.services.taste_summary import (
    InsufficientTasteEvidenceError,
    SummaryContext,
    assemble_taste_summary,
    prepare_summary_context,
    validate_taste_evidence,
)

logger = logging.getLogger(__name__)
ANALYSIS_ERROR_MESSAGE = '취향 분석을 완료하지 못했습니다. 잠시 후 다시 시도해 주세요.'
ANALYSIS_TIMEOUT_MESSAGE = '취향 분석 시간이 초과되었습니다. 잠시 후 다시 시도해 주세요.'


def validate_behavior_configuration() -> None:
    """Reject missing credentials or invalid deadlines before starting an SSE response."""
    timeout = settings.TASTE_ANALYSIS_TIMEOUT_SECONDS
    if not settings.GEMINI_API_KEY.strip() or not settings.ABTO_CALLING_KEY.strip() or not math.isfinite(timeout) or timeout <= 0:
        raise HTTPException(status_code=500, detail='AI 취향 분석 설정을 확인할 수 없습니다.')


@traceable(name="taste_profile_analysis", run_type="chain")
async def _analyze_profile(
    statistics: dict[str, Any], user_id: UUID,
) -> tuple[TasteProfileSchema, AnalysisMetadataSchema]:
    """Infer one profile and retain guarded evidence for summary and tracing.

    Args:
        statistics: Sanitized visit counts and distributions.
        user_id: Existing validated server identity for ABTO request attribution.
    Returns:
        Guarded profile and internal evidence needed by the summary stage.
    Raises:
        Exception: Provider or structured output errors propagate to the SSE handler.
    """
    output = await generate_taste_profile(json.dumps(statistics, ensure_ascii=False, sort_keys=True), user_id=user_id)
    profile, metadata = guard_taste_profile(output, statistics)
    run = get_current_run_tree()
    if run is not None:
        try:
            run.add_metadata({"analysisMetadata": metadata.model_dump()})
        except Exception:  # noqa: BLE001 -- observability must not fail valid analysis
            logger.warning("Unable to attach taste analysis evidence to active trace")
    return profile, metadata


@traceable(name="taste_profile_summary", run_type="chain")
async def _summarize_profile(
    context: SummaryContext, user_id: UUID,
) -> TasteSummary:
    """Turn supported profile facts into display copy without rescoring.

    Args:
        context: Exactly three display interests and all supported recommendation interests.
        user_id: Validated identity, without the original photo payload.
    Returns:
        Summary with validated references and server-owned numeric values.
    Raises:
        Exception: Provider, schema or evidence-reference validation failures.
    """
    output = await generate_taste_summary(
        json.dumps(context.model_input(), ensure_ascii=False, sort_keys=True), user_id=user_id,
    )
    return assemble_taste_summary(TasteSummaryDraft.model_validate(output), context)


async def analyze_behavior_stream(
    request: BehaviorAnalysisRequest,
) -> AsyncGenerator[dict[str, Any]]:
    """Generate analysis/summary progress and one complete event using two calls.

    Args:
        request: User metadata for visit-based taste analysis.
    Yields:
        API-AI-1 events; complete contains tasteProfile and summary only.
    Raises:
        HTTPException: 400 for no valid records; 500 for inference failures.
        InsufficientTasteEvidenceError: Valid sample size, dates or observed interest coverage is insufficient.
    """
    statistics = build_behavior_statistics(request.items)
    if not statistics["validPhotoCount"]:
        raise HTTPException(
            status_code=400, detail="분석 가능한 위치·촬영 시각 메타데이터가 없습니다.",
        )
    validate_taste_evidence(statistics)
    validate_behavior_configuration()

    yield {
        "event": "progress",
        "data": {
            "step": "ANALYZING_PREFERENCE",
            "message": "사용자 여행 성향을 분석 중입니다.",
        },
    }
    try:
        deadline = asyncio.get_running_loop().time() + settings.TASTE_ANALYSIS_TIMEOUT_SECONDS
        async with asyncio.timeout_at(deadline):
            taste_profile, metadata = await _analyze_profile(statistics, request.userId)
            context = prepare_summary_context(taste_profile, metadata, request.language)
        # Never suspend a generator inside a timeout scope: it can cancel the SSE consumer.
        yield {
            "event": "progress",
            "data": {"step": "GENERATING_SUMMARY", "message": "사용자 취향 정보를 요약합니다."},
        }
        async with asyncio.timeout_at(deadline):
            summary = await _summarize_profile(context, request.userId)
            result = BehaviorAnalysisResponse(tasteProfile=taste_profile, summary=summary)
    except InsufficientTasteEvidenceError:
        raise
    except TimeoutError as exc:
        logger.warning('Taste profile analysis exceeded its deadline', exc_info=True)
        raise HTTPException(status_code=500, detail=ANALYSIS_TIMEOUT_MESSAGE) from exc
    except Exception as exc:
        logger.exception("Taste profile analysis or summary failed")
        raise HTTPException(
            status_code=500, detail=ANALYSIS_ERROR_MESSAGE,
        ) from exc

    yield {
        "event": "complete",
        "data": result.model_dump(),
    }

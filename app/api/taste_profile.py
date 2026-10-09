import json
import logging

from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.config import settings
from app.schemas.behavior import BehaviorAnalysisRequest
from app.services.behavior_service import (
    ANALYSIS_ERROR_MESSAGE,
    ANALYSIS_TIMEOUT_MESSAGE,
    analyze_behavior_stream,
    validate_behavior_configuration,
)
from app.services.behavior_statistics import build_behavior_statistics

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/ai/taste-profile", tags=["Taste Profile"])


@router.post("/analysis", response_model=None)
@router.post("/behavior", response_model=None)
async def analyze_behavior_api(
    request: BehaviorAnalysisRequest,
    x_internal_api_key: str = Header(..., alias="X-Internal-Api-Key"),
) -> JSONResponse | StreamingResponse:
    """전처리된 이미지 메타데이터 리스트를 기반으로 사용자의 여행 성향(Taste Profile)을 추출하여 SSE 스트림으로 반환합니다. (API-AI-1)"""
    # 1. 인증 헤더 검증
    if x_internal_api_key != settings.INTERNAL_API_KEY:
        logger.warning(
            f"Unauthorized access attempt to /internal/ai/taste-profile/analysis for userId={request.userId}"
        )
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"status": 401, "message": "내부 인증 실패", "data": None},
        )

    # 2. 데이터 유효성 검사 (데이터 부족 예외 처리)
    if (
        not request.items
        or not build_behavior_statistics(request.items)["validPhotoCount"]
    ):
        logger.warning(
            f"Behavior analysis requested with no valid items for userId={request.userId}"
        )
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "status": 400,
                "message": "분석 가능한 전처리 메타데이터가 부족합니다.",
                "data": None,
            },
        )

    try:
        validate_behavior_configuration()
    except HTTPException as error:
        return JSONResponse(
            status_code=error.status_code,
            content={"status": error.status_code, "message": error.detail, "data": None},
        )

    logger.info(
        f"Behavior analysis request received for userId={request.userId} with {len(request.items)} items"
    )

    # 3. SSE 비동기 스트리밍 발생
    async def sse_generator():
        try:
            async for event in analyze_behavior_stream(request):
                # SSE 포맷 규격에 맞추어 \n\n으로 이벤트를 구분하여 반환
                yield f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
        except HTTPException as e:
            logger.warning(
                f"HTTPException in behavior stream for userId={request.userId} -> Code {e.status_code}: {e.detail}"
            )
            message = e.detail
            if e.status_code >= 500:
                message = ANALYSIS_TIMEOUT_MESSAGE if e.detail == ANALYSIS_TIMEOUT_MESSAGE else ANALYSIS_ERROR_MESSAGE
            error_data = {"status": e.status_code, "message": message}
            yield f"event: error\ndata: {json.dumps(error_data, ensure_ascii=False)}\n\n"
        except Exception:
            logger.exception(f"Error in behavior stream for userId={request.userId}")
            error_data = {
                "status": 500,
                "message": ANALYSIS_ERROR_MESSAGE,
            }
            yield f"event: error\ndata: {json.dumps(error_data, ensure_ascii=False)}\n\n"

    return StreamingResponse(sse_generator(), media_type="text/event-stream")

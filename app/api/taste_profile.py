import json
import logging

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.config import settings
from app.schemas.behavior import BehaviorAnalysisRequest
from app.services.behavior_service import analyze_behavior_stream

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/ai/taste-profile", tags=["Taste Profile"])

@router.post("/analysis")
@router.post("/behavior")
async def analyze_behavior_api(
    request: BehaviorAnalysisRequest,
    raw_request: Request,
    x_internal_api_key: str = Header(..., alias="X-Internal-Api-Key")
):
    """전처리된 이미지 메타데이터 리스트를 기반으로 사용자의 여행 성향(Taste Profile)을 추출하여 SSE 스트림으로 반환합니다. (API-AI-1)"""
    # 1. 인증 헤더 검증
    if x_internal_api_key != settings.INTERNAL_API_KEY:
        logger.warning(f"Unauthorized access attempt to /internal/ai/taste-profile/analysis for userId={request.userId}")
        logger.warning(f"외부 서버 API 요청 헤더 전체: {dict(raw_request.headers)}")
        logger.warning(f"수신된 API Key: '{x_internal_api_key}', 서버 설정 API Key: '{settings.INTERNAL_API_KEY}'")
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={
                "status": 401,
                "message": "내부 인증 실패",
                "data": None
            }
        )

    # 2. 데이터 유효성 검사 (데이터 부족 예외 처리)
    if not request.items:
        logger.warning(f"Behavior analysis requested with empty items for userId={request.userId}")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "status": 400,
                "message": "분석 가능한 전처리 메타데이터가 부족합니다.",
                "data": None
            }
        )

    logger.info(f"Behavior analysis request received for userId={request.userId} with {len(request.items)} items")

    # 3. SSE 비동기 스트리밍 발생
    async def sse_generator():
        try:
            async for event in analyze_behavior_stream(request):
                # SSE 포맷 규격에 맞추어 \n\n으로 이벤트를 구분하여 반환
                yield f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
        except HTTPException as e:
            logger.warning(f"HTTPException in behavior stream for userId={request.userId} -> Code {e.status_code}: {e.detail}")
            error_data = {"status": e.status_code, "message": e.detail}
            yield f"event: error\ndata: {json.dumps(error_data, ensure_ascii=False)}\n\n"
        except Exception as e:
            logger.exception(f"Error in behavior stream for userId={request.userId}")
            error_data = {"status": 500, "message": f"AI 분석 스트림 중 에러 발생: {e!s}"}
            yield f"event: error\ndata: {json.dumps(error_data, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream"
    )


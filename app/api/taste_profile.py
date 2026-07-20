"""
@file taste_profile.py
@description API-BA-6 (전처리 이미지 메타데이터 기반 성향 분석) API 엔드포인트 구현 및 인증, SSE 비동기 스트림 처리 모듈
@requirements REQ-11
@functional FUN-1
@api API-BA-6
@author Antigravity Agent
"""

import json
from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import StreamingResponse, JSONResponse
from app.schemas.behavior import BehaviorAnalysisRequest
from app.services.behavior_service import analyze_behavior_stream
from app.core.config import settings

router = APIRouter(prefix="/internal/ai/taste-profile", tags=["Taste Profile"])

@router.post("/behavior")
async def analyze_behavior_api(
    request: BehaviorAnalysisRequest,
    x_internal_api_key: str = Header(..., alias="X-Internal-Api-Key")
):
    """전처리된 이미지 메타데이터 리스트를 기반으로 사용자의 여행 성향(Taste Profile)을 추출하여 SSE 스트림으로 반환합니다.

    Args:
        request (BehaviorAnalysisRequest): 분석용 데이터.
        x_internal_api_key (str): 내부 통신 인증용 헤더 값.

    Returns:
        StreamingResponse: event: progress, event: complete를 포함한 SSE 응답.
        JSONResponse: 인증 실패(401) 또는 데이터 부족(400) 시 JSON 응답.
    """
    # 1. 인증 헤더 검증
    if x_internal_api_key != settings.INTERNAL_API_KEY:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={
                "status": 401,
                "message": "내부 인증 실패"
            }
        )

    # 2. 데이터 유효성 검사 (데이터 부족 예외 처리)
    if not request.items:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "status": 400,
                "message": "분석 가능한 전처리 메타데이터가 부족합니다."
            }
        )

    # 3. SSE 비동기 스트리밍 발생
    async def sse_generator():
        try:
            async for event in analyze_behavior_stream(request):
                # SSE 포맷 규격에 맞추어 \n\n으로 이벤트를 구분하여 반환
                yield f"event: {event['event']}\ndata: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
        except HTTPException as e:
            error_data = {"status": e.status_code, "message": e.detail}
            yield f"event: error\ndata: {json.dumps(error_data, ensure_ascii=False)}\n\n"
        except Exception as e:
            error_data = {"status": 500, "message": f"AI 분석 스트림 중 에러 발생: {str(e)}"}
            yield f"event: error\ndata: {json.dumps(error_data, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream"
    )

"""
@file course.py
@description API-BA-1 (성향 프로필 기반 여행 코스 생성) API 엔드포인트 및 인증, SSE 비동기 스트림 처리 모듈
@requirements REQ-7
@functional FUN-2
@api API-BA-1
@author Antigravity Agent
"""

from fastapi import APIRouter, Header, HTTPException, status
from fastapi.responses import StreamingResponse, JSONResponse
from app.schemas.course import CourseRequestSchema
from app.services.course_service import generate_course_service
from app.core.config import settings

router = APIRouter(prefix="/internal/ai", tags=["Course Generation"])


@router.post("/courses")
async def generate_course_api(
    request: CourseRequestSchema,
    x_internal_api_key: str = Header(None, alias="X-Internal-Api-Key"),
):
    """
    성향 프로필 및 여행 제약 조건을 기반으로 개인 맞춤형 여행 코스를 생성하여 SSE 스트림으로 반환합니다.
    """
    # 1. 인증 헤더 검증
    if not x_internal_api_key or x_internal_api_key != settings.INTERNAL_API_KEY:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"status": 401, "message": "내부 인증 실패"},
        )

    try:
        # 코스 생성 비동기 처리 및 SSE generator 획득
        sse_generator = await generate_course_service(request)
        return StreamingResponse(
            sse_generator, media_type="text/event-stream"
        )
    except HTTPException as e:
        return JSONResponse(
            status_code=e.status_code,
            content={"status": e.status_code, "message": e.detail},
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"status": 500, "message": f"AI 코스 생성 중 오류가 발생했습니다: {str(e)}"},
        )

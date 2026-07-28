"""
@file course_service.py
@description 성향 프로필 및 여행 조건을 처리하여 SSE 스트리밍 데이터를 생성하고 예외 정책(FUN-2)을 관리하는 서비스 모듈
@requirements REQ-7
@functional FUN-2
@api API-BA-1
@author Antigravity Agent
"""

import json
import logging
from typing import AsyncGenerator
from fastapi import HTTPException
from app.schemas.course import CourseRequestSchema, CourseSchema
from app.agent.course_chain import run_course_generation_chain

logger = logging.getLogger(__name__)


async def generate_course_service(request: CourseRequestSchema) -> AsyncGenerator[str, None]:
    """
    성향 프로필과 여행 조건 기반 코스를 비동기로 생성한 후 SSE 스트림을 반환합니다.
    1) LLM 파이프라인 실행 중 오류나 조건 미충족 시 404 / 500 HTTPException 발생 (StreamingResponse 시작 전)
    2) 정상 생성 완료 시 event: progress, event: complete SSE 메시지 스트리밍
    """
    logger.info(f"[course_service] Invoking course_generation_chain for userId={request.userId}")
    try:
        # LLM 비동기 파이프라인 호출
        course_result: CourseSchema = await run_course_generation_chain(request)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[course_service] Error in course generation chain for userId={request.userId}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="AI 코스 생성 중 오류가 발생했습니다.")

    if not course_result:
        # 조건에 맞는 장소가 없거나 결과 생성이 안된 경우
        logger.warning(f"[course_service] No course generated for userId={request.userId}")
        raise HTTPException(status_code=404, detail="조건에 맞는 장소가 없습니다.")

    logger.info(f"[course_service] Successfully generated course '{course_result.title}' for userId={request.userId}. Starting SSE stream.")

    async def sse_generator() -> AsyncGenerator[str, None]:
        # 1. 진행 상태 전송 (event: progress)
        progress_data = {
            "step": "GENERATING_ROUTE",
            "message": "장소와 이동 순서를 구성 중입니다.",
        }
        logger.debug(f"[course_service] Yielding event: progress for userId={request.userId}")
        yield f"event: progress\ndata: {json.dumps(progress_data, ensure_ascii=False)}\n\n"

        # 2. 최종 완료 응답 전송 (event: complete)
        complete_data = {"course": course_result.model_dump()}
        logger.info(f"[course_service] Yielding event: complete for userId={request.userId}")
        yield f"event: complete\ndata: {json.dumps(complete_data, ensure_ascii=False)}\n\n"

    return sse_generator()


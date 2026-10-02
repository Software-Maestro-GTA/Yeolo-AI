"""Deliver live graph progress through the existing SSE event contract."""

import asyncio
import json
import logging
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import aclosing, suppress
from datetime import date, timedelta

from fastapi import HTTPException

from app.agent.course_graph import stream_course_generation
from app.core.config import settings
from app.schemas.course import CourseRequestSchema
from app.services.course_history import CourseHistory

logger = logging.getLogger(__name__)


async def generate_course_service(request: CourseRequestSchema) -> AsyncGenerator[str]:
    """Validate before headers and return a stream of real generation progress.

    Args:
        request: Existing course generation request.
    Returns:
        SSE iterator using only progress/complete and the existing payload fields.
    Raises:
        HTTPException: Invalid input or missing provider configuration before
            response headers are sent. Unavailable history permits generation.

    Once streaming starts, failures emit progress and close without complete.
    Cancellation closes the graph and its outstanding external operations.
    """
    try:
        start = date.fromisoformat(request.tripCondition.startDate)
        start + timedelta(days=request.tripCondition.totalDays - 1)
    except (ValueError, OverflowError):
        raise HTTPException(status_code=400, detail='코스 생성 조건이 올바르지 않습니다.') from None
    if not request.tripCondition.destinationCity.strip() or not request.tripCondition.destinationCountry.strip():
        raise HTTPException(status_code=400, detail='코스 생성 조건이 올바르지 않습니다.')
    if not settings.GEMINI_API_KEY or not settings.GOOGLE_MAPS_API_KEY:
        raise HTTPException(status_code=500, detail='AI 코스 생성 설정을 확인할 수 없습니다.')
    try:
        await CourseHistory().ensure_available()
    except (OSError, sqlite3.Error):
        logger.warning('Course history preflight unavailable; generating with limited duplicate protection')

    async def sse_generator() -> AsyncGenerator[str]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=8)
        finished = object()

        async def produce() -> None:
            try:
                async with aclosing(stream_course_generation(request)) as source:
                    async for item in source:
                        await queue.put(item)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - sanitize the provider boundary
                logger.error('Course generation failed after stream start: %s', type(error).__name__)
                await queue.put(('progress', {'step': 'GENERATING_ROUTE', 'message': '검증 가능한 여행 코스를 완성하지 못했습니다. 잠시 후 다시 시도해 주세요.'}))
            finally:
                # A cancelled producer must not block forever on a full queue.
                if not asyncio.current_task().cancelling():
                    await queue.put(finished)

        producer = asyncio.create_task(produce())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ': keep-alive\n\n'
                    continue
                if item is finished:
                    break
                event, payload = item
                if event == 'complete':
                    # Join before publishing, so closing a completed stream cannot cancel it.
                    await producer
                yield f'event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'
                if event == 'complete':
                    break
        finally:
            if not producer.done():
                producer.cancel()
            with suppress(asyncio.CancelledError):
                await producer

    return sse_generator()

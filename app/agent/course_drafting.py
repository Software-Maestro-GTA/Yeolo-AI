"""Propose daily candidates with bounded concurrent LLM requests.

Preserve completed days, retry temporary provider failures and close SDK clients."""

import asyncio
import logging
import uuid
from datetime import date, timedelta

import httpx
from google.genai.errors import APIError
from langchain_core.exceptions import OutputParserException
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import ValidationError

from app.agent.course_state import CourseDraft, DraftDay, PartialDraftError
from app.agent.prompts import COURSE_CANDIDATE_PROMPT
from app.core.config import settings
from app.schemas.course import (
    CourseRequestSchema,
)

logger = logging.getLogger(__name__)

DAILY_DRAFT_TIMEOUT_SECONDS = 60.0


MIN_DAILY_DRAFT_BUDGET_SECONDS = 10.0


async def _draft_day_candidates(
    request: CourseRequestSchema,
    recent_ids: list[str],
    feedback: str = "",
    *,
    day_index: int,
    seed: str,
    deadline: float | None = None,
    day_hints: list[str] | None = None,
) -> CourseDraft:
    """Generate exactly one assigned day, keeping whole-trip planning context.

    The SDK receives a valid 60-second deadline; the local timer additionally
    honors the shared absolute deadline. Calls with under ten seconds left skip
    the provider, allowing the caller to retain already completed days.

    Raises:
        TimeoutError: Insufficient budget to start, or local execution timed out.
        ValueError: The provider did not return exactly one day.
    """
    remaining = (
        DAILY_DRAFT_TIMEOUT_SECONDS
        if deadline is None
        else min(
            DAILY_DRAFT_TIMEOUT_SECONDS, deadline - asyncio.get_running_loop().time()
        )
    )
    if remaining < MIN_DAILY_DRAFT_BUDGET_SECONDS:
        raise TimeoutError("Insufficient budget to start a daily candidate request")
    assigned = date.fromisoformat(request.tripCondition.startDate) + timedelta(
        days=day_index
    )
    context = f"이번 호출은 {day_index + 1}일차 {assigned.isoformat()}만 생성합니다. 전체 여행 조건은 맥락이며 days는 정확히 1개만 반환하세요. reason은 25자 이내 한 문장만 작성하세요. 다른 날짜에 배정한 권역/장소와 겹치지 마세요.\n전체 일자별 계획 힌트: {day_hints or []}\n{feedback}"
    # The provider deadline stays valid even when the local request expires sooner.
    model = ChatGoogleGenerativeAI(
        model=settings.GEMINI_MODEL_NAME,
        google_api_key=settings.GEMINI_API_KEY,
        thinking_level="low",
        max_retries=0,
        timeout=DAILY_DRAFT_TIMEOUT_SECONDS,
        max_output_tokens=5000,
    )
    try:
        # Suppress LangChain's candidate_count=1 default; GenAI omits None on the wire.
        chain = COURSE_CANDIDATE_PROMPT | model.with_structured_output(
            CourseDraft
        ).bind(generation_config={"candidate_count": None})
        async with asyncio.timeout(remaining):
            result = await chain.ainvoke(
                {
                    "mbti": request.mbti or "미제공",
                    "taste_profile": request.tasteProfile.model_dump_json()
                    if request.tasteProfile
                    else "미제공",
                    "trip_condition": request.tripCondition.model_dump_json(),
                    "recent_ids": ",".join(recent_ids[:120]),
                    "feedback": context,
                    "seed": seed,
                }
            )
        if len(result.days) != 1:
            raise OutputParserException("Return only the assigned day in days")
        return result
    finally:
        try:
            try:
                await model.client.aio.aclose()
            except Exception:  # noqa: BLE001 - client cleanup must not discard valid candidates
                logger.warning("Course model async client cleanup unavailable")
        finally:
            try:
                model.client.close()
            except Exception:  # noqa: BLE001 - preserve the original result or failure
                logger.warning("Course model client cleanup unavailable")


async def draft_candidates(
    request: CourseRequestSchema,
    recent_ids: list[str],
    feedback: str = "",
    *,
    pending_days: list[int] | None = None,
    existing_days: dict[int, DraftDay] | None = None,
    deadline: float | None = None,
) -> CourseDraft:
    """Generate days concurrently; retry only failed calls and retain every success.

    Day indices are assigned by the caller, never inferred from model output.
    Cancellation always joins started children before returning.

    Args:
        request: Travel conditions and user preferences.
        recent_ids: Previously selected venues to avoid where possible.
        feedback: Validation feedback for the candidate model.
        pending_days: Zero-based dates still requiring candidate proposals.
        existing_days: Successful proposals retained from previous calls.
        deadline: Shared absolute event-loop deadline, when supplied.

    Returns:
        Complete candidate draft ordered by the requested dates.

    Raises:
        PartialDraftError: Some days remain unfinished; carries successful days.
        APIError: A nonrecoverable model provider failure propagates.
    """
    days = dict(existing_days or {})
    pending = (
        list(range(request.tripCondition.totalDays))
        if pending_days is None
        else pending_days
    )
    seed = uuid.uuid4().hex[:12]
    directions = (
        "도심 중심 권역",
        "도심 동쪽 권역",
        "도심 서쪽 권역",
        "도심 남쪽 권역",
        "도심 북쪽 권역",
    )
    shift = int(seed[:4], 16) % len(directions)
    directions = directions[shift:] + directions[:shift]
    hints = [
        f"{i + 1}일차: {directions[i % len(directions)]}, 다른 날짜와 구분되는 인접한 개별 장소 묶음 {i + 1}"
        for i in range(request.tripCondition.totalDays)
    ]
    if days:
        feedback += "\n유지할 다른 날짜의 장소(중복 금지): " + "; ".join(
            f"{index + 1}일차: "
            + ", ".join(candidate.name for candidate in day.candidates)
            for index, day in sorted(days.items())
        )
    semaphore = asyncio.Semaphore(5)
    errors: dict[int, Exception] = {}

    async def one(index: int) -> None:
        async with semaphore:
            for attempt in range(2):
                started = asyncio.get_running_loop().time()
                try:
                    result = await _draft_day_candidates(
                        request,
                        recent_ids,
                        feedback,
                        day_index=index,
                        seed=seed,
                        deadline=deadline,
                        day_hints=hints,
                    )
                    days[index] = result.days[0]
                    errors.pop(index, None)
                    return
                except Exception as error:
                    provider_error = _draft_api_error(error)
                    logger.warning(
                        "Course daily draft failed: day=%d attempt=%d error=%s code=%s elapsed=%.2f",
                        index + 1,
                        attempt + 1,
                        type(error).__name__,
                        provider_error.code if provider_error is not None else None,
                        asyncio.get_running_loop().time() - started,
                    )
                    if not _recoverable_draft(error):
                        raise
                    errors[index] = error
                    if attempt or (
                        deadline is not None
                        and deadline - asyncio.get_running_loop().time()
                        < MIN_DAILY_DRAFT_BUDGET_SECONDS + 0.2
                    ):
                        return
                    await asyncio.sleep(0.2)

    tasks = [asyncio.create_task(one(index)) for index in pending]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    if errors or len(days) != request.tripCondition.totalDays:
        raise PartialDraftError(days, errors)
    return CourseDraft(
        title=f"{request.tripCondition.destinationCity} {request.tripCondition.totalDays}일 여행",
        reason="",
        tags=[],
        days=[days[index] for index in range(request.tripCondition.totalDays)],
    )


def _draft_api_error(error: BaseException) -> APIError | None:
    """Find a typed provider failure through wrappers without revisiting cycles."""
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, APIError):
            return current
        if current.__context__ is not None:
            pending.append(current.__context__)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
    return None


def _recoverable_draft(error: BaseException) -> bool:
    """Retry typed temporary provider/model failures while propagating cancellation."""
    if isinstance(error, asyncio.CancelledError):
        return False
    provider_error = _draft_api_error(error)
    if provider_error is not None:
        return provider_error.code == 429 or 500 <= provider_error.code < 600
    return isinstance(
        error,
        (
            TimeoutError,
            httpx.TimeoutException,
            httpx.NetworkError,
            OutputParserException,
            ValidationError,
        ),
    )

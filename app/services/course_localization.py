"""Translate course names and visit prose, leaving provider route memos intact.

Protected source facts, partial-result recovery and client cleanup are retained.
"""

import asyncio
import json
import logging
import math
import re
from collections import defaultdict
from typing import Any

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from app.core.config import settings
from app.core.llm import get_abto
from app.schemas.course import CourseRequestSchema, CourseSchema
from app.services.course_display_copy import DISPLAY_RULES, DisplayCopy, DisplayCopyPlan
from app.services.course_display_diagnostics import DisplayDiagnostics
from app.services.display_language import is_korean_text

logger = logging.getLogger(__name__)
LOCALIZATION_TIMEOUT_SECONDS = 60.0

PROGRESS_MESSAGES = {
    'anchors': '여행에 함께할 공항과 숙소 후보를 확인하고 있어요.',
    'airports': '첫날과 마지막 날에 공항과 이동을 연결하고 있어요.',
    'airports_unavailable': '이용 공항을 확인하지 못해 공항 구간을 제외한 일정으로 완성했어요.',
    'lodging': '숙박하는 날의 마지막에 숙소와 이동을 연결하고 있어요.',
    'lodging_unavailable': '숙소 위치를 확인하지 못해 관광·식사 일정으로 완성했어요.',
    'prepare': '여행 조건과 최근 코스 이력을 확인하고 있습니다.',
    'draft': '취향에 맞는 장소 후보와 식사 대안을 구성하고 있습니다.',
    'verify_places': '장소의 실제 위치와 목적지 일치 여부를 확인하고 있습니다.',
    'verify_routes': '실제 이동 시간을 바탕으로 일정과 대안을 확인하고 있습니다.',
    'schedule': '검증을 마친 일자별 장소와 이동 일정의 일치 여부를 최종 확인하고 있습니다.',
    'finalize': '확정된 코스의 비용과 중복 여부를 확인하고 있습니다.',
    'refill': '장소가 부족한 일차에 가까운 명소를 보충하고 있습니다.',
    'repair': '확인한 장소로 이동과 식사에 여유가 있는 대안 일정을 구성하고 있습니다.',
    'restore': '확인된 장소와 이동 경로를 유지하여 일정을 마무리하고 있습니다.',
    'translate_display': '장소 이름과 표시 문구를 한국어로 정리하고 있습니다.',
    'generate_place_copy': '장소별 추천 이유와 방문 팁을 작성하고 검토하고 있습니다.',
    'quote': '왕복 이동·숙박 비용을 확인하고 여행 예산을 배분하고 있습니다.',
    'quote_estimated': '예상 경비에 {items} 기준 추정액을 포함했습니다.',
    'budget_over': '현재 예상 경비 {total:,.0f}원은 입력하신 예산 {budget:,.0f}원보다 높습니다. 확인된 코스는 유지했습니다.',
    'history_unavailable': '최근 이력 조회 없이 확인된 코스를 구성하고 있습니다.',
    'repeated_supplemental': '확인된 장소의 안전한 이동 순서를 유지해 코스를 완성하고 있습니다.',
    'failure': '여행 코스를 완성하지 못했습니다. 잠시 후 다시 시도해 주세요.',
}

QUOTE_NAMES = {
    'transport': '왕복 이동',
    'flight': '항공',
    'lodging': '숙박',
}


def progress_message(language: str, key: str, **values: Any) -> str:
    """Return Korean SSE text for a course generation stage key.

    Args:
        language: Legacy locale argument; output is always Korean.
        key: Stage key shared by course nodes and the SSE service.
        values: quote_estimated uses items, budget_over uses total/budget.
    Returns:
        Progress text only; the existing GENERATING_ROUTE contract is unchanged.
    """
    if key not in PROGRESS_MESSAGES:
        return key
    values = dict(values)
    if key == "quote_estimated":
        items = values.get("items", ("transport", "lodging"))
        if isinstance(items, str):
            items = re.split(r"[,·/\s]+", items)
        aliases = {
            "항공": "flight",
            "항공권": "flight",
            "숙소": "lodging",
            "숙박": "lodging",
            "교통": "transport",
        }
        names = (
            [aliases.get(item, item) for item in items]
            if isinstance(items, (tuple, list))
            else []
        )
        recognized = [name for name in names if name in QUOTE_NAMES]
        values["items"] = " / ".join(
            QUOTE_NAMES[name]
            for name in (recognized or ["transport", "lodging"])
        )
    if key == "budget_over":
        values.setdefault("total", 0)
        values.setdefault("budget", 0)
    return PROGRESS_MESSAGES[key].format_map(
        defaultdict(str, values)
    )


def _finish_display(course: CourseSchema) -> CourseSchema:
    """Clear unavailable visit copy without creating replacement sentences.

    Args:
        course: Itinerary whose display labels and existing visit copy were localized.
    Returns:
        Same course; invalid-language visit fields become empty strings.
        Missing visit prose is written only by the grounded place-copy service.
    """
    for day in course.itinerary.days:
        for stop in day.stops:
            if stop.transportToNext.type in {'none', 'unknown'}:
                stop.transportToNext.memo = None
            for field in ('reason', 'memo'):
                if not is_korean_text(getattr(stop, field)):
                    setattr(stop, field, '')
    return course


async def localize_course(
    request: CourseRequestSchema,
    course: CourseSchema,
    *,
    timeout_seconds: float = LOCALIZATION_TIMEOUT_SECONDS,
    diagnostics: DisplayDiagnostics | None = None,
) -> CourseSchema:
    """Translate labels and existing visit copy without touching route guidance.

    Args:
        request: Identity for the existing course-generation model capability.
        course: Complete verified itinerary; only names/title/tags/visit copy may change.
        timeout_seconds: Optional remaining request budget; at most sixty seconds.
        diagnostics: Optional internal report; never included in the public course.
    Returns:
        The same course object with identities, prices and provider route memos
        kept intact. Failed display translations retain their original labels.
    Raises:
        asyncio.CancelledError: Caller cancellation; owned HTTP clients still close.
    """
    display = DisplayCopyPlan(course)
    report = diagnostics if diagnostics is not None else DisplayDiagnostics('translation')
    report.total = len(display.units)
    if not display.units:
        report.finish(0)
        return _finish_display(course)
    sync_options = None
    async_options = None
    phase = 'setup'
    cancelled = False
    try:
        if math.isnan(timeout_seconds) or timeout_seconds <= 0.1:
            phase = 'budget'
            raise TimeoutError("No remaining budget for optional localization")
        async with asyncio.timeout(min(LOCALIZATION_TIMEOUT_SECONDS, timeout_seconds)):
            abto = get_abto()
            sync_options = abto.openai_options()
            async_options = abto.async_openai_options()
            model = ChatOpenAI(
                model=settings.GEMINI_MODEL_NAME,
                use_responses_api=False,
                temperature=None,
                reasoning_effort="low",
                max_retries=0,
                timeout=LOCALIZATION_TIMEOUT_SECONDS,
                max_tokens=6000,
                **sync_options,
                http_async_client=async_options["http_client"],
            )
            # The OpenAI SDK's native Pydantic parse path serializes strict=True.
            # Use an explicit JSON schema and validate with Pydantic locally so
            # the Gateway actually receives the requested strict=False value.
            source = {'texts': display.units}
            prompt = ChatPromptTemplate.from_messages([
                ('system', DISPLAY_RULES),
                ('human', 'Target language: Korean (ko)\nVerified source copy:\n{source}'),
            ])
            chain = prompt | model.with_structured_output(
                DisplayCopy.model_json_schema(),
                method="json_schema",
                strict=False,
            )
            # Translation is part of the existing course-generation capability.
            # Reuse its configured Gemini route rather than requiring a separate
            # dashboard feature, which otherwise rejects requests before the LLM.
            with abto.with_context(
                feature_id="course.candidates.generate", device_id=str(request.userId)
            ):
                phase = 'display_translation'
                report.call(phase)
                result = await chain.ainvoke(
                    {
                        "source": json.dumps(source, ensure_ascii=False),
                    }, config=report.config(phase),
                )
            phase = 'translation_validation'
            raw = result.model_dump() if isinstance(result, BaseModel) else result
            if not isinstance(raw, dict) or not isinstance(raw.get('texts'), list):
                raise TypeError('Missing structured localization response')
            display.apply(display.response_texts(result))
            pending = display.pending_units()
            if pending:
                report.fail(phase, 'untranslated_or_rejected', count=len(pending), attempt=1)
                report.retries += 1
                logger.warning('Optional display translation repair: language=%s pending=%d total=%d', request.language, len(pending), len(display.units))
                repair_prompt = ChatPromptTemplate.from_messages([
                    ('system', DISPLAY_RULES + ' A previous response omitted these slots or failed validation. '
                     'If a slot includes previous_text and validation_error, correct that rejected draft using '
                     'the original text as the source of truth; return only id and text, never those diagnostic fields. '
                     'Translate only the supplied labels or existing prose into Korean. '
                     'Return names as labels and preserve every numeric placeholder exactly.'),
                    ('human', 'Target language: Korean (ko)\nVerified source copy:\n{source}'),
                ])
                repair_chain = repair_prompt | model.with_structured_output(
                    DisplayCopy.model_json_schema(), method='json_schema', strict=False,
                )
                # Share the original timeout and SDK clients; retry only rejected
                # text once, with no additional Maps lookup or route generation.
                with abto.with_context(feature_id='course.candidates.generate', device_id=str(request.userId)):
                    phase = 'display_translation_repair'
                    report.call(phase)
                    repaired = await repair_chain.ainvoke({
                        'source': json.dumps({'texts': pending}, ensure_ascii=False),
                    }, config=report.config(phase, attempt=2))
                phase = 'translation_validation'
                pending_ids = {unit['id'] for unit in pending}
                display.apply([item for item in display.response_texts(repaired) if item.id in pending_ids])
                missing = display.pending_units()
                if missing:
                    report.fail(phase, 'untranslated_or_rejected', count=len(missing), attempt=2)
            return _finish_display(course)
    except asyncio.CancelledError as error:
        cancelled = True
        report.fail(phase, 'cancelled', error=error)
        raise
    except Exception as error:  # noqa: BLE001 - optional display copy never discards a verified course
        reason = 'timeout' if isinstance(error, TimeoutError) else 'exception'
        report.fail(phase, reason, error=error)
        logger.warning(
            "Optional course localization unavailable: language=%s error=%s",
            request.language,
            type(error).__name__,
        )
        display.apply()
        return _finish_display(course)
    finally:
        try:
            if async_options is not None:
                try:
                    await async_options["http_client"].aclose()
                except Exception:  # noqa: BLE001 - preserve a valid itinerary or cancellation
                    logger.warning(
                        "Course localization async client cleanup unavailable"
                    )
        finally:
            if sync_options is not None:
                try:
                    sync_options["http_client"].close()
                except Exception:  # noqa: BLE001 - preserve a valid itinerary or cancellation
                    logger.warning("Course localization client cleanup unavailable")
            report.finish(report.total - len(display.pending_units()), cancelled=cancelled)

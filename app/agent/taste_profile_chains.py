"""Two sequential structured calls sharing the existing taste ABTO route."""

import logging
import random
from typing import Any
from uuid import UUID

from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from app.agent.prompts import TASTE_PROFILE_PROMPT
from app.agent.taste_summary_prompt import TASTE_SUMMARY_PROMPT
from app.core.config import settings
from app.core.llm import get_abto
from app.schemas.taste_profile import TasteProfileAnalysisOutput
from app.schemas.taste_summary import TasteSummaryDraft

logger = logging.getLogger(__name__)
SAFE_TRAVEL_HASHTAGS = (
    "#여행기록", "#공간탐색", "#취향탐색", "#여행탐색", "#장소탐색",
    "#여행발견", "#장소발견", "#공간발견", "#여행순간", "#여행노트",
    "#여행앨범", "#사진기록", "#여행장면", "#여행풍경", "#여행일지",
    "#여행지도", "#여행수첩", "#여행모음", "#여행생각", "#여행한컷",
)


def _validate_generated_output[Output: BaseModel](output_schema: type[Output], output: Any) -> Output:
    """Recover only malformed summary tags; retain validation for every other field.

    Args:
        output_schema: Expected structured model.
        output: Parsed provider JSON, without changing valid scores or copy.
    Returns:
        Validated output, sampling three distinct neutral tags only for hashtag errors.
    Raises:
        ValidationError: A non-hashtag field is invalid.
    """
    try:
        return output_schema.model_validate(output)
    except ValidationError as error:
        if output_schema is not TasteSummaryDraft or any(
            not item["loc"] or item["loc"][0] != "hashtags" for item in error.errors()
        ):
            raise
        logger.warning("Invalid generated hashtags replaced with neutral travel tags")
        return output_schema.model_validate({**output, "hashtags": random.sample(SAFE_TRAVEL_HASHTAGS, k=3)})


async def generate_taste_profile(statistics_report: str, *, user_id: UUID) -> TasteProfileAnalysisOutput:
    """Infer the original profile once from sanitized statistics.

    Args:
        statistics_report: Serialized visit statistics.
        user_id: Validated identity for existing ABTO attribution.
    Returns:
        Structured, not yet guarded profile.
    Raises:
        Exception: Configuration, provider or structured-output failures.
    """
    return await _generate_structured(
        TASTE_PROFILE_PROMPT, {"statistics_report": statistics_report}, TasteProfileAnalysisOutput, user_id,
    )


async def generate_taste_summary(summary_context: str, *, user_id: UUID) -> TasteSummaryDraft:
    """Generate UI copy and one city recommendation; scores remain server-owned.

    Args:
        summary_context: Three display axes and all supported recommendation interests as JSON.
        user_id: Same validated identity used by the analysis call.
    Returns:
        Summary draft for reference validation and final assembly.
    Raises:
        Exception: Configuration, provider or structured-output failures.
    """
    return await _generate_structured(
        TASTE_SUMMARY_PROMPT, {"summary_context": summary_context}, TasteSummaryDraft, user_id,
    )


async def _generate_structured[Output: BaseModel](
    prompt: ChatPromptTemplate, values: dict[str, str], output_schema: type[Output], user_id: UUID,
) -> Output:
    """Create a request-scoped client and release it on success, failure or cancellation.

    Args:
        prompt: Analysis or summary instructions.
        values: Serialized, sanitized prompt inputs.
        output_schema: Expected structured result.
        user_id: Validated server user identity shared with the Calling context.
    Returns:
        The structured result returned by Gemini through the existing taste route.
    Raises:
        ValueError: Provider credentials are missing.
        Exception: Provider and structured-output failures propagate to the service.
    """
    if not settings.GEMINI_API_KEY.strip():
        raise ValueError('Gemini credentials are not configured')
    abto = get_abto()
    sync_options = abto.openai_options()
    async_options = None
    try:
        async_options = abto.async_openai_options()
        model = ChatOpenAI(
            model=settings.GEMINI_MODEL_NAME,
            use_responses_api=False,
            temperature=None,
            timeout=max(10., settings.TASTE_ANALYSIS_TIMEOUT_SECONDS),
            max_retries=0,
            **sync_options,
            http_async_client=async_options['http_client'],
        )
        # Keep the full schema on the wire, then validate summary JSON here so a
        # hashtag-only error cannot discard otherwise valid analysis and copy.
        wire_schema = output_schema.model_json_schema() if output_schema is TasteSummaryDraft else output_schema
        chain = prompt | model.with_structured_output(
            wire_schema, method='json_schema', strict=False,
        )
        with abto.with_context(feature_id='taste.profile.analysis', device_id=str(user_id)):
            return _validate_generated_output(output_schema, await chain.ainvoke(values))
    finally:
        try:
            if async_options is not None:
                await async_options['http_client'].aclose()
        finally:
            sync_options['http_client'].close()

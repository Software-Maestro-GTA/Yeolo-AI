"""Single structured-output chain for the complete taste profile."""

from uuid import UUID

from langchain_openai import ChatOpenAI

from app.agent.prompts import TASTE_PROFILE_PROMPT
from app.core.config import settings
from app.core.llm import get_abto
from app.schemas.taste_profile import TasteProfileAnalysisOutput


async def generate_taste_profile(statistics_report: str, *, user_id: UUID) -> TasteProfileAnalysisOutput:
    """Create a request-scoped client and release it on success, failure or cancellation.

    Args:
        statistics_report: Sanitized visit statistics serialized as JSON.
        user_id: Validated server user identity shared with the Calling context.
    Returns:
        The structured profile returned by Gemini.
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
        chain = TASTE_PROFILE_PROMPT | model.with_structured_output(
            TasteProfileAnalysisOutput, method='json_schema', strict=False,
        )
        with abto.with_context(feature_id='taste.profile.analysis', device_id=str(user_id)):
            return await chain.ainvoke({'statistics_report': statistics_report})
    finally:
        try:
            if async_options is not None:
                await async_options['http_client'].aclose()
        finally:
            sync_options['http_client'].close()

"""Single structured-output chain for the complete taste profile."""

from langchain_google_genai import ChatGoogleGenerativeAI

from app.agent.prompts import TASTE_PROFILE_PROMPT
from app.core.config import settings
from app.schemas.taste_profile import TasteProfileAnalysisOutput


async def generate_taste_profile(statistics_report: str) -> TasteProfileAnalysisOutput:
    """Create a request-scoped client and release it on success, failure or cancellation.

    Args:
        statistics_report: Sanitized visit statistics serialized as JSON.
    Returns:
        The structured profile returned by Gemini.
    Raises:
        ValueError: Provider credentials are missing.
        Exception: Provider and structured-output failures propagate to the service.
    """
    if not settings.GEMINI_API_KEY.strip():
        raise ValueError('Gemini credentials are not configured')
    model = ChatGoogleGenerativeAI(
        model=settings.GEMINI_MODEL_NAME,
        google_api_key=settings.GEMINI_API_KEY,
        # Keep the provider deadline valid even with a shorter local deadline.
        timeout=max(10., settings.TASTE_ANALYSIS_TIMEOUT_SECONDS),
        max_retries=0,
    )
    try:
        # Suppress LangChain's candidate_count=1 default; GenAI omits None on the wire.
        chain = TASTE_PROFILE_PROMPT | model.with_structured_output(
            TasteProfileAnalysisOutput
        ).bind(generation_config={'candidate_count': None})
        return await chain.ainvoke({'statistics_report': statistics_report})
    finally:
        try:
            await model.client.aio.aclose()
        finally:
            model.client.close()

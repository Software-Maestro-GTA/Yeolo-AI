"""Initialize server-only ABTO routing for the two approved Gemini capabilities."""

from functools import lru_cache

from abto import Abto, init_abto

from app.core.config import settings


@lru_cache(maxsize=1)
def get_abto() -> Abto:
    """Return the shared Calling context, without making a network request.

    Returns:
        ABTO client using server credentials and no direct provider fallback.
    Raises:
        ValueError: Calling/Gemini credentials or the Gateway URL are invalid.

    Gemini must be selected for each feature in the ABTO dashboard. HTTP clients
    returned by the public options methods belong to each model invocation and
    are closed by its existing finally block.
    """
    if not settings.GEMINI_API_KEY.strip() or not settings.ABTO_CALLING_KEY.strip():
        raise ValueError('ABTO and Gemini credentials are required')
    return init_abto(
        api_key=settings.ABTO_CALLING_KEY,
        gateway_base_url=settings.ABTO_GATEWAY_BASE_URL,
        provider_keys={'gemini': lambda: settings.GEMINI_API_KEY},
        fallback=False,
    )

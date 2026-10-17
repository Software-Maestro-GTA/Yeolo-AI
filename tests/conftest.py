"""Keep unit tests independent of provider credentials and external networks."""

import os

import pytest

from app.core.config import settings
from app.core.llm import get_abto


@pytest.fixture(autouse=True)
def offline_calling_configuration(mocker):
    """Never use local Calling/provider credentials or cached clients in tests."""
    mocker.patch.object(settings, 'ABTO_CALLING_KEY', 'ck-abto-offline')
    mocker.patch.object(settings, 'ABTO_GATEWAY_BASE_URL', 'https://gateway.abto.app/v1')
    mocker.patch.object(settings, 'GEMINI_API_KEY', 'offline-gemini-key')
    mocker.patch.object(settings, 'MYREALTRIP_PARTNER_API_KEY', '')
    mocker.patch.object(settings, 'MYREALTRIP_ORIGIN_AIRPORT', None)
    mocker.patch.object(settings, 'MYREALTRIP_ADULTS', 1)
    mocker.patch.object(settings, 'MYREALTRIP_QUOTE_TIMEOUT_SECONDS', 8.)
    mocker.patch.object(settings, 'MYREALTRIP_MCP_URL', 'https://quotes.example.test/mcp')
    mocker.patch.object(settings, 'MYREALTRIP_PARTNER_BASE_URL', 'https://partner.example.test')
    get_abto.cache_clear()
    yield
    get_abto.cache_clear()


@pytest.fixture(autouse=True)
def forbid_live_http(mocker):
    """ASGI and MockTransport remain available; real HTTP transports must not run."""
    mocker.patch.dict(os.environ, {'LANGSMITH_TRACING': 'false', 'LANGCHAIN_TRACING_V2': 'false'})
    mocker.patch(
        'httpx.AsyncHTTPTransport.handle_async_request',
        side_effect=AssertionError('Live HTTP is forbidden in unit tests'),
    )
    mocker.patch(
        'httpx.HTTPTransport.handle_request',
        side_effect=AssertionError('Live HTTP is forbidden in unit tests'),
    )
    mocker.patch(
        'httpx2.AsyncHTTPTransport.handle_async_request',
        side_effect=AssertionError('Live HTTP is forbidden in unit tests'),
    )
    mocker.patch(
        'httpx2.HTTPTransport.handle_request',
        side_effect=AssertionError('Live HTTP is forbidden in unit tests'),
    )


@pytest.fixture(autouse=True)
def offline_trip_quotes(request, mocker):
    """Use unknown quotes for graph tests; provider wire tests keep the real client."""
    if request.module.__name__.endswith('test_myrealtrip_quotes'):
        return
    from app.services.myrealtrip_quotes import (
        MyRealTripQuoteProvider,
        QuoteItem,
        TripQuotes,
    )

    mocker.patch.object(
        MyRealTripQuoteProvider, 'quote', new_callable=mocker.AsyncMock,
        return_value=TripQuotes(QuoteItem('unavailable', reason='offline'), QuoteItem('unavailable', reason='offline')),
    )

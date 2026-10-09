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

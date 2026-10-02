"""Keep unit tests independent of provider credentials and external networks."""

import os

import pytest


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

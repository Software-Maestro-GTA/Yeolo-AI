"""Keep unit tests independent of provider credentials and external networks."""

import os
from importlib import import_module
from unittest.mock import AsyncMock

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


@pytest.fixture(autouse=True)
def offline_place_copy(mocker):
    """Keep optional finalized-place prose independent of credentials and tokens."""
    try:
        service = import_module('app.services.course_copy')
    except ModuleNotFoundError as error:
        if error.name != 'app.services.course_copy':
            raise
        yield None  # The new service is intentionally absent during TDD Red.
        return
    original = service.generate_place_copy
    boundary = mocker.patch.object(service, 'generate_place_copy', new_callable=AsyncMock, return_value={'stops': []})
    boundary.original = original
    yield boundary

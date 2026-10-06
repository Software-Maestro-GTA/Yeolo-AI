"""Bound every course request to a shared ninety-second budget."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.schemas.course import CourseRequestSchema


@pytest.fixture
def trip_request():
    return CourseRequestSchema.model_validate({
        'userId': '550e8400-e29b-41d4-a716-446655440000', 'mbti': 'INTJ',
        'tripCondition': {'destinationCountry': '일본', 'destinationCity': '도쿄',
                          'startDate': '2026-10-17', 'totalDays': 5, 'budgetType': 'moderate'},
    })


@pytest.mark.asyncio
@pytest.mark.parametrize('configured', [120, 300, 45])
async def test_request_deadline_never_exceeds_ninety_seconds(mocker, trip_request, configured):
    from app.agent.course_graph import stream_course_generation

    captured = []
    async def events(state, **kwargs):
        captured.append(state)
        yield 'custom', {'step': 'GENERATING_ROUTE', 'message': '확인 중'}
    provider = mocker.MagicMock()
    provider.__aenter__ = AsyncMock(return_value=provider)
    provider.__aexit__ = AsyncMock(return_value=False)
    mocker.patch('app.agent.course_graph.VerifiedMapsProvider', return_value=provider)
    mocker.patch('app.agent.course_graph.CourseHistory')
    mocker.patch('app.agent.course_graph.build_course_graph').return_value.astream.side_effect = events
    mocker.patch('app.agent.course_graph.settings.COURSE_TIMEOUT_SECONDS', configured)
    before = asyncio.get_running_loop().time()
    assert [event async for event in stream_course_generation(trip_request)]
    assert 0 < captured[0]['deadline'] - before <= min(90, configured) + .1

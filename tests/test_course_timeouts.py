"""Keep Gemini deadlines valid while honoring configured local generation budgets."""

import asyncio
import runpy
from collections import Counter
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai.errors import APIError
from langchain_core.runnables import RunnableLambda
from langchain_google_genai.chat_models import ChatGoogleGenerativeAIError

from app.agent import course_graph
from app.agent.course_graph import Candidate, CourseDraft, DraftDay
from app.schemas.course import CourseRequestSchema


@pytest.fixture
def request_data():
    return CourseRequestSchema.model_validate({
        'userId': '550e8400-e29b-41d4-a716-446655440000', 'mbti': 'INTJ',
        'tripCondition': {'destinationCountry': '일본', 'destinationCity': '도쿄',
                          'startDate': '2026-10-17', 'totalDays': 2, 'budgetType': 'moderate'},
    })


def day_draft(index=0):
    return CourseDraft(title='후보', reason='문화 여행', days=[DraftDay(candidates=[Candidate(name=f'{index}-미술관'), Candidate(name=f'{index}-공원')])])


async def respond_with_draft(prompt):
    return day_draft()


def wrapped_api_error(code):
    error = ChatGoogleGenerativeAIError('Provider failed')
    error.__cause__ = APIError(code, {'error': {'message': 'Provider failed'}})
    return error


def test_default_generation_budget_is_five_minutes(mocker):
    """Evaluate defaults in a separate namespace without replacing live settings."""
    import os

    original_getenv = os.getenv
    mocker.patch('dotenv.load_dotenv')
    mocker.patch('os.getenv', side_effect=lambda key, default=None: default if key == 'COURSE_TIMEOUT_SECONDS' else original_getenv(key, default))
    assert runpy.run_path('app/core/config.py')['settings'].COURSE_TIMEOUT_SECONDS == 300


@pytest.mark.asyncio
@pytest.mark.parametrize(('remaining', 'expected_local'), [(10, 10), (30, 30), (120, 60), (None, 60)])
async def test_sdk_deadline_is_fixed_and_local_budget_is_independent(mocker, request_data, remaining, expected_local):
    """The valid ten-second boundary must not become an invalid SDK deadline."""
    mocker.patch('app.agent.course_graph.asyncio.get_running_loop', return_value=SimpleNamespace(time=lambda: 100.))
    model = mocker.patch('app.agent.course_graph.ChatGoogleGenerativeAI')
    model.return_value.with_structured_output.return_value = RunnableLambda(respond_with_draft)
    timeout = mocker.spy(asyncio, 'timeout')
    result = await course_graph._draft_day_candidates(request_data, [], day_index=0, seed='test', deadline=None if remaining is None else 100 + remaining)
    assert len(result.days) == 1
    assert model.call_args.kwargs['timeout'] == 60
    assert timeout.call_args.args == (expected_local,)


@pytest.mark.asyncio
@pytest.mark.parametrize('remaining', [7, 9.999, 0])
async def test_insufficient_daily_budget_starts_no_model_request(mocker, request_data, remaining):
    mocker.patch('app.agent.course_graph.asyncio.get_running_loop', return_value=SimpleNamespace(time=lambda: 100.))
    model = mocker.patch('app.agent.course_graph.ChatGoogleGenerativeAI')
    model.return_value.with_structured_output.return_value = RunnableLambda(respond_with_draft)
    with pytest.raises(TimeoutError):
        await course_graph._draft_day_candidates(request_data, [], day_index=0, seed='test', deadline=100 + remaining)
    model.assert_not_called()


@pytest.mark.asyncio
async def test_local_timeout_cancels_inflight_call_without_short_sdk_deadline(mocker, request_data):
    closed = asyncio.Event()

    async def blocked(prompt):
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    model = mocker.patch('app.agent.course_graph.ChatGoogleGenerativeAI')
    model.return_value.with_structured_output.return_value = RunnableLambda(blocked)
    real_timeout = asyncio.timeout
    # Accelerate the local timer, leaving the requested budget observable.
    timer = mocker.patch('app.agent.course_graph.asyncio.timeout', side_effect=lambda seconds: real_timeout(.01))
    with pytest.raises(TimeoutError):
        await course_graph._draft_day_candidates(request_data, [], day_index=0, seed='test')
    assert closed.is_set()
    assert timer.call_args.args == (60,)
    assert model.call_args.kwargs['timeout'] == 60


@pytest.mark.asyncio
@pytest.mark.parametrize('code', [504, 429])
async def test_wrapped_transient_provider_error_retries_only_failed_day(mocker, request_data, code):
    calls = Counter()

    async def generate(request, recent_ids, feedback='', *, day_index, **kwargs):
        calls[day_index] += 1
        if day_index == 1 and calls[day_index] == 1:
            raise wrapped_api_error(code)
        return day_draft(day_index)

    mocker.patch('app.agent.course_graph._draft_day_candidates', side_effect=generate)
    result = await course_graph.draft_candidates(request_data, [])
    assert calls == {0: 1, 1: 2}
    assert [day.candidates[0].name for day in result.days] == ['0-미술관', '1-미술관']


@pytest.mark.asyncio
async def test_wrapped_invalid_argument_is_not_retried(mocker, request_data):
    request_data.tripCondition.totalDays = 1
    error = wrapped_api_error(400)
    generate = mocker.patch('app.agent.course_graph._draft_day_candidates', new_callable=AsyncMock, side_effect=error)
    with pytest.raises(ChatGoogleGenerativeAIError) as raised:
        await course_graph.draft_candidates(request_data, [])
    assert raised.value is error
    generate.assert_awaited_once()


def test_wrapped_error_context_and_cycles_are_classified_without_text_guessing():
    context_error = RuntimeError('opaque wrapper')
    context_error.__context__ = APIError(504, {'error': {'message': 'Deadline exceeded'}})
    assert course_graph._recoverable_draft(context_error)
    cycle = RuntimeError('504 DEADLINE_EXCEEDED is only text')
    cycle.__cause__ = cycle
    assert not course_graph._recoverable_draft(cycle)
    assert not course_graph._recoverable_draft(wrapped_api_error(401))


@pytest.mark.asyncio
async def test_retry_is_skipped_when_budget_drops_below_provider_minimum(mocker, request_data):
    request_data.tripCondition.totalDays = 1
    clock = [100.]
    mocker.patch('app.agent.course_graph.asyncio.get_running_loop', return_value=SimpleNamespace(time=lambda: clock[0]))

    async def generate(*args, **kwargs):
        clock[0] = 113.
        raise TimeoutError('First attempt consumed the remaining budget')

    generate_mock = mocker.patch('app.agent.course_graph._draft_day_candidates', side_effect=generate)
    with pytest.raises(course_graph.PartialDraftError):
        await course_graph.draft_candidates(request_data, [], deadline=120.)
    generate_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_queued_day_does_not_start_after_semaphore_wait_consumes_budget(mocker, request_data):
    request_data.tripCondition.totalDays = 6
    clock = [100.]
    mocker.patch('app.agent.course_graph.asyncio.get_running_loop', return_value=SimpleNamespace(time=lambda: clock[0]))
    model = mocker.patch('app.agent.course_graph.ChatGoogleGenerativeAI')
    entered = 0
    ready = asyncio.Event()

    async def respond(prompt):
        nonlocal entered
        entered += 1
        if entered == 5:
            clock[0] = 123.
            ready.set()
        await ready.wait()
        return day_draft(entered)

    model.return_value.with_structured_output.return_value = RunnableLambda(respond)
    with pytest.raises(course_graph.PartialDraftError) as raised:
        await course_graph.draft_candidates(request_data, [], deadline=130.)
    assert model.call_count == 5
    assert set(raised.value.days) == set(range(5))
    assert set(raised.value.errors) == {5}


@pytest.mark.asyncio
async def test_stream_honors_explicit_stricter_deadline(mocker, request_data):
    captured = []

    async def events(state, **kwargs):
        captured.append(state['deadline'])
        yield 'custom', {'step': 'GENERATING_ROUTE', 'message': '확인 중'}

    provider = mocker.MagicMock()
    provider.__aenter__ = AsyncMock(return_value=provider)
    provider.__aexit__ = AsyncMock(return_value=False)
    mocker.patch('app.agent.course_graph.VerifiedMapsProvider', return_value=provider)
    mocker.patch('app.agent.course_graph.CourseHistory')
    mocker.patch('app.agent.course_graph.build_course_graph').return_value.astream.side_effect = events
    mocker.patch('app.agent.course_graph.settings.COURSE_TIMEOUT_SECONDS', 300)
    deadline = asyncio.get_running_loop().time() + 30
    assert [event async for event in course_graph.stream_course_generation(request_data, deadline=deadline)]
    assert captured == pytest.approx([deadline - 1], abs=.001)


@pytest.mark.asyncio
async def test_service_propagates_configured_budget_before_preflight(mocker, request_data):
    from app.services.course_service import generate_course_service

    captured = []
    mocker.patch('app.services.course_service.settings.COURSE_TIMEOUT_SECONDS', 300)
    mocker.patch('app.services.course_service.settings.GEMINI_API_KEY', 'test-key')
    mocker.patch('app.services.course_service.settings.GOOGLE_MAPS_API_KEY', 'test-key')
    mocker.patch('app.services.course_service.CourseHistory').return_value.ensure_available = AsyncMock()

    async def stream(request, *, deadline):
        captured.append(deadline)
        yield 'progress', {'step': 'GENERATING_ROUTE', 'message': '확인 중'}

    mocker.patch('app.services.course_service.stream_course_generation', new=stream)
    before = asyncio.get_running_loop().time()
    source = await generate_course_service(request_data)
    assert [event async for event in source]
    assert captured[0] - before == pytest.approx(300, abs=.1)

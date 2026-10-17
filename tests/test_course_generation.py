import asyncio
import errno
import json
import sqlite3
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.main import app
from app.schemas.course import (
    CourseSchema,
    PlaceSchema,
    StopSchema,
    TransportToNextSchema,
)

TEST_API_KEY = "test_internal_secret_key"


async def successful_stream(course):
    """Represent the public graph event boundary for API-only tests."""
    yield 'progress', {'step': 'GENERATING_ROUTE', 'message': '검증 중'}
    yield 'complete', {'course': course.model_dump()}


async def failed_stream(error):
    """Raise after one event so the HTTP response has already started."""
    yield 'progress', {'step': 'GENERATING_ROUTE', 'message': '검증 중'}
    raise error



@pytest.fixture
def mock_env(mocker, tmp_path):
    mocker.patch("app.core.config.settings.COURSE_HISTORY_PATH", str(tmp_path / "history.sqlite3"))
    mocker.patch("app.core.config.settings.INTERNAL_API_KEY", TEST_API_KEY)
    mocker.patch("app.core.config.settings.GEMINI_API_KEY", "offline-gemini")
    mocker.patch("app.core.config.settings.GOOGLE_MAPS_API_KEY", "offline-maps")


@pytest.fixture
def valid_taste_profile():
    return {
        "tasteProfileId": "550e8400-e29b-41d4-a716-446655440001",
        "travelPurpose": {
            "relaxation": 4,
            "sightseeing": 3,
            "culturalExperience": 3,
            "gourmet": 5,
            "natureExploration": 4,
            "activity": 2,
            "shopping": 2,
            "festivalEvent": 1,
            "wellness": 3,
            "selfDevelopment": 1,
        },
        "travelPaceDensity": "balanced",
        "preferredLocationType": {
            "bigCity": 3,
            "smallTownAlley": 4,
            "natureHinterland": 4,
            "beachResort": 5,
            "mountainPlateau": 2,
            "historicalCity": 3,
            "themeParkResort": 1,
            "famousSpotPreferred": 3,
            "hiddenSpotPreferred": 5,
        },
        "activityPreference": {
            "viewing": 3,
            "experience": 4,
            "adventure": 2,
            "photographyVideo": 5,
            "gourmetExploration": 5,
            "nightlife": 2,
            "shopping": 2,
            "relaxation": 4,
            "localInteraction": 3,
        },
        "spendingTendency": "cost_effective",
        "companionType": "friends",
        "foodPreference": {
            "localFoodActive": 5,
            "famousRestaurantCentered": 4,
            "streetFood": 4,
            "cafeDessert": 5,
            "fineDining": 2,
            "familiarFoodPreferred": 2,
            "dietaryRestriction": 1,
            "sightseeingOverFood": 2,
        },
        "seasonalEnvironmentPreference": [
            "warm_region",
            "spring_flower_autumn_foliage",
            "off_season",
        ],
    }


@pytest.fixture
def valid_trip_condition():
    return {
        "destinationCountry": "대한민국",
        "destinationCity": "제주",
        "startDate": "2026-08-01",
        "totalDays": 2,
        "budget": 900000,
    }


@pytest.fixture
def valid_course_request_payload(valid_taste_profile, valid_trip_condition):
    return {
        "userId": "550e8400-e29b-41d4-a716-446655440000", 'language': 'ko',
        "mbti": "ENFP",
        "tasteProfile": valid_taste_profile,
        "tripCondition": valid_trip_condition,
    }


@pytest.fixture
def sample_course_schema():
    return CourseSchema.model_validate({
        "title": "제주 가성비 힐링 & 미식 여행 2일",
        "destinationCountry": "대한민국", "destinationCity": "제주",
        "startDate": "2026-08-01", "totalDays": 2, "totalCost": 15000,
        "tags": ["#제주미식", "#가성비여행", "#힐링", "#친구와함께"],
        "itinerary": {"days": [{
            "day": 1, "date": "2026-08-01", "stops": [{
                "sequence": 1, "stayMinutes": 90,
                "memo": "시장 안내도를 보고 둘러볼 구역을 먼저 정해 보세요.",
                "reason": "다양한 먹거리를 둘러보며 미식 여행을 즐길 수 있는 전통시장입니다.",
                "cost": 15000,
                "place": {"placeId": "places/dongmun", "placeName": "제주 동문시장",
                          "category": "FOOD", "latitude": 33.5126, "longitude": 126.5283},
                "transportToNext": {"type": "none", "distance": 0, "minutes": 0, "memo": None},
            }],
        }]},
    })


@pytest.mark.asyncio
@pytest.mark.parametrize('preference', ['mbti_only', 'taste_only', 'both'])
async def test_generate_course_success(mock_env, valid_course_request_payload, sample_course_schema, mocker, preference):
    """Each supported preference combination returns the complete SSE contract."""
    if preference == 'mbti_only':
        valid_course_request_payload['tasteProfile'] = None
    elif preference == 'taste_only':
        valid_course_request_payload['mbti'] = None
    mocker.patch(
        'app.services.course_service.stream_course_generation',
        side_effect=lambda request, *, deadline: successful_stream(sample_course_schema),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(
            '/internal/ai/courses', headers={'X-Internal-Api-Key': TEST_API_KEY},
            json=valid_course_request_payload,
        )
    assert response.status_code == 200
    assert 'text/event-stream' in response.headers.get('content-type', '')
    assert 'event: progress' in response.text
    assert response.text.count('event: complete') == 1
    assert 'GENERATING_ROUTE' in response.text
    assert sample_course_schema.title in response.text


@pytest.mark.asyncio
async def test_generate_course_missing_both_mbti_and_taste_profile(mock_env, valid_trip_condition):
    """
    mbti와 tasteProfile이 둘 다 누락된 요청 시 API-AI-2 400 Bad Request 에러 반환 검증
    """
    payload = {
        "userId": "550e8400-e29b-41d4-a716-446655440000", 'language': 'ko',
        "mbti": None,
        "tasteProfile": None,
        "tripCondition": valid_trip_condition,
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        response = await ac.post(
            "/internal/ai/courses",
            headers={"X-Internal-Api-Key": TEST_API_KEY},
            json=payload,
        )

    assert response.status_code == 400
    assert response.json()["status"] == 400
    assert response.json()["message"] == "코스 생성 조건이 올바르지 않습니다."


@pytest.mark.asyncio
async def test_generate_course_bad_request(mock_env):
    """
    필수 데이터 누락 또는 스키마 미충족 시 400 Bad Request 반환 검증
    """
    invalid_payload = {
        "userId": "invalid-uuid-or-missing-fields", 'language': 'ko',
    }

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        response = await ac.post(
            "/internal/ai/courses",
            headers={"X-Internal-Api-Key": TEST_API_KEY},
            json=invalid_payload,
        )

    assert response.status_code == 400
    assert response.json()["status"] == 400
    assert response.json()["message"] == "코스 생성 조건이 올바르지 않습니다."


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [
    pytest.param(ValueError('조건에 맞는 장소가 없습니다.'), id='no_places'),
    pytest.param(RuntimeError('provider unavailable'), id='provider_error'),
])
async def test_generation_error_after_stream_start_never_completes(mock_env, valid_course_request_payload, mocker, error):
    """Both place and model failures close an HTTP 200 stream without completion."""
    mocker.patch(
        'app.services.course_service.stream_course_generation',
        side_effect=lambda request, *, deadline: failed_stream(error),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(
            '/internal/ai/courses', headers={'X-Internal-Api-Key': TEST_API_KEY},
            json=valid_course_request_payload,
        )
    assert response.status_code == 200
    assert 'event: progress' in response.text
    assert 'event: complete' not in response.text


def test_stop_schema_cost_field_validation():
    """
    StopSchema에 cost(예상 비용) 필드가 올바르게 정의되고, 기본값(0) 및 양수 값 처리, 음수 거부 검증
    """
    place = PlaceSchema(
        placeId="places/test1234",
        placeName="테스트 장소",
        category="SPOT",
        latitude=37.5,
        longitude=127.0,
    )
    transport = TransportToNextSchema(
        type="walking",
        minutes=10,
    )

    # 1. cost 기본값 검증 (0)
    stop_default = StopSchema(
        sequence=1,
        stayMinutes=60,
        memo="테스트 메모",
        reason="테스트 추천 이유",
        place=place,
        transportToNext=transport,
    )
    assert stop_default.cost == 0

    # 2. 명시적 cost 지정 검증
    stop_custom_cost = StopSchema(
        sequence=2,
        stayMinutes=90,
        memo="식사 메모",
        reason="맛집 추천",
        cost=25000,
        place=place,
        transportToNext=transport,
    )
    assert stop_custom_cost.cost == 25000
    stop_dict = stop_custom_cost.model_dump()
    assert stop_dict["cost"] == 25000

    # 3. 음수 cost 거부 검증
    with pytest.raises(ValidationError):
        StopSchema(
            sequence=3,
            stayMinutes=30,
            memo="잘못된 비용 메모",
            reason="추천",
            cost=-5000,
            place=place,
            transportToNext=transport,
        )



@pytest.mark.asyncio
async def test_service_emits_progress_before_generation_finishes(mock_env, valid_course_request_payload, sample_course_schema, mocker):
    from app.schemas.course import CourseRequestSchema
    from app.services.course_service import generate_course_service

    release = asyncio.Event()

    async def blocked_generation(request, *, deadline):
        yield 'progress', {'step': 'GENERATING_ROUTE', 'message': '후보 생성 중'}
        await release.wait()
        yield 'complete', {'course': sample_course_schema.model_dump()}

    mocker.patch('app.services.course_service.stream_course_generation', side_effect=blocked_generation)
    stream = await asyncio.wait_for(generate_course_service(CourseRequestSchema.model_validate(valid_course_request_payload)), timeout=.5)
    first = await asyncio.wait_for(anext(stream), timeout=.5)
    assert 'event: progress' in first
    assert not release.is_set()
    release.set()
    events = [first, *[event async for event in stream]]
    for event in events:
        data = json.loads(event.split('data: ', 1)[1])
        if event.startswith('event: progress'):
            assert set(data) == {'step', 'message'}
            assert data['step'] == 'GENERATING_ROUTE'
        else:
            assert set(data) == {'course'}
            CourseSchema.model_validate(data['course'])
    assert sum(event.startswith('event: complete') for event in events) == 1


@pytest.mark.asyncio
async def test_service_cancellation_closes_pending_generation(mock_env, valid_course_request_payload, mocker):
    from app.schemas.course import CourseRequestSchema
    from app.services.course_service import generate_course_service

    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def pending_generation(request, *, deadline):
        try:
            yield 'progress', {'step': 'GENERATING_ROUTE', 'message': '생성 중'}
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    mocker.patch('app.services.course_service.stream_course_generation', side_effect=pending_generation)
    stream = await generate_course_service(CourseRequestSchema.model_validate(valid_course_request_payload))
    await anext(stream)
    task = asyncio.create_task(anext(stream))
    await asyncio.wait_for(entered.wait(), timeout=.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(cleaned.wait(), timeout=.5)


@pytest.mark.asyncio
async def test_service_preserves_source_timeout_across_progress_events(mock_env, valid_course_request_payload, mocker):
    """A timeout opened before progress must still cancel later generation work."""
    from app.schemas.course import CourseRequestSchema
    from app.services.course_service import generate_course_service

    cleaned = asyncio.Event()

    async def timed_generation(request, *, deadline):
        try:
            async with asyncio.timeout(.03):
                yield 'progress', {'step': 'GENERATING_ROUTE', 'message': '후보 생성 중'}
                await asyncio.Event().wait()
        finally:
            cleaned.set()

    mocker.patch('app.services.course_service.stream_course_generation', side_effect=timed_generation)
    stream = await generate_course_service(CourseRequestSchema.model_validate(valid_course_request_payload))

    async def consume():
        return [event async for event in stream]

    events = await asyncio.wait_for(consume(), timeout=.5)
    assert cleaned.is_set()
    assert len(events) >= 2
    assert all(event.startswith('event: progress') for event in events)
    assert all('event: complete' not in event for event in events)


@pytest.mark.asyncio
async def test_service_complete_allows_producer_to_finish_normally(mock_env, valid_course_request_payload, sample_course_schema, mocker):
    """Receiving complete must not cancel the producer during its normal exit."""
    from app.schemas.course import CourseRequestSchema
    from app.services.course_service import generate_course_service

    normal_exit = asyncio.Event()
    cancelled = asyncio.Event()

    async def finishing_generation(request, *, deadline):
        try:
            yield 'progress', {'step': 'GENERATING_ROUTE', 'message': '생성 중'}
            yield 'complete', {'course': sample_course_schema.model_dump()}
            await asyncio.sleep(.01)
            normal_exit.set()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    mocker.patch('app.services.course_service.stream_course_generation', side_effect=finishing_generation)
    stream = await generate_course_service(CourseRequestSchema.model_validate(valid_course_request_payload))
    events = []
    try:
        async for event in stream:
            events.append(event)
            if event.startswith('event: complete'):
                assert normal_exit.is_set()
                break
    finally:
        await stream.aclose()
    assert sum(event.startswith('event: complete') for event in events) == 1
    assert normal_exit.is_set()
    assert not cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize('storage_error', [
    PermissionError(errno.EACCES, 'Permission denied', '.data'),
    sqlite3.OperationalError('attempt to write a readonly database: /private/db'),
    sqlite3.DatabaseError('file is not a database: /private/db'),
])
async def test_unavailable_history_keeps_verified_sse_generation_available(
    mock_env, valid_course_request_payload, mocker, storage_error, sample_course_schema,
):
    """Optional history storage failure must not discard a verified course."""
    from app.services.course_history import CourseHistory

    probe = mocker.patch.object(
        CourseHistory, 'ensure_available', new_callable=AsyncMock,
        side_effect=storage_error,
    )
    generation = mocker.patch(
        'app.services.course_service.stream_course_generation',
        side_effect=lambda request, *, deadline: successful_stream(sample_course_schema),
    )
    maps_provider = mocker.patch('app.agent.course_graph.VerifiedMapsProvider')
    model = mocker.patch('app.agent.course_drafting.ChatOpenAI')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(
            '/internal/ai/courses', headers={'X-Internal-Api-Key': TEST_API_KEY},
            json=valid_course_request_payload,
        )

    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/event-stream')
    assert response.text.count('event: complete') == 1
    assert 'event: progress' in response.text
    assert '.data' not in response.text
    assert '/private/db' not in response.text
    probe.assert_awaited_once_with()
    generation.assert_called_once()
    maps_provider.assert_not_called()
    model.assert_not_called()


@pytest.mark.asyncio
async def test_history_preflight_finishes_before_stream_factory(
    mock_env, valid_course_request_payload, sample_course_schema, mocker,
):
    """Readiness is awaited before an SSE iterator or any graph can start."""
    from app.schemas.course import CourseRequestSchema
    from app.services.course_history import CourseHistory
    from app.services.course_service import generate_course_service

    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_probe():
        entered.set()
        await release.wait()

    probe = mocker.patch.object(
        CourseHistory, 'ensure_available', new_callable=AsyncMock,
        side_effect=blocked_probe,
    )
    generation = mocker.patch(
        'app.services.course_service.stream_course_generation',
        side_effect=lambda request, *, deadline: successful_stream(sample_course_schema),
    )
    task = asyncio.create_task(generate_course_service(
        CourseRequestSchema.model_validate(valid_course_request_payload),
    ))
    try:
        await asyncio.wait_for(entered.wait(), timeout=.5)
        assert not task.done()
        generation.assert_not_called()
        release.set()
        stream = await task
        events = [event async for event in stream]
        assert events[0].startswith('event: progress')
        assert events[-1].startswith('event: complete')
        probe.assert_awaited_once_with()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_case', ['unauthorized', 'calendar', 'schema'])
async def test_invalid_request_does_not_touch_history(
    mock_env, valid_course_request_payload, mocker, invalid_case,
):
    """Invalid and unauthorized requests must finish without storage access."""
    from app.services.course_history import CourseHistory

    probe = mocker.patch.object(
        CourseHistory, 'ensure_available', new_callable=AsyncMock,
        side_effect=AssertionError('Rejected request must not touch storage'),
    )
    headers = {'X-Internal-Api-Key': TEST_API_KEY}
    expected_status = 400
    if invalid_case == 'unauthorized':
        headers = {}
        expected_status = 401
    elif invalid_case == 'calendar':
        valid_course_request_payload['tripCondition']['startDate'] = '2026-02-30'
    else:
        valid_course_request_payload['tripCondition']['totalDays'] = 0
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/internal/ai/courses', headers=headers, json=valid_course_request_payload)
    assert response.status_code == expected_status
    assert response.json()['status'] == expected_status
    assert response.json()['message'] == (
        '내부 인증 실패' if expected_status == 401 else '코스 생성 조건이 올바르지 않습니다.'
    )
    probe.assert_not_awaited()

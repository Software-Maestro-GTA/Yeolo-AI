"""Offline Gateway wire tests for immutable facts and optional copy translation."""

import asyncio
import json
import re
from copy import deepcopy

import httpx
import pytest

from app.agent.tools.verified_maps import InternalPlaceSchema
from app.core.llm import get_abto
from app.schemas.course import CourseRequestSchema, CourseSchema
from app.schemas.taste_profile import TasteProfileSchema
from app.services import course_localization
from app.services.course_display_diagnostics import DisplayDiagnostics

USER_ID = "550e8400-e29b-41d4-a716-446655440000"


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ko', 'en', 'ja', 'zh', 'zh-TW', 'fr', 'es', 'de', 'pt'])
async def test_lodging_copy_stays_empty_when_localization_unavailable(language, mocker):
    """An overnight stop cannot acquire template prose or change its price."""
    value = course()
    hotel = value.itinerary.days[0].stops[-1]
    hotel.place = InternalPlaceSchema(
        placeId='verified-hotel', placeName='숙소', category='lodging',
        latitude=37.501, longitude=127.101,
    )
    hotel.cost = 47501
    hotel.reason = '未翻訳 숙소 recommendation'
    hotel.memo = 'Untranslated mixed 방문 팁'
    gateway = mocker.patch('app.services.course_localization.get_abto')
    result = await course_localization.localize_course(request(language), value, timeout_seconds=0)
    updated = result.itinerary.days[0].stops[-1]
    assert updated.memo == updated.reason == ""
    assert updated.place.placeId == 'verified-hotel' and updated.cost == 47501
    assert updated.transportToNext.type == 'none' and result.totalCost == 95000
    gateway.assert_not_called()


def request(language="ko", profile=None):
    return CourseRequestSchema.model_validate(
        {
            "userId": USER_ID,
            "language": language,
            "mbti": "INTJ",
            "tasteProfile": profile,
            "tripCondition": {
                "destinationCountry": "대한민국",
                "destinationCity": "서울",
                "startDate": "2027-01-10",
                "totalDays": 1,
                "budget": 100000,
            },
        }
    )


def course():
    return CourseSchema.model_validate(
        {
            "title": "서울 1일 여행",
            "destinationCountry": "대한민국",
            "destinationCity": "서울",
            "startDate": "2027-01-10",
            "totalDays": 1,
            "totalCost": 95000,
            "tags": ["문화 탐방", "자연 산책"],
            "itinerary": {
                "days": [
                    {
                        "day": 1,
                        "date": "2027-01-10",
                        "stops": [
                            {
                                "sequence": 1,
                                "stayMinutes": 90,
                                "memo": "현장 관람 안내를 확인하고 전시를 살펴보세요.",
                                "reason": "검증된 박물관에서 전시를 둘러보는 경험을 추천했어요.",
                                "cost": 7000,
                                "place": InternalPlaceSchema(
                                    placeId="verified-museum",
                                    placeName="검증된 박물관",
                                    category="museum",
                                    latitude=37.5,
                                    longitude=127.1,
                                ),
                                "transportToNext": {
                                    "type": "walking",
                                    "distance": 700,
                                    "minutes": 12,
                                    "memo": "Follow the pedestrian route.",
                                },
                            },
                            {
                                "sequence": 2,
                                "stayMinutes": 60,
                                "memo": "현장 안내를 보고 주변을 둘러보세요.",
                                "reason": "검증된 공원에서 자연을 둘러보는 경험을 추천했어요.",
                                "cost": 0,
                                "place": InternalPlaceSchema(
                                    placeId="verified-park",
                                    placeName="검증된 공원",
                                    category="park",
                                    latitude=37.501,
                                    longitude=127.101,
                                ),
                                "transportToNext": {
                                    "type": "none",
                                    "distance": None,
                                    "minutes": None,
                                    "memo": None,
                                },
                            },
                        ],
                    }
                ]
            },
        }
    )


def facts(value):
    output = deepcopy(value.model_dump())
    output.pop("title")
    output.pop("tags")
    for day in output["itinerary"]["days"]:
        for stop in day["stops"]:
            stop.pop("reason")
            stop.pop("memo")
            stop["place"].pop("placeName")  # A translated label is not an identity.
            stop["transportToNext"].pop("memo")
    return output


def completion(output):
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-copy",
            "object": "chat.completion",
            "created": 0,
            "model": "gemini-3.8-flash",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": json.dumps(output)},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        },
    )


@pytest.fixture
def wire(mocker):
    clients = []
    abto = get_abto()
    for name in ("openai_options", "async_openai_options"):
        original = getattr(abto, name)

        def record(*args, _original=original, **kwargs):
            options = _original(*args, **kwargs)
            clients.append(options["http_client"])
            return options

        mocker.patch.object(abto, name, side_effect=record)

    def install(respond):
        mocker.patch(
            "httpx._client.HTTPTransport",
            side_effect=lambda **kwargs: httpx.MockTransport(
                lambda _: pytest.fail("Async localization must not send sync HTTP"),
            ),
        )
        mocker.patch(
            "httpx._client.AsyncHTTPTransport",
            side_effect=lambda **kwargs: httpx.MockTransport(respond),
        )
        return clients

    return install


@pytest.mark.asyncio
@pytest.mark.parametrize('legacy_language', ['ko', 'en-US', 'ja', 'zh-TW'])
async def test_real_gateway_wire_is_korean_only_and_preserves_schema_identity_deadline_cleanup(wire, legacy_language):
    calls = []
    def respond(outgoing):
        calls.append(outgoing)
        return completion({'texts': [{'id': 'd0', 'text': '서울 박물관'}]})
    clients = wire(respond)
    value = course()
    value.itinerary.days[0].stops[0].place.placeName = "Seoul Museum"
    before = value.model_dump()
    immutable = facts(value)
    result = await course_localization.localize_course(request(legacy_language), value)
    assert result is value and facts(result) == immutable
    assert result.title == before['title'] and result.tags == before['tags']
    assert result.itinerary.days[0].stops[0].place.placeName == '서울 박물관'
    assert result.itinerary.days[0].stops[0].transportToNext.memo == 'Follow the pedestrian route.'
    assert len(calls) == 1
    outgoing = calls[0]
    assert str(outgoing.url) == 'https://gateway.abto.app/v1/chat/completions'
    assert outgoing.headers['x-abto-feature-id'] == 'course.candidates.generate'
    assert outgoing.headers['x-abto-device-id'] == USER_ID
    assert outgoing.headers['authorization'] == 'Bearer ck-abto-offline'
    assert outgoing.headers['x-abto-key-gemini'] == 'offline-gemini-key'
    assert outgoing.extensions['timeout']['read'] == 60
    body = json.loads(outgoing.content)
    assert body['max_completion_tokens'] == 6000 and body['reasoning_effort'] == 'low'
    assert body['response_format']['type'] == 'json_schema'
    assert body['response_format']['json_schema']['strict'] is False
    schema = body['response_format']['json_schema']['schema']
    assert set(schema['properties']) == {'texts'}
    assert set(schema['$defs']['DisplayText']['properties']) == {'id', 'text'}
    assert 'temperature' not in body and 'tools' not in body
    source = body['messages'][-1]['content']
    assert 'Target language: Korean (ko)' in source
    assert 'latitude' not in source and 'totalCost' not in source
    assert len(clients) == 2 and all(client.is_closed for client in clients)
    assert get_abto().get_headers() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["ko", "ko-KR"])
async def test_korean_is_returned_untouched_without_optional_model(language, mocker):
    constructor = mocker.patch.object(
        course_localization,
        "get_abto",
        side_effect=AssertionError("No optional Korean model"),
    )
    value = course()
    value.itinerary.days[0].stops[0].transportToNext.memo = "보행 경로를 따라 이동하세요."
    before = value.model_dump()
    result = await course_localization.localize_course(request(language), value)
    assert result is value and result.model_dump() == before
    constructor.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "language,marker",
    [
        ("en-US", "Start"),
        ("ja", "現地"),
        ("zh-CN", "现场"),
        ("zh-TW", "現場"),
        ("fr", "Consultez"),
        ("es", "Consulta"),
        ("de", "Lesen"),
        ("pt-BR", "Consulte"),
    ],
)
async def test_optional_model_outage_does_not_create_visit_templates(
    language, marker, wire, caplog
):
    calls = []

    def respond(http_request):
        calls.append(http_request)
        return httpx.Response(
            503,
            json={
                "error": {"message": "private server failure", "type": "server_error"}
            },
        )

    clients = wire(respond)
    value = course()
    value.itinerary.days[0].stops[0].place.placeName = "Seoul Museum"
    before = facts(value)
    result = await course_localization.localize_course(request(language), value)
    assert facts(result) == before
    assert [(stop.reason, stop.memo) for day in result.itinerary.days for stop in day.stops] == [(stop.reason, stop.memo) for day in course().itinerary.days for stop in day.stops]
    assert (
        len(calls) == 1
        and len(clients) == 2
        and all(client.is_closed for client in clients)
    )
    assert "private server failure" not in caplog.text


@pytest.mark.asyncio
async def test_strong_actual_preferences_only_and_low_budget_skip_model(mocker):
    constructor = mocker.patch.object(
        course_localization,
        "get_abto",
        side_effect=AssertionError("Insufficient budget"),
    )
    fields = {
        section: dict.fromkeys(
            TasteProfileSchema.model_fields[section].annotation.model_fields, 2
        )
        for section in (
            "travelPurpose",
            "preferredLocationType",
            "activityPreference",
            "foodPreference",
        )
    }
    fields.update(
        travelPaceDensity="balanced",
        spendingTendency="moderate",
        companionType="solo",
        seasonalEnvironmentPreference=["warm_region"],
    )
    fields["travelPurpose"]["culturalExperience"] = 5
    value = course()
    value.itinerary.days[0].stops[0].place.placeName = "Seoul Museum"
    result = await course_localization.localize_course(
        request("en", fields), value, timeout_seconds=0.1
    )
    museum, park = result.itinerary.days[0].stops
    assert (museum.reason, museum.memo, park.reason, park.memo) == tuple(text for stop in course().itinerary.days[0].stops for text in (stop.reason, stop.memo))
    constructor.assert_not_called()


@pytest.mark.asyncio
async def test_localization_cancellation_propagates_and_closes_gateway_clients(wire):
    entered = asyncio.Event()

    async def respond(http_request):
        entered.set()
        await asyncio.Event().wait()

    clients = wire(respond)
    value = course()
    value.itinerary.days[0].stops[0].place.placeName = "Seoul Museum"
    before = value.model_dump()
    report = DisplayDiagnostics('translation')
    task = asyncio.create_task(course_localization.localize_course(request(), value, diagnostics=report))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert value.model_dump() == before
    assert len(clients) == 2 and all(client.is_closed for client in clients)
    assert report.status == 'cancelled'
    assert any(item['phase'] == 'display_translation' and item['reason'] == 'cancelled' for item in report.failures)


@pytest.mark.asyncio
async def test_optional_local_timer_returns_localized_course_instead_of_failure(wire):
    async def respond(http_request):
        await asyncio.Event().wait()

    clients = wire(respond)
    value = course()
    value.itinerary.days[0].stops[0].place.placeName = 'Seoul Museum'
    report = DisplayDiagnostics('translation')
    result = await asyncio.wait_for(
        course_localization.localize_course(request(), value, timeout_seconds=0.2, diagnostics=report),
        timeout=2,
    )
    assert result.itinerary.days[0].stops[0].reason == course().itinerary.days[0].stops[0].reason
    assert len(clients) == 2 and all(client.is_closed for client in clients)
    assert report.status == 'failed'
    assert any(item['phase'] == 'display_translation' and item['reason'] == 'timeout' for item in report.failures)


@pytest.mark.parametrize(
    "language", ["en", "ja", "zh-CN", "zh-TW", "fr", "es", "de", "pt"]
)
def test_stage_keys_and_quote_budget_notices_are_always_korean(language):
    stage = course_localization.progress_message(
        language, "verify_places"
    )
    refill = course_localization.progress_message(language, "refill")
    quote = course_localization.progress_message(
        language, "quote_estimated", items=("transport", "lodging")
    )
    budget = course_localization.progress_message(
        language, "budget_over", total=120000, budget=100000
    )
    assert all(re.search(r"[가-힣]", message) for message in (stage, refill, quote, budget))
    assert "120,000" in budget and "100,000" in budget
    assert "{items}" not in quote
    assert quote != 'quote_estimated'


@pytest.mark.asyncio
async def test_other_bcp47_locale_uses_korean_without_foreign_fallback(
    wire, caplog
):
    calls = []

    def respond(http_request):
        calls.append(http_request)
        return httpx.Response(
            503, json={"error": {"message": "private failure", "type": "server_error"}}
        )

    wire(respond)
    value = course()
    value.itinerary.days[0].stops[0].place.placeName = "Seoul Museum"
    result = await course_localization.localize_course(request("vi-VN"), value)
    assert (
        "Target language: Korean (ko)"
        in json.loads(calls[0].content)["messages"][-1]["content"]
    )
    assert result.itinerary.days[0].stops[0].reason == course().itinerary.days[0].stops[0].reason
    assert "English for unsupported locale" not in caplog.text

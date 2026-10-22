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


def request(language="en", profile=None):
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


def translated():
    return {
        "title": "서울: a 1-day trip",
        "tags": ["Culture", "Nature"],
        "stops": [
            {
                "day": 1,
                "sequence": 1,
                "placeId": "verified-museum",
                "reason": "Visit 검증된 박물관 for an exhibition experience.",
                "memo": "Read the visitor guidance before exploring the displays.",
            },
            {
                "day": 1,
                "sequence": 2,
                "placeId": "verified-park",
                "reason": "Visit 검증된 공원 for a chance to explore nature.",
                "memo": "Follow the on-site guidance and explore the surroundings.",
            },
        ],
        "navigation": [
            {"id": "n0", "text": "Follow the pedestrian route."},
            {"id": "n1", "text": "Verified Museum"},
            {"id": "n2", "text": "Verified Park"},
        ],
    }


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
async def test_real_gateway_wire_copy_schema_identity_deadline_and_owned_client_cleanup(
    wire,
):
    calls = []

    def respond(http_request):
        calls.append(http_request)
        return completion(translated())

    clients = wire(respond)
    value = course()
    before = facts(value)
    result = await course_localization.localize_course(request(), value)
    assert (
        result is value
        and result.title == translated()["title"]
        and result.tags == ["Culture", "Nature"]
    )
    assert facts(result) == before
    assert result.itinerary.days[0].stops[0].transportToNext.memo == "Follow the pedestrian route."
    assert (
        result.itinerary.days[0].stops[0].reason == translated()["stops"][0]["reason"].replace("검증된 박물관", "Verified Museum")
    )
    assert len(calls) == 1
    http_request = calls[0]
    assert str(http_request.url) == "https://gateway.abto.app/v1/chat/completions"
    assert http_request.headers["x-abto-feature-id"] == "course.candidates.generate"
    assert http_request.headers["x-abto-device-id"] == USER_ID
    assert http_request.headers["authorization"] == "Bearer ck-abto-offline"
    assert http_request.headers["x-abto-key-gemini"] == "offline-gemini-key"
    assert http_request.extensions["timeout"]["read"] == 60
    payload = json.loads(http_request.content)
    assert (
        payload["max_completion_tokens"] == 10000
        and payload["reasoning_effort"] == "low"
    )
    assert payload["response_format"]["type"] == "json_schema"
    assert payload["response_format"]["json_schema"]["strict"] is False
    assert "temperature" not in payload and "tools" not in payload
    schema = payload["response_format"]["json_schema"]["schema"]
    assert set(schema["properties"]) == {"title", "tags", "stops", "navigation"}
    assert result.itinerary.days[0].stops[0].place.placeName == "Verified Museum"
    assert not {"latitude", "cost", "stayMinutes", "transportToNext"} & set(
        schema["$defs"]["LocalizedStopCopy"]["properties"]
    )
    source = payload["messages"][-1]["content"]
    assert "Target language: en" in source and "verified-museum" in source
    assert "latitude" not in source and "totalCost" not in source
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
    "damage",
    [
        "immutable_fields",
        "missing_stop",
        "duplicate_stop",
        "wrong_place",
        "blank",
        "new_fact",
        "changed_name",
        "korean_prose",
    ],
)
async def test_hostile_or_incomplete_translation_falls_back_without_changing_verified_facts(
    damage, wire
):
    output = translated()
    if damage == "immutable_fields":
        output["stops"][0].update(cost=1, latitude=0, place={"placeId": "invented"})
    elif damage == "missing_stop":
        output["stops"].pop()
    elif damage == "duplicate_stop":
        output["stops"][1] = dict(output["stops"][0])
    elif damage == "wrong_place":
        output["stops"][0]["placeId"] = "invented"
    elif damage == "blank":
        output["stops"][0]["reason"] = "   "
    elif damage == "new_fact":
        output["stops"][0]["memo"] = "Open 24 hours. Admission costs 999 won."
    elif damage == "changed_name":
        output["stops"][0]["reason"] = (
            "Visit an invented museum for an exhibition experience."
        )
    else:
        output["stops"][0]["reason"] = "검증된 박물관에서 관람해 보세요."
    clients = wire(lambda _: completion(output))
    value = course()
    before = facts(value)
    result = await course_localization.localize_course(request(), value)
    assert result is value and facts(result) == before
    assert result.itinerary.days[0].stops[0].reason != output["stops"][0]["reason"]
    assert result.itinerary.days[0].stops[0].memo == result.itinerary.days[0].stops[0].reason == ""
    assert len(clients) == 2 and all(client.is_closed for client in clients)


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
    before = facts(value)
    result = await course_localization.localize_course(request(language), value)
    assert facts(result) == before
    assert all(stop.memo == stop.reason == "" for day in result.itinerary.days for stop in day.stops)
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
    result = await course_localization.localize_course(
        request("en", fields), value, timeout_seconds=0.1
    )
    museum, park = result.itinerary.days[0].stops
    assert museum.reason == museum.memo == park.reason == park.memo == ""
    constructor.assert_not_called()


@pytest.mark.asyncio
async def test_korean_mixed_navigation_preserves_source_when_translation_is_unavailable(mocker):
    constructor = mocker.patch.object(
        course_localization, "get_abto", side_effect=ValueError("Offline translation unavailable")
    )
    value = course()
    route = value.itinerary.days[0].stops[0].transportToNext
    route.type = "transit"
    route.memo = "Số 131 Hoàng Diệu에서 07번을 타고 Đối Diện 18-20에서 내리세요."
    before = facts(value)

    result = await course_localization.localize_course(request("ko-KR"), value)

    assert result is value and facts(result) == before
    assert route.memo == "Số 131 Hoàng Diệu에서 07번을 타고 Đối Diện 18-20에서 내리세요."
    assert result.itinerary.days[0].stops[1].transportToNext.memo is None
    constructor.assert_called_once()


@pytest.mark.asyncio
async def test_foreign_and_legacy_mixed_navigation_use_requested_language(
    mocker,
):
    mocker.patch.object(
        course_localization, "get_abto", side_effect=ValueError("Missing optional key")
    )
    value = course()
    value.itinerary.days[0].stops[
        0
    ].transportToNext.memo = "Follow the pedestrian route to 검증된 공원."
    value.itinerary.days[0].stops[
        1
    ].transportToNext.memo = "주요 이동: 안내를 따라 이동하세요."
    result = await course_localization.localize_course(request("ja"), value)
    assert result.itinerary.days[0].stops[0].transportToNext.memo == "Follow the pedestrian route to 검증된 공원."
    assert result.itinerary.days[0].stops[1].transportToNext.memo is None


@pytest.mark.asyncio
async def test_localization_cancellation_propagates_and_closes_gateway_clients(wire):
    entered = asyncio.Event()

    async def respond(http_request):
        entered.set()
        await asyncio.Event().wait()

    clients = wire(respond)
    value = course()
    before = value.model_dump()
    task = asyncio.create_task(course_localization.localize_course(request(), value))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert value.model_dump() == before
    assert len(clients) == 2 and all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_optional_local_timer_returns_localized_course_instead_of_failure(wire):
    async def respond(http_request):
        await asyncio.Event().wait()

    clients = wire(respond)
    result = await asyncio.wait_for(
        course_localization.localize_course(request(), course(), timeout_seconds=0.2),
        timeout=2,
    )
    assert result.itinerary.days[0].stops[0].memo == result.itinerary.days[0].stops[0].reason == ""
    assert len(clients) == 2 and all(client.is_closed for client in clients)


@pytest.mark.parametrize(
    "language", ["en", "ja", "zh-CN", "zh-TW", "fr", "es", "de", "pt"]
)
def test_stage_keys_and_quote_budget_notices_are_localized(language):
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
    assert not any(
        re.search(r"[가-힣]", message) for message in (stage, refill, quote, budget)
    )
    assert "120,000" in budget and "100,000" in budget
    assert "{items}" not in quote
    assert quote != 'quote_estimated'


@pytest.mark.asyncio
async def test_other_bcp47_locale_still_attempts_requested_language_then_logs_english_fallback(
    wire, caplog
):
    calls = []

    def respond(http_request):
        calls.append(http_request)
        return httpx.Response(
            503, json={"error": {"message": "private failure", "type": "server_error"}}
        )

    wire(respond)
    result = await course_localization.localize_course(request("vi-VN"), course())
    assert (
        "Target language: vi-VN"
        in json.loads(calls[0].content)["messages"][-1]["content"]
    )
    assert result.itinerary.days[0].stops[0].memo == result.itinerary.days[0].stops[0].reason == ""
    assert "English for unsupported locale: vi-VN" in caplog.text

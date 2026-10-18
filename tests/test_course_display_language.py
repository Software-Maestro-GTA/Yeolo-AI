"""Verify display-language isolation without extra calls or itinerary mutations."""

import json

import httpx
import pytest

from app.agent.tools.verified_maps import InternalTransportSchema
from app.services import course_localization
from tests.test_course_localization import (
    completion,
    course,
    facts,
    request,
    translated,
)
from tests.test_course_localization import wire as _wire

wire = _wire


def source_copy(outgoing):
    """Read the real SDK request, excluding prompts and transport metadata."""
    payload = json.loads(outgoing.content)
    return json.loads(
        payload["messages"][-1]["content"].split("Verified source copy:\n")[1]
    )


@pytest.mark.asyncio
async def test_korean_structured_navigation_skips_optional_llm_when_display_is_localized(
    mocker,
):
    constructor = mocker.patch.object(
        course_localization,
        "get_abto",
        side_effect=AssertionError("Localized display needs no optional LLM call"),
    )
    value = course()
    value.itinerary.days[0].stops[0].transportToNext = InternalTransportSchema(
        type="transit",
        distance=700,
        minutes=12,
        memo="박물관 정류장에서 B96을 타고 공원 정류장에서 내리세요.",
        navigationSource=[
            {
                "kind": "transit",
                "start": "박물관 정류장",
                "end": "공원 정류장",
                "line": "B96",
                "direction": "",
            }
        ],
    )
    before = value.model_dump()

    result = await course_localization.localize_course(request("ko"), value)

    assert result is value and result.model_dump() == before
    constructor.assert_not_called()


@pytest.mark.asyncio
async def test_localized_structured_route_with_spaced_units_skips_model_setup(mocker):
    constructor = mocker.patch.object(
        course_localization,
        "get_abto",
        side_effect=AssertionError("Measured units are not foreign-language prose"),
    )
    value = course()
    memo = "왼쪽 33 m에 있는 박물관을 지나 1.2 km를 더 걸으세요."
    value.itinerary.days[0].stops[0].transportToNext = InternalTransportSchema(
        type="walking",
        distance=1233,
        minutes=20,
        memo=memo,
        navigationSource=[{"kind": "instruction", "text": memo}],
    )
    before = value.model_dump()

    result = await course_localization.localize_course(request("ko"), value)

    assert result is value and result.model_dump() == before
    constructor.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_structured_action_translates_complete_legacy_memo(wire):
    calls = []
    original = "雁荡路에서 南昌路 방면 북쪽으로 걷기. 우회전하여 南昌路에 진입."
    localized = "옌당루에서 난창루 방면 북쪽으로 걷다가 우회전해 난창루로 들어서세요."

    def respond(outgoing):
        calls.append(outgoing)
        source = source_copy(outgoing)
        assert len(source["navigation"]) == 1
        unit = source["navigation"][0]
        assert unit["kind"] == "instruction" and unit["text"] == original
        return completion(
            {"navigation": [{"id": unit["id"], "text": localized}]}
        )

    clients = wire(respond)
    value = course()
    value.itinerary.days[0].stops[0].transportToNext = InternalTransportSchema(
        type="walking",
        distance=700,
        minutes=12,
        memo=original,
        navigationSource=[{"kind": "transit", "start": "도중 정류장", "line": "B96"}],
    )
    before = facts(value)

    result = await course_localization.localize_course(request("ko"), value)

    assert result.itinerary.days[0].stops[0].transportToNext.memo == localized
    assert result is value and facts(result) == before
    assert len(calls) == 1 and all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_korean_foreign_title_tags_and_reason_share_one_existing_copy_call(wire):
    calls = []
    expected = {
        "서울 Quiet gallery 산책": "서울의 고요한 전시 공간 산책",
        "문화 Art": "문화 예술",
        "검증된 박물관에서 contemporary art를 둘러보세요.": (
            "검증된 박물관에서 현대 미술을 둘러보세요."
        ),
    }

    def respond(outgoing):
        calls.append(outgoing)
        source = source_copy(outgoing)
        assert set(source) == {"navigation"}
        assert {unit["text"] for unit in source["navigation"]} == set(expected)
        return completion(
            {
                "navigation": [
                    {"id": unit["id"], "text": expected[unit["text"]]}
                    for unit in source["navigation"]
                ]
            }
        )

    clients = wire(respond)
    value = course()
    first = value.itinerary.days[0].stops[0]
    first.transportToNext.memo = "보행 경로를 따라 이동하세요."
    value.title = "서울 Quiet gallery 산책"
    value.tags = ["문화 Art", "자연 산책"]
    first.reason = "검증된 박물관에서 contemporary art를 둘러보세요."
    before = facts(value)

    result = await course_localization.localize_course(request("ko"), value)

    assert result.title == "서울의 고요한 전시 공간 산책"
    assert result.tags == ["문화 예술", "자연 산책"]
    assert first.reason == "검증된 박물관에서 현대 미술을 둘러보세요."
    assert facts(result) == before
    assert len(calls) == 1 and all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_known_foreign_place_name_is_translated_once_for_all_display_references(wire):
    calls = []

    def respond(outgoing):
        calls.append(outgoing)
        source = source_copy(outgoing)
        assert len(source["navigation"]) == 1
        name = source["navigation"][0]
        assert name["kind"] == "name" and name["text"] == "Seoul Museum"
        return completion(
            {"navigation": [{"id": name["id"], "text": "서울 박물관"}]}
        )

    clients = wire(respond)
    value = course()
    first = value.itinerary.days[0].stops[0]
    first.place.placeName = "Seoul Museum"
    first.reason = "Seoul Museum에서 전시를 둘러보세요."
    first.memo = "Seoul Museum의 현장 안내를 살펴보세요."
    first.transportToNext.memo = "보행 경로를 따라 이동하세요."
    value.title = "Seoul Museum에서 즐기는 산책"
    value.tags = ["Seoul Museum", "자연 산책"]
    before = facts(value)

    result = await course_localization.localize_course(request("ko"), value)

    assert first.place.placeName == "서울 박물관"
    assert first.reason == "서울 박물관에서 전시를 둘러보세요."
    assert first.memo == "서울 박물관의 현장 안내를 살펴보세요."
    assert result.title == "서울 박물관에서 즐기는 산책"
    assert result.tags == ["서울 박물관", "자연 산책"]
    assert "Seoul Museum" not in result.model_dump_json()
    assert facts(result) == before
    assert len(calls) == 1 and all(client.is_closed for client in clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_text", ["", 123])
async def test_invalid_peer_schema_keeps_valid_name_and_memo_when_repair_fails(
    wire, invalid_text
):
    calls = []
    pending = None

    def respond(outgoing):
        nonlocal pending
        calls.append(outgoing)
        source = source_copy(outgoing)
        if len(calls) > 1:
            assert source["navigation"] == [pending]
            return httpx.Response(503, json={"error": {"message": "offline failure"}})
        output = []
        for unit in source["navigation"]:
            if unit["text"] == "Tokyo Park":
                pending = unit
                text = invalid_text
            elif unit["text"] == "Tokyo Museum":
                text = "도쿄 박물관"
            else:
                assert unit["text"] == "劇場通りで右折してください。"
                text = "게키조도리에서 우회전하세요."
            output.append({"id": unit["id"], "text": text})
        return completion({"navigation": output})

    clients = wire(respond)
    value = course()
    first, second = value.itinerary.days[0].stops
    first.place.placeName = "Tokyo Museum"
    second.place.placeName = "Tokyo Park"
    first.transportToNext.memo = "劇場通りで右折してください。"
    before = facts(value)

    result = await course_localization.localize_course(request("ko"), value)

    assert first.place.placeName == "도쿄 박물관"
    assert first.transportToNext.memo == "게키조도리에서 우회전하세요."
    assert result is value and facts(result) == before
    assert len(calls) == 2 and all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_invalid_non_korean_course_copy_keeps_valid_names_and_navigation(wire):
    calls = []

    def respond(outgoing):
        calls.append(outgoing)
        output = translated()
        output["title"] = ""
        output["navigation"][0]["text"] = "Follow the pedestrian path."
        return completion(output)

    clients = wire(respond)
    value = course()
    before = facts(value)

    result = await course_localization.localize_course(request("en"), value)

    first, second = result.itinerary.days[0].stops
    assert first.place.placeName == "Verified Museum"
    assert second.place.placeName == "Verified Park"
    assert first.transportToNext.memo == "Follow the pedestrian path."
    assert result is value and facts(result) == before
    assert len(calls) == 1 and all(client.is_closed for client in clients)


@pytest.mark.parametrize(
    ("language", "copy"),
    [
        ("en", "Walk along 黄陂南路 toward the park."),
        ("en", "Enjoy the park and посетите the gallery."),
        ("ja", "公園でゆっくり散策し、Turn leftしてください。"),
        ("zh", "沿着道路向前走り、左转。"),
    ],
)
def test_course_copy_rejects_foreign_script_outside_preserved_place_names(language, copy):
    with pytest.raises(ValueError):
        course_localization._validate_text(copy, copy, [], language)


@pytest.mark.parametrize(
    ("language", "copy", "name"),
    [
        ("en", "Visit 검증된 박물관 and explore the exhibits.", "검증된 박물관"),
        ("ja", "Café Lumièreの展示をお楽しみください。", "Café Lumière"),
        ("zh", "在Café Lumière欣赏展览。", "Café Lumière"),
    ],
)
def test_course_copy_allows_only_exact_preserved_place_names(language, copy, name):
    assert course_localization._validate_text(copy, copy, [name], language) == copy

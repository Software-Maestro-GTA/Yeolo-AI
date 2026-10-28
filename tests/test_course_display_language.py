"""Verify display-language isolation without extra calls or itinerary mutations."""

import json

import httpx
import pytest

from app.services import course_localization
from app.services.course_display_copy import DisplayCopyPlan, DisplayText
from tests.test_course_localization import (
    completion,
    course,
    facts,
    request,
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
        assert set(source) == {"texts"}
        assert {unit["text"] for unit in source["texts"]} == set(expected)
        return completion(
            {
                "texts": [
                    {"id": unit["id"], "text": expected[unit["text"]]}
                    for unit in source["texts"]
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
        assert len(source["texts"]) == 1
        name = source["texts"][0]
        assert name["kind"] == "name" and name["text"] == "Seoul Museum"
        return completion(
            {"texts": [{"id": name["id"], "text": "서울 박물관"}]}
        )

    clients = wire(respond)
    value = course()
    first = value.itinerary.days[0].stops[0]
    first.place.placeName = "Seoul Museum"
    first.reason = "Seoul Museum에서 전시를 둘러보세요."
    first.memo = "Seoul Museum의 현장 안내를 살펴보세요."
    original_memo = first.transportToNext.memo = "Seoul Museum → 菸廠路 (276)"
    value.title = "Seoul Museum에서 즐기는 산책"
    value.tags = ["Seoul Museum", "자연 산책"]
    before = facts(value)

    result = await course_localization.localize_course(request("ko"), value)

    assert first.place.placeName == "서울 박물관"
    assert first.reason == "서울 박물관에서 전시를 둘러보세요."
    assert first.memo == "서울 박물관의 현장 안내를 살펴보세요."
    assert result.title == "서울 박물관에서 즐기는 산책"
    assert result.tags == ["서울 박물관", "자연 산책"]
    assert first.transportToNext.memo == original_memo
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
            assert source["texts"] == [pending]
            return httpx.Response(503, json={"error": {"message": "offline failure"}})
        output = []
        for unit in source["texts"]:
            if unit["text"] == "Tokyo Park":
                pending = unit
                text = invalid_text
            elif unit["text"] == "Tokyo Museum":
                text = "도쿄 박물관"
            else:
                pytest.fail(f"Unexpected display slot: {unit}")
            output.append({"id": unit["id"], "text": text})
        return completion({"texts": output})

    clients = wire(respond)
    value = course()
    first, second = value.itinerary.days[0].stops
    first.place.placeName = "Tokyo Museum"
    second.place.placeName = "Tokyo Park"
    first.transportToNext.memo = "劇場通りで右折してください。"
    before = facts(value)

    result = await course_localization.localize_course(request("ko"), value)

    assert first.place.placeName == "도쿄 박물관"
    assert first.transportToNext.memo == "劇場通りで右折してください。"
    assert result is value and facts(result) == before
    assert len(calls) == 2 and all(client.is_closed for client in clients)


@pytest.mark.asyncio
@pytest.mark.parametrize('original', [
    '長安東路二段에서 遼寧街 방면 서쪽으로 걷기. Zhonglun에서 276번(Jiuzhuang 방면)을 타고 내리세요.',
    '菸廠路에서 市民大道五段14巷 방면 서쪽으로 걷기 목적지는 길의 오른쪽에 있습니다.',
    'Take JR from Shinjuku (Platform W) to Tokyo.',
])
async def test_foreign_route_memo_alone_never_initializes_translation(original, mocker):
    value = course()
    route = value.itinerary.days[0].stops[0].transportToNext
    route.memo = original
    gateway = mocker.patch.object(course_localization, 'get_abto', side_effect=AssertionError('Route guidance must not reach an LLM'))
    before = value.model_dump()
    result = await course_localization.localize_course(request(), value)
    assert result.model_dump() == before and route.memo == original
    gateway.assert_not_called()


@pytest.mark.parametrize('invalid', [
    '호텔 [[F0]]의 2층', 'Hotel [[F0]]', '호텔 99', '호텔 [[F1]]',
])
def test_display_name_validation_preserves_source_numbers_and_language(invalid):
    value = course()
    place = value.itinerary.days[0].stops[0].place
    place.placeName = 'Hotel 17'
    plan = DisplayCopyPlan(value)
    assert plan.units == [{'id': 'd0', 'kind': 'name', 'text': 'Hotel [[F0]]'}]
    plan.apply([DisplayText(id='d0', text=invalid)])
    assert place.placeName == 'Hotel 17' and len(plan.pending_units()) == 1
    plan.apply([DisplayText(id='d0', text='호텔 [[F0]]')])
    assert place.placeName == '호텔 17' and plan.pending_units() == []


def test_duplicate_display_slots_do_not_discard_a_valid_peer_or_overwrite_approved_text():
    value = course()
    first, second = value.itinerary.days[0].stops
    first.place.placeName, second.place.placeName = 'Museum', 'Park'
    plan = DisplayCopyPlan(value)
    plan.apply(plan.response_texts({'texts': [
        {'id': 'd0', 'text': '박물관'}, {'id': 'd0', 'text': '다른 이름'}, {'id': 'd1', 'text': '공원'},
    ]}))
    assert first.place.placeName == 'Museum' and second.place.placeName == '공원'
    assert [unit['id'] for unit in plan.pending_units()] == ['d0']
    plan.apply([DisplayText(id='d0', text='박물관'), DisplayText(id='d1', text='다른 공원')])
    assert first.place.placeName == '박물관' and second.place.placeName == '공원'

"""Exercise grounded copy through the real LangChain/SDK wire without networking."""

import asyncio
import json
from copy import deepcopy

import httpx
import pytest

from app.agent.tools.verified_maps import InternalPlaceSchema
from app.core.llm import get_abto
from app.schemas.course import CourseRequestSchema, CourseSchema
from app.schemas.taste_profile import (
    ActivityPreferenceSchema,
    FoodPreferenceSchema,
    PreferredLocationTypeSchema,
    TravelPurposeSchema,
)
from app.services import course_place_copy


@pytest.fixture
def copy_request():
    profile = {name: dict.fromkeys(model.model_fields, 1) for name, model in {
        "travelPurpose": TravelPurposeSchema, "activityPreference": ActivityPreferenceSchema,
        "foodPreference": FoodPreferenceSchema, "preferredLocationType": PreferredLocationTypeSchema,
    }.items()}
    profile["travelPurpose"]["culturalExperience"] = 5
    profile.update(travelPaceDensity="balanced", spendingTendency="moderate", companionType="friends")
    return CourseRequestSchema.model_validate({
        "userId": "550e8400-e29b-41d4-a716-446655440000", "language": "ko", "mbti": "ENFP",
        "tasteProfile": profile,
        "tripCondition": {"destinationCountry": "대한민국", "destinationCity": "서울",
                          "startDate": "2026-11-01", "totalDays": 1, "budget": 900000},
    })


@pytest.fixture
def copy_course():
    stops = []
    for sequence, (name, category) in enumerate((("한빛박물관", "museum"), ("초록공원", "park")), 1):
        stops.append({
            "sequence": sequence, "stayMinutes": 60, "cost": 0,
            "reason": f"{name}의 안전한 기존 추천 이유", "memo": f"{name}의 안전한 기존 방문 팁",
            "place": InternalPlaceSchema(placeId=f"places/verified-{sequence}", placeName=name,
                                         category=category, address="대한민국 서울", latitude=37.5, longitude=127),
            "transportToNext": {"type": "none", "distance": 0, "minutes": 0, "memo": None},
        })
    return CourseSchema.model_validate({
        "title": "서울 여행", "destinationCountry": "대한민국", "destinationCity": "서울",
        "startDate": "2026-11-01", "totalDays": 1, "totalCost": 100000,
        "tags": ["문화", "자연"], "itinerary": {"days": [{"day": 1, "date": "2026-11-01", "stops": stops}]},
    })


def proposed():
    return {"stops": [
        {"slot": "1:1:places/verified-1", "reason": "문화 공간을 둘러보는 여행에 한빛박물관의 관람 경험을 더했어요.",
         "memo": "전시 안내를 먼저 읽고 궁금한 주제를 중심으로 자료를 살펴보세요."},
        {"slot": "1:2:places/verified-2", "reason": "초록공원에서는 걸으며 주변을 둘러보는 시간을 가질 수 있어요.",
         "memo": "현장 안내를 살펴 산책 방향을 고르고 돌아오는 길도 함께 생각해 보세요."},
    ]}


def reviewed(output):
    return {"stops": [{"slot": stop["slot"], "reason_supported": True, "memo_supported": True}
                      for stop in output["stops"]]}


def completion(output):
    return httpx.Response(200, json={
        "id": "chatcmpl-grounded-copy", "object": "chat.completion", "created": 0,
        "model": "offline-model", "choices": [{"index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": json.dumps(output)}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
    })


@pytest.fixture
def wire(mocker):
    clients, calls = [], []
    abto = get_abto()
    for method in ("openai_options", "async_openai_options"):
        original = getattr(abto, method)

        def record(*args, _original=original, **kwargs):
            options = _original(*args, **kwargs)
            clients.append(options["http_client"])
            return options

        mocker.patch.object(abto, method, side_effect=record)

    def install(respond):
        async def handle(request):
            calls.append(request)
            result = respond(request)
            return await result if asyncio.iscoroutine(result) else result

        mocker.patch("httpx._client.HTTPTransport", side_effect=lambda **kwargs: httpx.MockTransport(
            lambda request: pytest.fail("Copy generation must use async HTTP")))
        mocker.patch("httpx._client.AsyncHTTPTransport", side_effect=lambda **kwargs: httpx.MockTransport(handle))
        return calls, clients

    return install


def facts(course):
    value = deepcopy(course.model_dump())
    for day in value["itinerary"]["days"]:
        for stop in day["stops"]:
            stop.pop("reason")
            stop.pop("memo")
    return value


@pytest.mark.asyncio
async def test_real_sdk_writes_then_independently_reviews_only_verified_evidence(copy_request, copy_course, wire):
    output = proposed()
    calls, clients = wire(lambda request: completion(output if len(calls) == 1 else reviewed(output)))
    before = facts(copy_course)
    result = await course_place_copy.write_place_copy(copy_request, copy_course)
    assert result is copy_course and facts(result) == before
    for stop, expected in zip(result.itinerary.days[0].stops, output["stops"], strict=True):
        assert (stop.reason, stop.memo) == (expected["reason"], expected["memo"])
    assert len(calls) == 2 and all(client.is_closed for client in clients)
    bodies = [json.loads(call.content) for call in calls]
    assert [body["response_format"]["json_schema"]["name"] for body in bodies] == ["PlaceCopyBatch", "CopyReview"]
    for body in bodies:
        assert body["response_format"]["json_schema"]["strict"] is False
        assert "temperature" not in body and body["reasoning_effort"] == "low"
    source = json.loads(bodies[0]["messages"][-1]["content"].split("Evidence:\n")[1])
    assert source[0]["verified_type"] == "museum" and source[0]["verified_address"] == "대한민국 서울"
    assert source[0]["compatible_preferences"] == [{"field": "travelPurpose.culturalExperience", "label": "문화 체험", "score": 5}]
    assert source[1]["compatible_preferences"] == []
    serialized_source = json.dumps(source, ensure_ascii=False)
    assert not any(value in serialized_source for value in ("기존 추천", "기존 방문", "ENFP", "900000", "stayMinutes", "budget"))
    assert "Proposed copy:" in bodies[1]["messages"][-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("unsupported", [
    "왕실 유물을 전시하는 박물관이에요.", "무료 입장과 예약 없는 방문이 가능해요.",
    "옥상 정원이 아름다운 숨은 명소예요.", "이곳의 대표 메뉴는 알레르기 걱정 없이 먹을 수 있어요.",
    "사용자가 좋아하는 자연 산책을 위해 이 박물관을 골랐어요.",
])
async def test_unsupported_writer_claims_rejected_by_review_leave_rejected_slot_empty_and_keep_valid_peer(
    copy_request, copy_course, wire, unsupported,
):
    output = proposed()
    output["stops"][0]["reason"] = unsupported
    review = reviewed(output)
    review["stops"][0]["reason_supported"] = False
    calls, _ = wire(lambda request: completion(output if len(calls) == 1 else review))
    original = copy_course.itinerary.days[0].stops[0].model_dump()
    original["reason"] = original["memo"] = ""
    result = await course_place_copy.write_place_copy(copy_request, copy_course)
    assert result.itinerary.days[0].stops[0].model_dump() == original
    assert result.itinerary.days[0].stops[1].memo == output["stops"][1]["memo"]


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", [
    "입장료는 1000원이에요.", "안내는 https://unverified.test 를 참고하세요.",
    "A famous museum with a rooftop garden.", "왕실 유물을 전시하는 곳이에요.",
    "소장품과 상설 전시를 감상해 보세요.", "무료 입장과 예약 없이 방문이 가능해요.",
    "옥상 정원에서 조용한 분위기를 즐겨 보세요.",
    "초록공원의 산책 경험을 이곳에서 더해 보세요.", "...",
])
async def test_local_checks_reject_new_numbers_links_and_wrong_language_even_if_reviewer_approves(
    copy_request, copy_course, wire, unsafe,
):
    output = proposed()
    output["stops"][0]["memo"] = unsafe
    calls, _ = wire(lambda request: completion(output if len(calls) == 1 else reviewed(output)))
    original = ""
    result = await course_place_copy.write_place_copy(copy_request, copy_course)
    assert result.itinerary.days[0].stops[0].memo == original


@pytest.mark.asyncio
async def test_name_swapped_repeated_templates_are_not_applied(copy_request, copy_course, wire):
    output = proposed()
    for item, name in zip(output["stops"], ("한빛박물관", "초록공원"), strict=True):
        item["reason"] = f"{name}에서 새로운 방문 경험을 더해 보세요."
    calls, _ = wire(lambda request: completion(output if len(calls) == 1 else reviewed(output)))
    before = copy_course.model_dump()
    for day in before["itinerary"]["days"]:
        for stop in day["stops"]:
            stop["reason"] = stop["memo"] = ""
    await course_place_copy.write_place_copy(copy_request, copy_course)
    assert copy_course.model_dump() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,kind", [(phase, kind) for phase in ("writer", "review") for kind in ("missing", "duplicate", "wrong", "extra")])
async def test_incomplete_duplicate_foreign_or_extra_output_cannot_change_itinerary(copy_request, copy_course, wire, phase, kind):
    output, review = proposed(), reviewed(proposed())
    target = output if phase == "writer" else review
    if kind == "missing":
        target["stops"].pop()
    elif kind == "duplicate":
        target["stops"][1]["slot"] = target["stops"][0]["slot"]
    elif kind == "wrong":
        target["stops"][0]["slot"] = "unselected-place"
    else:
        target["stops"][0]["cost"] = 0
    calls, clients = wire(lambda request: completion(output if len(calls) == 1 else review))
    before = copy_course.model_dump()
    for day in before["itinerary"]["days"]:
        for stop in day["stops"]:
            stop["reason"] = stop["memo"] = ""
    await course_place_copy.write_place_copy(copy_request, copy_course)
    assert copy_course.model_dump() == before
    assert len(calls) == (1 if phase == "writer" else 2)
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["writer", "review"])
async def test_gateway_outage_leaves_copy_empty_without_retries(copy_request, copy_course, wire, phase):
    def respond(request):
        if phase == "writer" or len(calls) == 2:
            return httpx.Response(503, json={"error": {"message": "offline", "type": "server_error"}})
        return completion(proposed())

    calls, clients = wire(respond)
    before = copy_course.model_dump()
    for day in before["itinerary"]["days"]:
        for stop in day["stops"]:
            stop["reason"] = stop["memo"] = ""
    await course_place_copy.write_place_copy(copy_request, copy_course)
    assert copy_course.model_dump() == before
    assert len(calls) == (1 if phase == "writer" else 2)
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_and_cancellation_close_inflight_work(copy_request, copy_course, wire, cancel):
    started, closed = asyncio.Event(), asyncio.Event()

    async def respond(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    _, clients = wire(respond)
    before = copy_course.model_dump()
    for day in before["itinerary"]["days"]:
        for stop in day["stops"]:
            stop["reason"] = stop["memo"] = ""
    task = asyncio.create_task(course_place_copy.write_place_copy(copy_request, copy_course, timeout_seconds=0.2))
    await asyncio.wait_for(started.wait(), 1)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert await task is copy_course
    assert closed.is_set() and copy_course.model_dump() == before
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_expired_budget_skips_model_setup(copy_request, copy_course, mocker):
    gateway = mocker.patch.object(course_place_copy, "get_abto")
    assert await course_place_copy.write_place_copy(copy_request, copy_course, timeout_seconds=0) is copy_course
    gateway.assert_not_called()
    assert all(stop.memo == stop.reason == "" for day in copy_course.itinerary.days for stop in day.stops)


@pytest.mark.asyncio
async def test_requested_language_and_mbti_only_do_not_invent_preferences(copy_request, copy_course, wire):
    copy_request.language, copy_request.tasteProfile = "en", None
    output = proposed()
    calls, _ = wire(lambda request: completion(output if len(calls) == 1 else reviewed(output)))
    await course_place_copy.write_place_copy(copy_request, copy_course)
    assert copy_course.itinerary.days[0].stops[0].reason == output['stops'][0]['reason']
    source = json.loads(json.loads(calls[0].content)['messages'][-1]['content'].split('Evidence:\n')[1])
    assert all(not item['compatible_preferences'] for item in source)
    assert 'ENFP' not in json.dumps(source)
    assert 'Target language: ko' in json.loads(calls[0].content)['messages'][-1]['content']


@pytest.mark.asyncio
async def test_shared_hotel_and_airport_slots_are_written_without_replacing_verified_facts(copy_request, copy_course, wire):
    stops = copy_course.itinerary.days[0].stops
    stops[0].place.category, stops[1].place.category = 'lodging', 'airport'
    output = proposed()
    output['stops'][0].update(
        reason="여행 중 돌아와 하루를 마무리할 숙소 후보로 연결했어요.",
        memo="예약 내역의 객실 구성과 체크인 조건을 확인해 두세요.",
    )
    output['stops'][1].update(
        reason="여행지와 항공 이동을 연결하는 공항이에요.",
        memo="예약 내역에서 터미널과 항공사의 수속 마감 안내를 살펴보세요.",
    )
    calls, _ = wire(lambda request: completion(output if len(calls) == 1 else reviewed(output)))
    before = facts(copy_course)
    await course_place_copy.write_place_copy(copy_request, copy_course)
    assert facts(copy_course) == before
    assert stops[0].memo == output['stops'][0]['memo'] and stops[1].reason == output['stops'][1]['reason']


@pytest.mark.asyncio
async def test_failed_batch_does_not_discard_approved_other_batch(copy_request, copy_course, wire):
    # Two independent batches also cover slots beyond the first model schema cap.
    original = copy_course.itinerary.days[0].stops[0]
    letters = '가나다라마바사아자차카타파'
    stops = []
    for index, letter in enumerate(letters, 1):
        stop = original.model_copy(deep=True)
        stop.sequence, stop.place.placeId, stop.place.placeName = index, f'places/{letter}', f'{letter}박물관'
        stops.append(stop)
    copy_course.itinerary.days[0].stops = stops

    def respond(request):
        content = json.loads(request.content)['messages'][-1]['content']
        source = json.loads(content.split('Evidence:\n')[1].split('\nProposed copy:')[0])
        if len(source) == 1:
            return httpx.Response(503, json={'error': {'message': 'offline', 'type': 'server_error'}})
        output = {'stops': [{
            'slot': item['slot'], 'reason': f"{letters[index]} 관람 경험을 여행에 더했어요.",
            'memo': f"{letters[index]} 주제를 골라 전시 안내를 살펴보세요.",
        } for index, item in enumerate(source)]}
        return completion(reviewed(output) if 'Proposed copy:' in content else output)

    calls, clients = wire(respond)
    before = facts(copy_course)
    last_fallback = ""
    await course_place_copy.write_place_copy(copy_request, copy_course)
    assert len(calls) == 3 and all(client.is_closed for client in clients)
    assert facts(copy_course) == before and stops[-1].memo == last_fallback
    assert all(stop.memo != last_fallback for stop in stops[:-1])

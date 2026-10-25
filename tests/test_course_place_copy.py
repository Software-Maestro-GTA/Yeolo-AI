"""Exercise grounded copy through the real LangChain/SDK wire without networking."""

import asyncio
import json
from copy import deepcopy
from datetime import date, timedelta

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
from app.services.course_display_diagnostics import DisplayDiagnostics


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


def copy_response(request, output, review=None, *, filter_slots=True):
    content = json.loads(request.content)["messages"][-1]["content"]
    source = json.loads(content.split("Evidence:\n")[1].split("\nProposed copy:")[0])
    slots = {item["slot"] for item in source}
    value = (review if review is not None else reviewed(output)) if "Proposed copy:" in content else output
    if filter_slots:
        value = {"stops": [item for item in value["stops"] if item["slot"] in slots]}
    return completion(value)


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


def extend_days(course, count):
    original = course.itinerary.days[0]
    course.itinerary.days = []
    course.totalDays = count
    for index in range(count):
        day = original.model_copy(deep=True)
        day.day = index + 1
        day.date = (date.fromisoformat(original.date) + timedelta(days=index)).isoformat()
        for stop in day.stops:
            stop.place.placeId = f"places/day-{day.day}-stop-{stop.sequence}"
        course.itinerary.days.append(day)


def day_payload(request):
    content = json.loads(request.content)["messages"][-1]["content"]
    source = json.loads(content.split("Evidence:\n")[1].split("\nProposed copy:")[0])
    days = {item["slot"].split(":")[0] for item in source}
    assert len(days) == 1, "A model request must contain evidence from exactly one day"
    output = {"stops": []}
    for item in source:
        day, sequence, _ = item["slot"].split(":", 2)
        letter = "가나다라마바사아"[(int(day) - 1) * 2 + int(sequence) - 1]
        output["stops"].append({
            "slot": item["slot"], "reason": f"{letter} 관람 경험을 여행에 더했어요.",
            "memo": f"{letter} 주제를 골라 현장 안내를 살펴보세요.",
        })
    return int(next(iter(days))), "Proposed copy:" in content, output


@pytest.mark.asyncio
async def test_days_write_concurrently_and_review_without_waiting_for_other_days(copy_request, copy_course, wire):
    extend_days(copy_course, 3)
    all_writers_started, last_day_reviewed = asyncio.Event(), asyncio.Event()
    writers, reviews = [], []

    async def respond(request):
        day, is_review, output = day_payload(request)
        if not is_review:
            writers.append(day)
            if len(writers) == 3:
                all_writers_started.set()
            await asyncio.wait_for(all_writers_started.wait(), 2)
        else:
            if day == 1:
                await asyncio.wait_for(last_day_reviewed.wait(), 2)
            reviews.append(day)
            if day == 3:
                last_day_reviewed.set()
        return completion(reviewed(output) if is_review else output)

    calls, clients = wire(respond)
    before = facts(copy_course)
    await course_place_copy.write_place_copy(copy_request, copy_course)
    assert sorted(writers) == [1, 2, 3] and reviews.index(3) < reviews.index(1)
    assert len(calls) == 6 and all(client.is_closed for client in clients)
    assert facts(copy_course) == before
    for day in copy_course.itinerary.days:
        for stop in day.stops:
            letter = "가나다라마바사"[(day.day - 1) * 2 + stop.sequence - 1]
            assert stop.reason == f"{letter} 관람 경험을 여행에 더했어요."
            assert stop.memo == f"{letter} 주제를 골라 현장 안내를 살펴보세요."


@pytest.mark.asyncio
async def test_all_days_start_together_without_three_day_cap_and_failure_preserves_peers(copy_request, copy_course, wire):
    extend_days(copy_course, 4)
    all_days_started, release = asyncio.Event(), asyncio.Event()
    writers = []
    active = peak = 0

    async def respond(request):
        nonlocal active, peak
        day, is_review, output = day_payload(request)
        active += 1
        peak = max(peak, active)
        try:
            if not is_review:
                writers.append(day)
                if len(writers) == 4:
                    all_days_started.set()
                await release.wait()
            if day == 2:
                return httpx.Response(503, json={"error": {"message": "offline", "type": "server_error"}})
            return completion(reviewed(output) if is_review else output)
        finally:
            active -= 1

    calls, clients = wire(respond)
    before = facts(copy_course)
    task = asyncio.create_task(course_place_copy.write_place_copy(copy_request, copy_course))
    try:
        await asyncio.wait_for(all_days_started.wait(), 2)
        assert writers == [1, 2, 3, 4] and peak == 4
    finally:
        release.set()
        await task
    assert writers == [1, 2, 3, 4, 2] and peak == 4 and active == 0
    assert len(calls) == 8 and all(client.is_closed for client in clients)
    assert facts(copy_course) == before
    for day in copy_course.itinerary.days:
        assert all(bool(stop.reason) == bool(stop.memo) == (day.day != 2) for stop in day.stops)


@pytest.mark.asyncio
async def test_day_review_timeout_keeps_completed_days_and_closes_pending_work(copy_request, copy_course, wire):
    extend_days(copy_course, 3)
    hanging_review_closed = asyncio.Event()

    async def respond(request):
        day, is_review, output = day_payload(request)
        if day == 2 and is_review:
            try:
                await asyncio.Event().wait()
            finally:
                hanging_review_closed.set()
        return completion(reviewed(output) if is_review else output)

    calls, clients = wire(respond)
    before = facts(copy_course)
    report = DisplayDiagnostics('place_copy')
    await course_place_copy.write_place_copy(copy_request, copy_course, timeout_seconds=0.4, diagnostics=report)
    assert hanging_review_closed.is_set()
    assert len(calls) == 6 and all(client.is_closed for client in clients)
    assert facts(copy_course) == before
    for day in copy_course.itinerary.days:
        assert all(bool(stop.reason) == bool(stop.memo) == (day.day != 2) for stop in day.stops)
    assert report.status == 'partial' and (report.total, report.completed) == (6, 4)
    assert any(item['phase'] == 'place_copy_review' and item['reason'] == 'cancelled'
               and item['day'] == 2 for item in report.failures)
    assert any(item['phase'] == 'budget' and item['reason'] == 'timeout' for item in report.failures)


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_budget", [True, False])
async def test_shared_course_budget_allows_work_and_retry_beyond_former_copy_limit(
    copy_request, copy_course, wire, mocker, explicit_budget,
):
    """Scale time so a simulated 35-second first call can recover before 95 seconds."""
    real_timeout = asyncio.timeout
    budgets = []

    def accelerated_timeout(seconds):
        budgets.append(seconds)
        return real_timeout(seconds / 100)

    mocker.patch.object(course_place_copy.settings, "COURSE_TIMEOUT_SECONDS", 95)
    mocker.patch.object(course_place_copy.asyncio, "timeout", side_effect=accelerated_timeout)
    writers = 0

    async def respond(request):
        nonlocal writers
        content = json.loads(request.content)["messages"][-1]["content"]
        if "Proposed copy:" not in content:
            writers += 1
            if writers == 1:
                await asyncio.sleep(0.35)
                return httpx.Response(503, json={"error": {"message": "offline", "type": "server_error"}})
        return copy_response(request, proposed())

    calls, clients = wire(respond)
    before = facts(copy_course)
    kwargs = {"timeout_seconds": 95} if explicit_budget else {}
    await course_place_copy.write_place_copy(copy_request, copy_course, **kwargs)
    assert budgets == [95] and writers == 2 and len(calls) == 3
    assert facts(copy_course) == before
    assert all(stop.reason and stop.memo for stop in copy_course.itinerary.days[0].stops)
    for call in calls:
        assert call.extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), None)
        assert "x-stainless-read-timeout" not in call.headers
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_real_sdk_writes_then_independently_reviews_only_verified_evidence(copy_request, copy_course, wire):
    output = proposed()
    calls, clients = wire(lambda request: copy_response(request, output))
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
        assert body["max_completion_tokens"] == 16000
    source = json.loads(bodies[0]["messages"][-1]["content"].split("Evidence:\n")[1])
    assert source[0]["verified_type"] == "museum" and source[0]["verified_address"] == "대한민국 서울"
    assert source[0]["compatible_preferences"] == [{"field": "travelPurpose.culturalExperience", "label": "문화 체험", "score": 5}]
    assert source[1]["compatible_preferences"] == []
    assert source[0]["itinerary_context"] == {"previous_verified_type": None, "next_verified_type": "park"}
    assert source[1]["itinerary_context"] == {"previous_verified_type": "museum", "next_verified_type": None}
    serialized_source = json.dumps(source, ensure_ascii=False)
    assert not any(value in serialized_source for value in ("기존 추천", "기존 방문", "ENFP", "900000", "stayMinutes", "budget"))
    assert "Proposed copy:" in bodies[1]["messages"][-1]["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize('advice', [
    '조용한 분위기를 유지하며 다른 이용자를 배려해 둘러보세요.',
    '대표 메뉴가 있는지 현장 안내를 확인하고 취향에 맞게 선택해 보세요.',
    '무료 입장 여부와 예약 조건을 확인한 뒤 방문 계획을 조정해 보세요.',
    '상설 전시가 있다면 공개된 관람 안내에 따라 관심 있는 주제부터 살펴보세요.',
])
async def test_reviewed_conditional_advice_is_not_rejected_by_keyword(copy_request, copy_course, wire, advice):
    output = proposed()
    output['stops'][0]['memo'] = advice
    calls, _ = wire(lambda request: copy_response(request, output))
    report = DisplayDiagnostics('place_copy')
    await course_place_copy.write_place_copy(copy_request, copy_course, diagnostics=report)
    first = copy_course.itinerary.days[0].stops[0]
    assert (first.reason, first.memo) == (output['stops'][0]['reason'], advice)
    assert len(calls) == 2 and report.status == 'success' and report.retries == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('rejected_field', ['reason', 'memo'])
async def test_field_review_preserves_approved_text_when_retry_fails(copy_request, copy_course, wire, rejected_field):
    output = proposed()
    approved_field = 'memo' if rejected_field == 'reason' else 'reason'
    writers = []

    def respond(request):
        content = json.loads(request.content)['messages'][-1]['content']
        if 'Proposed copy:' not in content:
            writers.append(json.loads(content.split('Evidence:\n')[1]))
            if len(writers) == 2:
                return httpx.Response(503, json={'error': {'message': 'offline', 'type': 'server_error'}})
        review = reviewed(output)
        review['stops'][0][f'{rejected_field}_supported'] = False
        return copy_response(request, output, review)

    calls, _ = wire(respond)
    before = facts(copy_course)
    report = DisplayDiagnostics('place_copy')
    await course_place_copy.write_place_copy(copy_request, copy_course, diagnostics=report)
    first = copy_course.itinerary.days[0].stops[0]
    assert getattr(first, rejected_field) == ''
    assert getattr(first, approved_field) == output['stops'][0][approved_field]
    assert writers[1][0]['approved_copy'] == {approved_field: output['stops'][0][approved_field]}
    assert facts(copy_course) == before and len(calls) == 3
    assert (report.completed, report.total, report.status) == (1, 2, 'partial')


@pytest.mark.asyncio
@pytest.mark.parametrize("unsupported", [
    "왕실 유물을 전시하는 박물관이에요.", "무료 입장과 예약 없는 방문이 가능해요.",
    "옥상 정원이 아름다운 숨은 명소예요.", "이곳의 대표 메뉴는 알레르기 걱정 없이 먹을 수 있어요.",
    "소장품과 상설 전시를 감상해 보세요.", "조용한 분위기가 보장되는 박물관이에요.",
    "사용자가 좋아하는 자연 산책을 위해 이 박물관을 골랐어요.",
])
async def test_unsupported_writer_claims_leave_reason_empty_and_keep_approved_memo_and_peer(
    copy_request, copy_course, wire, unsupported,
):
    output = proposed()
    output["stops"][0]["reason"] = unsupported
    review = reviewed(output)
    review["stops"][0]["reason_supported"] = False
    wire(lambda request: copy_response(request, output, review))
    original = copy_course.itinerary.days[0].stops[0].model_dump()
    original["reason"] = ""
    original["memo"] = output['stops'][0]['memo']
    result = await course_place_copy.write_place_copy(copy_request, copy_course)
    assert result.itinerary.days[0].stops[0].model_dump() == original
    assert result.itinerary.days[0].stops[1].memo == output["stops"][1]["memo"]


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", [
    "입장료는 1000원이에요.", "안내는 https://unverified.test 를 참고하세요.",
    "A famous museum with a rooftop garden.",
    "초록공원의 산책 경험을 이곳에서 더해 보세요.", "...",
])
async def test_local_checks_reject_new_numbers_links_and_wrong_language_even_if_reviewer_approves(
    copy_request, copy_course, wire, unsafe,
):
    output = proposed()
    output["stops"][0]["memo"] = unsafe
    wire(lambda request: copy_response(request, output))
    original = ""
    result = await course_place_copy.write_place_copy(copy_request, copy_course)
    assert result.itinerary.days[0].stops[0].memo == original
    assert result.itinerary.days[0].stops[0].reason == output['stops'][0]['reason']


@pytest.mark.asyncio
@pytest.mark.parametrize("separate_days", [False, True])
async def test_name_swapped_repeated_templates_are_not_applied(copy_request, copy_course, wire, separate_days):
    output = proposed()
    for item, name in zip(output["stops"], ("한빛박물관", "초록공원"), strict=True):
        item["reason"] = f"{name}에서 새로운 방문 경험을 더해 보세요."
    if separate_days:
        first_day = copy_course.itinerary.days[0]
        second_day = first_day.model_copy(deep=True)
        second_day.day = 2
        second_day.date = (date.fromisoformat(first_day.date) + timedelta(days=1)).isoformat()
        second_day.stops = [first_day.stops.pop()]
        copy_course.itinerary.days.append(second_day)
        copy_course.totalDays = 2
        output["stops"][1]["slot"] = "2:2:places/verified-2"

    def respond(request):
        content = json.loads(request.content)["messages"][-1]["content"]
        source = json.loads(content.split("Evidence:\n")[1].split("\nProposed copy:")[0])
        slots = {item["slot"] for item in source}
        batch = {"stops": [item for item in output["stops"] if item["slot"] in slots]}
        return completion(reviewed(batch) if "Proposed copy:" in content else batch)

    wire(respond)
    before = copy_course.model_dump()
    for day in before["itinerary"]["days"]:
        for stop in day["stops"]:
            stop["reason"] = ""
            stop["memo"] = next(item['memo'] for item in output['stops']
                                if item['slot'].endswith(stop['place']['placeId']))
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
    calls, clients = wire(lambda request: copy_response(request, output, review, filter_slots=False))
    before = copy_course.model_dump()
    for day in before["itinerary"]["days"]:
        for stop in day["stops"]:
            stop["reason"] = stop["memo"] = ""
    await course_place_copy.write_place_copy(copy_request, copy_course)
    assert copy_course.model_dump() == before
    assert len(calls) == (2 if phase == "writer" else 4)
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["writer", "review"])
async def test_gateway_outage_leaves_copy_empty_after_exactly_one_retry(copy_request, copy_course, wire, phase):
    def respond(request):
        content = json.loads(request.content)["messages"][-1]["content"]
        if phase == "writer" or "Proposed copy:" in content:
            return httpx.Response(503, json={"error": {"message": "offline", "type": "server_error"}})
        return completion(proposed())

    calls, clients = wire(respond)
    before = copy_course.model_dump()
    for day in before["itinerary"]["days"]:
        for stop in day["stops"]:
            stop["reason"] = stop["memo"] = ""
    await course_place_copy.write_place_copy(copy_request, copy_course)
    assert copy_course.model_dump() == before
    assert len(calls) == (2 if phase == "writer" else 4)
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["writer", "review"])
@pytest.mark.parametrize("failure", ["503", "invalid_schema"])
async def test_failed_pipeline_recovers_on_one_retry(copy_request, copy_course, wire, phase, failure):
    failed = False

    def respond(request):
        nonlocal failed
        content = json.loads(request.content)["messages"][-1]["content"]
        current_phase = "review" if "Proposed copy:" in content else "writer"
        if not failed and current_phase == phase:
            failed = True
            if failure == "503":
                return httpx.Response(503, json={"error": {"message": "offline", "type": "server_error"}})
            return completion({"unrecognized": "invalid structured output"})
        return copy_response(request, proposed())

    calls, clients = wire(respond)
    before = facts(copy_course)
    await course_place_copy.write_place_copy(copy_request, copy_course)
    assert facts(copy_course) == before
    assert all(stop.reason and stop.memo for stop in copy_course.itinerary.days[0].stops)
    assert len(calls) == (3 if phase == "writer" else 4)
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["review", "local_guard"])
@pytest.mark.parametrize("retry_copy", ["valid", "duplicate", "duplicate_memo"])
async def test_retry_targets_failed_slots_only_and_preserves_approved_peer(
    copy_request, copy_course, wire, failure, retry_copy,
):
    writers = []
    initial, fixed = proposed(), proposed()
    initial["stops"][0]["memo"] = "입장료는 1000원이에요."
    if retry_copy == "duplicate":
        fixed["stops"][0]["reason"] = fixed["stops"][1]["reason"].replace("초록공원", "한빛박물관")
    elif retry_copy == 'duplicate_memo':
        fixed['stops'][0]['memo'] = fixed['stops'][1]['memo']

    def respond(request):
        content = json.loads(request.content)["messages"][-1]["content"]
        if "Proposed copy:" not in content:
            writers.append(json.loads(content.split("Evidence:\n")[1]))
        output = initial if len(writers) == 1 else fixed
        review = reviewed(output)
        if failure == "review" and len(writers) == 1:
            review["stops"][0]["memo_supported"] = False
        return copy_response(request, output, review)

    calls, clients = wire(respond)
    before = facts(copy_course)
    await course_place_copy.write_place_copy(copy_request, copy_course)
    assert [len(source) for source in writers] == [2, 1]
    assert writers[1][0]["slot"] == "1:1:places/verified-1"
    first, second = copy_course.itinerary.days[0].stops
    assert (second.reason, second.memo) == (initial["stops"][1]["reason"], initial["stops"][1]["memo"])
    # A retry cannot replace an already approved reason, even with a duplicate.
    expected_memo = '' if retry_copy == 'duplicate_memo' else fixed['stops'][0]['memo']
    assert (first.reason, first.memo) == (initial["stops"][0]["reason"], expected_memo)
    assert writers[1][0]['approved_copy'] == {'reason': initial['stops'][0]['reason']}
    assert facts(copy_course) == before and len(calls) == 4
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_capacity_failures_wait_for_initial_work_then_retry_serially(copy_request, copy_course, wire):
    extend_days(copy_course, 3)
    busy_review, release = asyncio.Event(), asyncio.Event()
    finished_initial = False
    attempts, retry_order = {}, []

    async def respond(request):
        nonlocal finished_initial
        day, is_review, output = day_payload(request)
        if not is_review:
            attempts[day] = attempts.get(day, 0) + 1
        if day in (1, 2) and attempts[day] == 1:
            return httpx.Response(429, json={"error": {"message": "concurrency limit", "type": "rate_limit_error"}})
        if day == 3 and is_review:
            busy_review.set()
            await release.wait()
            finished_initial = True
        if attempts[day] == 2:
            assert finished_initial, "A capacity retry must wait for initial pipelines to finish"
            retry_order.append((day, "review" if is_review else "writer"))
        return completion(reviewed(output) if is_review else output)

    calls, clients = wire(respond)
    before = facts(copy_course)
    task = asyncio.create_task(course_place_copy.write_place_copy(copy_request, copy_course))
    try:
        await asyncio.wait_for(busy_review.wait(), 2)
        assert attempts == {1: 1, 2: 1, 3: 1} and not retry_order
    finally:
        release.set()
        await task
    assert retry_order == [(1, "writer"), (1, "review"), (2, "writer"), (2, "review")]
    assert all(stop.reason and stop.memo for day in copy_course.itinerary.days for stop in day.stops)
    assert len(calls) == 8 and facts(copy_course) == before
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_all_initial_requests_throttled_do_not_deadlock_or_retry_more_than_once(copy_request, copy_course, wire):
    extend_days(copy_course, 3)
    calls, clients = wire(lambda request: httpx.Response(
        429, json={"error": {"message": "concurrency limit", "type": "rate_limit_error"}},
    ))
    await asyncio.wait_for(course_place_copy.write_place_copy(copy_request, copy_course), 2)
    assert len(calls) == 6
    assert all(stop.reason == stop.memo == "" for day in copy_course.itinerary.days for stop in day.stops)
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_or_cancellation_during_retry_closes_work_without_another_request(copy_request, copy_course, wire, cancel):
    extend_days(copy_course, 2)
    retry_started, retry_closed = asyncio.Event(), asyncio.Event()
    writers = {}

    async def respond(request):
        day, is_review, output = day_payload(request)
        if not is_review:
            writers[day] = writers.get(day, 0) + 1
        if day == 1:
            if writers[day] == 1:
                return httpx.Response(503, json={"error": {"message": "offline", "type": "server_error"}})
            retry_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                retry_closed.set()
        return completion(reviewed(output) if is_review else output)

    calls, clients = wire(respond)
    before = facts(copy_course)
    task = asyncio.create_task(course_place_copy.write_place_copy(copy_request, copy_course, timeout_seconds=0.4))
    await asyncio.wait_for(retry_started.wait(), 2)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        await task
        assert all(stop.reason and stop.memo for stop in copy_course.itinerary.days[1].stops)
    assert retry_closed.is_set() and writers == {1: 2, 2: 1}
    assert all(stop.reason == stop.memo == "" for stop in copy_course.itinerary.days[0].stops)
    assert len(calls) == 4 and facts(copy_course) == before
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
    report = DisplayDiagnostics('place_copy')
    task = asyncio.create_task(course_place_copy.write_place_copy(
        copy_request, copy_course, timeout_seconds=0.2, diagnostics=report,
    ))
    await asyncio.wait_for(started.wait(), 1)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert await task is copy_course
    assert closed.is_set() and copy_course.model_dump() == before
    assert all(client.is_closed for client in clients)
    assert report.status == ('cancelled' if cancel else 'failed')
    assert any(item['phase'] == 'place_copy_write' and item['reason'] == 'cancelled' for item in report.failures)


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
    calls, _ = wire(lambda request: copy_response(request, output))
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
    wire(lambda request: copy_response(request, output))
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
    assert len(calls) == 4 and all(client.is_closed for client in clients)
    assert facts(copy_course) == before and stops[-1].memo == last_fallback
    assert all(stop.memo != last_fallback for stop in stops[:-1])


@pytest.mark.asyncio
async def test_detailed_grounded_copy_survives_review_without_truncation(copy_request, copy_course, wire):
    stops = copy_course.itinerary.days[0].stops
    stops[0].place.providerTypes = ['museum', 'art_gallery', 'point_of_interest']
    output = proposed()
    output['stops'][0].update(
        reason=('한빛박물관은 박물관과 미술관 유형으로 확인되어, 문화 공간을 둘러보는 경험을 여행에 더할 수 있어요. '
                '문화 체험에 높은 관심을 보인 취향과 연결하고, 뒤이어 공원을 방문하는 순서에 맞춰 서로 다른 유형의 공간을 접하도록 구성했어요.'),
        memo=('관람을 시작할 때 현장 안내를 살펴보고, 관심이 가는 주제가 있다면 그 부분을 중심으로 둘러보세요. '
              '설명 자료를 만날 경우 눈에 들어오는 내용과 연결해 읽어보면 관람의 초점을 잡는 데 도움이 될 수 있어요. '
              '모든 대상을 같은 비중으로 보려고 하기보다 더 알아보고 싶은 부분을 고르고, 관람 중 관심이 달라지면 살펴보는 순서도 조정해 보세요.'),
    )
    calls, _ = wire(lambda request: copy_response(request, output))
    before = facts(copy_course)
    result = await course_place_copy.write_place_copy(copy_request, copy_course)
    assert facts(result) == before
    assert result.itinerary.days[0].stops[0].reason == output['stops'][0]['reason']
    assert result.itinerary.days[0].stops[0].memo == output['stops'][0]['memo']
    assert 100 <= len(stops[0].reason) <= 180
    assert 140 <= len(stops[0].memo) <= 240
    assert len(calls) == 2
    source = json.loads(json.loads(calls[0].content)['messages'][-1]['content'].split('Evidence:\n')[1])
    assert source[0]['verified_types'] == ['art_gallery', 'museum', 'point_of_interest']
    assert source[0]['itinerary_context']['next_verified_type'] == 'park'
    review_source = json.loads(calls[1].content)['messages'][-1]['content']
    reviewed_copy = json.loads(review_source.split('Proposed copy:\n')[1])
    assert reviewed_copy['stops'][0]['memo'] == output['stops'][0]['memo']


@pytest.mark.asyncio
async def test_long_unsupported_detail_is_rejected_while_grounded_peer_survives(copy_request, copy_course, wire):
    output = proposed()
    output['stops'][0]['reason'] = (
        '문화 공간을 둘러보는 경험을 여행에 더하며 관심 있는 내용을 차분히 살펴볼 수 있도록 골랐어요. '
        '이 박물관의 특별 전시에서는 지역 장인의 작업 과정을 직접 보고, 해설사가 작품에 담긴 배경을 설명해 주기 때문에 문화 체험 취향에 구체적으로 맞출 수 있어요.'
    )
    review = reviewed(output)
    review['stops'][0]['reason_supported'] = False
    wire(lambda request: copy_response(request, output, review))
    before = facts(copy_course)
    await course_place_copy.write_place_copy(copy_request, copy_course)
    first, second = copy_course.itinerary.days[0].stops
    assert len(output['stops'][0]['reason']) > 100
    assert first.reason == '' and first.memo == output['stops'][0]['memo']
    assert second.reason == output['stops'][1]['reason']
    assert facts(copy_course) == before

"""Exercise optional place prose using final identities and offline model results."""

import asyncio
import json

import pytest

from app.schemas.course import CourseRequestSchema, CourseSchema
from app.schemas.taste_profile import (
    ActivityPreferenceSchema,
    FoodPreferenceSchema,
    PreferredLocationTypeSchema,
    TasteProfileSchema,
    TravelPurposeSchema,
)

HOURS_NOTE = ' 영업시간 미확인: 방문 전 확인이 필요합니다.'


@pytest.fixture
def copy_request():
    profile = {name: dict.fromkeys(model.model_fields, 1) for name, model in {
        'travelPurpose': TravelPurposeSchema, 'foodPreference': FoodPreferenceSchema,
        'activityPreference': ActivityPreferenceSchema, 'preferredLocationType': PreferredLocationTypeSchema,
    }.items()}
    profile.update(travelPaceDensity='balanced', spendingTendency='moderate', companionType='friends')
    profile['travelPurpose']['culturalExperience'] = 5
    profile['activityPreference']['viewing'] = 4
    return CourseRequestSchema.model_validate({
        'userId': '550e8400-e29b-41d4-a716-446655440000', 'mbti': 'INTJ',
        'tasteProfile': TasteProfileSchema.model_validate(profile),
        'tripCondition': {'destinationCountry': '대한민국', 'destinationCity': '서울', 'startDate': '2026-10-06', 'totalDays': 2, 'budgetType': 'moderate'},
    })


@pytest.fixture
def verified_course():
    def stop(sequence, place_id, name, suffix=''):
        return {
            'sequence': sequence, 'arrivalTime': '10:15', 'stayMinutes': 65,
            'memo': '기존 검증된 방문 팁.' + suffix, 'reason': '기존 검증된 경험 추천.', 'cost': 17000,
            'place': {'placeId': place_id, 'placeName': name, 'placeEngName': 'Verified Museum', 'category': 'museum', 'address': '대한민국 서울', 'latitude': 37.55, 'longitude': 126.98},
            'transportToNext': {'type': 'walking', 'distance': 700, 'minutes': 12, 'cost': 0, 'memo': '[추정 도보] 기존 표식 보존'},
        }
    return CourseSchema.model_validate({
        'title': '검증된 서울 코스', 'destinationCountry': '대한민국', 'destinationCity': '서울',
        'startDate': '2026-10-06', 'totalDays': 2, 'tags': ['문화'], 'recommendationReason': '기존 코스 추천과 이력 안내.',
        'itinerary': {'days': [
            {'day': 1, 'date': '2026-10-06', 'memo': '첫날 감성 요약.', 'stops': [stop(1, 'places/national', '국립중앙박물관', HOURS_NOTE), stop(2, 'places/modern', '국립현대미술관')]},
            {'day': 2, 'date': '2026-10-07', 'memo': '둘째 날 감성 요약.', 'stops': [stop(1, 'places/national', '국립중앙박물관')]},
        ]},
    })


def valid_rows(course):
    return [{'day': day.day, 'sequence': stop.sequence, 'placeId': stop.place.placeId,
             'reason': f'{stop.place.placeName}에서 전시의 주제를 살펴보는 경험을 추천해요. 작품과 자료를 직접 비교하며 새로운 관점을 발견해보세요.',
             'memo': f'{stop.place.placeName}의 전시 흐름을 따라 작품의 표현과 자료의 연결을 살펴보세요. 먼저 마음에 드는 주제를 고르고 설명을 읽으며 관람하면 공간을 더욱 깊이 경험할 수 있어요.'}
            for day in course.itinerary.days for stop in day.stops]


def without_place_prose(course):
    payload = course.model_dump()
    for day in payload['itinerary']['days']:
        for stop in day['stops']:
            stop.pop('reason')
            stop.pop('memo')
    return payload


@pytest.mark.asyncio
async def test_one_batch_changes_only_prose_for_all_final_occurrences_and_preserves_hours(verified_course, copy_request, offline_place_copy):
    from app.services.course_copy import enrich_course_place_copy

    original = verified_course.model_dump()
    rows = valid_rows(verified_course)
    offline_place_copy.return_value = {'stops': list(reversed(rows))}
    result = await enrich_course_place_copy(verified_course, copy_request)
    offline_place_copy.assert_awaited_once()
    assert result is not verified_course and verified_course.model_dump() == original
    assert without_place_prose(result) == without_place_prose(verified_course)
    for day in result.itinerary.days:
        for stop in day.stops:
            expected = next(row for row in rows if (row['day'], row['sequence'], row['placeId']) == (day.day, stop.sequence, stop.place.placeId))
            assert stop.reason == expected['reason']
            suffix = HOURS_NOTE if day.day == 1 and stop.sequence == 1 else ''
            assert stop.memo == expected['memo'] + suffix
    assert result.itinerary.days[0].stops[0].reason != result.itinerary.days[0].stops[1].reason
    payload = offline_place_copy.call_args.args[0]
    assert len(payload['stops']) == 3
    assert {(row['day'], row['sequence'], row['placeId']) for row in payload['stops']} == {(row['day'], row['sequence'], row['placeId']) for row in rows}


@pytest.mark.asyncio
async def test_model_input_uses_actual_strong_preferences_verified_data_and_separate_selection_context(verified_course, copy_request, offline_place_copy):
    from app.services.course_copy import enrich_course_place_copy

    contexts = {(1, 1, 'places/national'): {'experiences': ['culture'], 'name': '후보가 만든 가짜 이름', 'reason': '후보 주장 무료 입장'}}
    await enrich_course_place_copy(verified_course, copy_request, selection_context=contexts)
    payload = offline_place_copy.call_args.args[0]
    encoded = json.dumps(payload, ensure_ascii=False)
    assert '국립중앙박물관' in encoded and '대한민국 서울' in encoded and 'museum' in encoded
    preferences = json.dumps(payload['preferences'], ensure_ascii=False)
    assert 'culturalExperience' in preferences and 'viewing' in preferences and '5' in preferences and '4' in preferences
    assert not any(leak in encoded for leak in (str(copy_request.userId), 'INTJ', '외향', '10:15', '17000', '700', '65', '도보', '영업시간 미확인', '기존 검증된', '후보가 만든', '후보 주장'))
    assert not any(low_score_field in preferences for low_score_field in ('natureExploration', 'nightlife', 'dietaryRestriction'))
    assert 'culture' in encoded
    # Context can help explain selection but must be labeled separately from
    # the official provider facts; it is not a verified place description.
    context_row = payload['stops'][0]
    assert any('context' in key.lower() for key in context_row)
    copy_request.tasteProfile = None
    offline_place_copy.reset_mock()
    await enrich_course_place_copy(verified_course, copy_request)
    assert not offline_place_copy.call_args.args[0]['preferences']
    assert 'INTJ' not in json.dumps(offline_place_copy.call_args.args[0], ensure_ascii=False)


@pytest.mark.asyncio
async def test_identity_duplicate_unknown_swapped_and_malformed_rows_fall_back_individually(verified_course, copy_request, offline_place_copy):
    from app.services.course_copy import enrich_course_place_copy

    rows = valid_rows(verified_course)
    rows[1]['memo'] = 42
    # Duplicate target must not pick whichever response happens to occur last.
    duplicate = {**rows[0], 'reason': '다른 중복 문구.'}
    unknown = {**rows[0], 'placeId': 'places/unverified'}
    swapped = {**rows[2], 'sequence': 2}
    offline_place_copy.return_value = {'stops': [rows[0], rows[1], rows[2], duplicate, unknown, swapped, None]}
    result = await enrich_course_place_copy(verified_course, copy_request)
    assert result.itinerary.days[0].stops == verified_course.itinerary.days[0].stops
    assert result.itinerary.days[1].stops[0].reason == rows[2]['reason']
    assert result.itinerary.days[1].stops[0].memo == rows[2]['memo']
    assert without_place_prose(result) == without_place_prose(verified_course)


@pytest.mark.asyncio
async def test_missing_identity_leaves_only_that_occurrence_unchanged(verified_course, copy_request, offline_place_copy):
    from app.services.course_copy import enrich_course_place_copy

    row = valid_rows(verified_course)[0]
    offline_place_copy.return_value = {'stops': [row]}
    result = await enrich_course_place_copy(verified_course, copy_request)
    assert result.itinerary.days[0].stops[0].reason == row['reason']
    assert result.itinerary.days[0].stops[1] == verified_course.itinerary.days[0].stops[1]
    assert result.itinerary.days[1] == verified_course.itinerary.days[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_text', [
    '출처: https://maps.google.com/place/123', '<b>멋진 전시</b>',
    '18:00에 방문해 65분 머물도록 배치했어요.', '입장료는 17000원입니다.',
    '문화에 관심이 많은 당신에게 좋아요.', '예약이 필수이며 촬영이 가능합니다.',
    '새로운 내용\x00숨겨진 제어문자', '문장을 자르면 안 됩니다. ' * 300,
], ids=['source_url', 'markup', 'schedule_echo', 'price', 'interest_label', 'operational_claim', 'control', 'oversize'])
async def test_policy_invalid_pair_preserves_its_baseline_without_discarding_other_valid_rows(verified_course, copy_request, offline_place_copy, bad_text):
    from app.services.course_copy import enrich_course_place_copy

    rows = valid_rows(verified_course)
    rows[0]['memo'] = bad_text
    offline_place_copy.return_value = {'stops': rows}
    result = await enrich_course_place_copy(verified_course, copy_request)
    assert result.itinerary.days[0].stops[0] == verified_course.itinerary.days[0].stops[0]
    assert result.itinerary.days[0].stops[1].reason == rows[1]['reason']


@pytest.mark.asyncio
@pytest.mark.parametrize(('memo', 'accepted'), [
    ('공간의 선과 빛을 살펴보며 촬영 구도를 구상해보세요. 눈길을 끄는 형태를 다양한 각도에서 바라보면 여행 장면을 새롭게 발견할 수 있어요.', True),
    ('실내 촬영이 가능합니다. 전시 공간을 사진으로 기록하며 작품의 형태와 배치를 살펴보세요.', False),
], ids=['photography_experience', 'unsupported_photography_permission'])
async def test_photography_experience_is_distinct_from_unverified_permission(verified_course, copy_request, offline_place_copy, memo, accepted):
    from app.services.course_copy import enrich_course_place_copy

    copy_request.tasteProfile.activityPreference.photographyVideo = 5
    original = verified_course.model_dump()
    rows = valid_rows(verified_course)
    rows[0]['reason'] = '공간의 형태와 빛을 관찰하며 사진으로 여행을 기억하고 싶은 취향에 어울려요. 작품을 바라보는 새로운 시선을 발견해보세요.'
    rows[0]['memo'] = memo
    offline_place_copy.return_value = {'stops': rows}
    result = await enrich_course_place_copy(verified_course, copy_request)
    target = result.itinerary.days[0].stops[0]
    if accepted:
        assert target.reason == rows[0]['reason'] and target.memo == memo + HOURS_NOTE
    else:
        assert target == verified_course.itinerary.days[0].stops[0]
    assert result.itinerary.days[0].stops[1].reason == rows[1]['reason']
    assert without_place_prose(result) == without_place_prose(verified_course)
    assert verified_course.model_dump() == original
    offline_place_copy.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['provider', 'schema'])
async def test_optional_provider_or_top_level_output_error_preserves_valid_course(verified_course, copy_request, offline_place_copy, failure):
    from app.services.course_copy import enrich_course_place_copy

    if failure == 'provider':
        offline_place_copy.side_effect = RuntimeError('private provider stack trace')
    else:
        offline_place_copy.return_value = {'unexpected': 'bad top-level model output'}
    result = await enrich_course_place_copy(verified_course, copy_request)
    assert result == verified_course
    offline_place_copy.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('oversize', ['count', 'payload', 'low_budget'])
async def test_unbounded_input_or_no_optional_budget_skips_entire_call(verified_course, copy_request, offline_place_copy, oversize):
    from app.services.course_copy import enrich_course_place_copy

    budget = 12
    if oversize == 'count':
        verified_course.itinerary.days[0].stops *= 500
    elif oversize == 'payload':
        verified_course.itinerary.days[0].stops[0].place.placeName = '길고 긴 장소 ' * 20000
    else:
        budget = 0
    result = await enrich_course_place_copy(verified_course, copy_request, timeout_seconds=budget)
    assert result == verified_course
    offline_place_copy.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('external_cancel', [False, True])
async def test_timeout_or_external_cancellation_joins_model_work(verified_course, copy_request, offline_place_copy, external_cancel):
    from app.services.course_copy import enrich_course_place_copy

    started, cleaned = asyncio.Event(), asyncio.Event()
    async def blocked(payload):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
    offline_place_copy.side_effect = blocked
    task = asyncio.create_task(enrich_course_place_copy(verified_course, copy_request, timeout_seconds=12 if external_cancel else .05))
    try:
        await asyncio.wait_for(started.wait(), 1)
        if external_cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await asyncio.wait_for(task, 1) == verified_course
        assert cleaned.is_set()
        offline_place_copy.assert_awaited_once()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_real_model_boundary_is_one_bounded_structured_call_and_prompt_separates_evidence(mocker, offline_place_copy):
    from langchain_core.runnables import RunnableLambda

    captured = []
    def receive(prompt, **_kwargs):
        captured.append(prompt.to_string())
        return {'stops': []}
    model = mocker.patch('app.services.course_copy.ChatGoogleGenerativeAI')
    model.return_value.with_structured_output.return_value = RunnableLambda(receive)
    payload = {'stops': [{'day': 1, 'sequence': 1, 'placeId': 'places/a', 'placeName': '국립중앙박물관', 'category': 'museum', 'address': '대한민국 서울', 'selectionContext': ['culture']}], 'preferences': []}
    await offline_place_copy.original(payload)
    model.assert_called_once()
    kwargs = model.call_args.kwargs
    assert kwargs['max_retries'] == 0 and 0 < kwargs['max_output_tokens'] <= 16000
    assert kwargs['thinking_level'] == 'low'
    assert len(captured) == 1
    prompt = captured[0]
    assert '국립중앙박물관' in prompt and 'places/a' in prompt
    assert '설명해도 됩니다' not in prompt
    assert any(word in prompt for word in ('모델 지식', '모델 기억', '사전 지식'))
    assert any(word in prompt for word in ('추가하지', '금지', '사용하지'))
    assert '제공된' in prompt
    assert any(word in prompt for word in ('검증되지', '검증된 사실이 아', 'unverified'))
    assert any(word in prompt for word in ('지시로', '명령으로', 'instructions'))
    assert 'MBTI' in prompt and '예약' in prompt and '가격' in prompt


@pytest.mark.asyncio
async def test_structured_parse_accepts_one_oversized_row_without_losing_other_valid_copy(verified_course, copy_request, mocker, offline_place_copy):
    from langchain_core.runnables import RunnableLambda

    from app.services.course_copy import enrich_course_place_copy

    rows = valid_rows(verified_course)[:2]
    rows[0]['memo'] = '전시의 흐름을 따라 내용을 살펴보세요. ' * 100
    model = mocker.patch('app.services.course_copy.ChatGoogleGenerativeAI')
    parsed = []
    def receive(prompt, **_kwargs):
        # Reproduce the schema selected for the actual provider boundary. A
        # local text-length failure must not erase other correctly identified rows.
        schema = model.return_value.with_structured_output.call_args.args[0]
        batch = schema.model_validate({'stops': rows})
        parsed.append(batch)
        return batch
    model.return_value.with_structured_output.return_value = RunnableLambda(receive)
    offline_place_copy.side_effect = offline_place_copy.original
    original = verified_course.model_dump()
    result = await enrich_course_place_copy(verified_course, copy_request)
    assert len(parsed) == 1 and len(parsed[0].stops) == 2
    assert result.itinerary.days[0].stops[0] == verified_course.itinerary.days[0].stops[0]
    assert result.itinerary.days[0].stops[1].reason == rows[1]['reason']
    assert result.itinerary.days[0].stops[1].memo == rows[1]['memo']
    assert result.itinerary.days[1] == verified_course.itinerary.days[1]
    assert without_place_prose(result) == without_place_prose(verified_course)
    assert verified_course.model_dump() == original
    model.assert_called_once()
    offline_place_copy.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(('field', 'value'), [('day', True), ('sequence', '1'), ('day', 1.0)], ids=['boolean_day', 'string_sequence', 'float_day'])
async def test_actual_generated_schema_rejects_coercible_identity_and_retains_baseline(verified_course, copy_request, mocker, offline_place_copy, field, value):
    from langchain_core.runnables import RunnableLambda
    from pydantic import ValidationError

    from app.services.course_copy import enrich_course_place_copy

    rows = valid_rows(verified_course)[:2]
    rows[0][field] = value
    model = mocker.patch('app.services.course_copy.ChatGoogleGenerativeAI')
    schemas = []
    def receive(prompt, **_kwargs):
        schema = model.return_value.with_structured_output.call_args.args[0]
        schemas.append(schema)
        return schema.model_validate({'stops': rows})
    model.return_value.with_structured_output.return_value = RunnableLambda(receive)
    offline_place_copy.side_effect = offline_place_copy.original
    original = verified_course.model_dump()
    result = await enrich_course_place_copy(verified_course, copy_request)
    assert len(schemas) == 1
    with pytest.raises(ValidationError):
        schemas[0].model_validate({'stops': rows})
    # A malformed identity may fail the whole structured parse. Preserve all
    # baseline prose rather than applying a coerced identity to a final stop.
    assert result == verified_course and result is not verified_course
    assert verified_course.model_dump() == original
    model.assert_called_once()
    offline_place_copy.assert_awaited_once()

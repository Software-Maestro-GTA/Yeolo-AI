"""Check experience explanations and useful visit notes without paid services."""

import re
from datetime import date

import pytest

from app.schemas.course import CourseRequestSchema, DayItinerarySchema, StopSchema
from app.schemas.taste_profile import (
    ActivityPreferenceSchema,
    FoodPreferenceSchema,
    PreferredLocationTypeSchema,
    TasteProfileSchema,
    TravelPurposeSchema,
)
from app.services.course_reasons import apply_personalized_reasons


@pytest.fixture
def content_request():
    return CourseRequestSchema.model_validate({
        'userId': '550e8400-e29b-41d4-a716-446655440000', 'mbti': 'ENFP',
        'tripCondition': {'destinationCountry': '일본', 'destinationCity': '도쿄',
                          'startDate': '2026-10-05', 'totalDays': 1, 'budgetType': 'moderate'},
    })


def profile_with_scores(**scores):
    payload = {
        key: dict.fromkeys(model.model_fields, 1)
        for key, model in {
            'travelPurpose': TravelPurposeSchema, 'activityPreference': ActivityPreferenceSchema,
            'foodPreference': FoodPreferenceSchema, 'preferredLocationType': PreferredLocationTypeSchema,
        }.items()
    }
    payload.update(travelPaceDensity='balanced', spendingTendency='moderate', companionType='friends')
    for path, value in scores.items():
        section, field = path.split('__')
        payload[section][field] = value
    return TasteProfileSchema.model_validate(payload)


def stop_with_type(category, index=1):
    return StopSchema.model_validate({
        'sequence': index, 'arrivalTime': '10:15', 'stayMinutes': 95, 'cost': 17000,
        'memo': '검증된 방문 메모', 'reason': '유명하고 조용한 숨은 명소이며 예약 없이 무료 입장 가능',
        'place': {'placeId': f'verified-{index}', 'placeName': f'검증 장소 {index}',
                  'category': category, 'latitude': 35.67, 'longitude': 139.76},
        'transportToNext': {'type': 'walking', 'distance': 700, 'minutes': 12, 'cost': 0,
                            'memo': '확인된 경로'},
    })


def content_day(*categories):
    return DayItinerarySchema(day=1, date='2026-10-05', memo='기존 일정 안내',
                              stops=[stop_with_type(category, index) for index, category in enumerate(categories, 1)])


def assert_experience_only(text):
    assert not re.search(r'\d{2}:\d{2}|\d+\s*(분|일|곳)|17000', text)
    assert not any(phrase in text for phrase in ['도착', '체류', '이동 시간', '예산', '비용', '가격', '촘촘한 일정'])
    assert not any(claim in text for claim in ['외향', '모험적', 'ENFP', '관심이 많은', '숨은 명소', '유명', '조용', '무료 입장', '예약 없이'])


@pytest.mark.parametrize(('category', 'experience_words'), [
    ('museum', ('전시', '관람', '작품')),
    ('shinto_shrine', ('신사',)), ('buddhist_temple', ('사찰',)),
    ('park', ('산책',)), ('national_park', ('산책', '탐방')),
    ('observation_deck', ('전망',)), ('convention_center', ('현장', '안내')),
    ('tourist_attraction', ('둘러', '살펴', '방문')),
    ('japanese_restaurant', ('일식', '일본 음식')),
    ('ramen_restaurant', ('라멘',)), ('hamburger_restaurant', ('햄버거',)),
    ('tonkatsu_restaurant', ('돈카츠', '돈가스', '돈까스')),
    ('sushi_restaurant', ('초밥', '스시')),
    ('restaurant', ('식사', '음식')),
])
def test_mbti_only_explains_verified_type_experience(content_request, category, experience_words):
    day = content_day(category)
    original = day.stops[0].model_dump()
    summary = apply_personalized_reasons(content_request, [day])
    reason = day.stops[0].reason
    assert any(word in reason for word in experience_words)
    assert any(word in summary for word in experience_words)
    assert day.stops[0].place.placeName in reason
    assert_experience_only(reason)
    assert_experience_only(summary)
    assert not any(claim in reason for claim in ['선호', '취향', '좋아하'])
    assert {key: value for key, value in day.stops[0].model_dump().items() if key != 'reason'} == {
        key: value for key, value in original.items() if key != 'reason'
    }


@pytest.mark.parametrize('pace', ['slow_stay', 'long_stay', 'dense_schedule'])
def test_pace_does_not_replace_place_experience_with_schedule(content_request, pace):
    content_request.tasteProfile = profile_with_scores()
    content_request.tasteProfile.travelPaceDensity = pace
    day = content_day(*(['museum'] * 6))
    summary = apply_personalized_reasons(content_request, [day])
    for stop in day.stops:
        assert_experience_only(stop.reason)
        assert any(word in stop.reason for word in ['전시', '작품', '관람'])
    assert_experience_only(summary)
    assert not any(absent in summary for absent in ['산책', '미식', '식사', '자연 탐방'])


def test_strongest_actual_preference_explains_verified_museum_experience(content_request):
    content_request.tasteProfile = profile_with_scores(travelPurpose__culturalExperience=4, activityPreference__viewing=5)
    day = content_day('museum')
    apply_personalized_reasons(content_request, [day])
    assert '관람' in day.stops[0].reason
    assert any(word in day.stops[0].reason for word in ['전시', '작품', '박물관'])
    assert_experience_only(day.stops[0].reason)


def test_unknown_category_and_unmatched_preferences_do_not_invent_experience(content_request):
    content_request.tasteProfile = profile_with_scores(travelPurpose__natureExploration=5, foodPreference__dietaryRestriction=5)
    day = content_day('unknown_provider_type')
    summary = apply_personalized_reasons(content_request, [day])
    for text in [summary, day.stops[0].reason]:
        assert_experience_only(text)
        assert not any(claim in text for claim in ['선호', '취향', '산책', '전시', '알레르기', '자연', '식사', '휴식'])
        assert any(action in text for action in ['둘러', '살펴', '방문'])


def test_general_restaurant_does_not_infer_cuisine_or_other_absent_experiences(content_request):
    content_request.tasteProfile = profile_with_scores(travelPurpose__gourmet=5, foodPreference__localFoodActive=5)
    day = content_day('restaurant', 'restaurant')
    summary = apply_personalized_reasons(content_request, [day])
    for text in [summary, *[stop.reason for stop in day.stops]]:
        assert_experience_only(text)
        assert not any(claim in text for claim in ['현지 음식', '일식', '라멘', '초밥', '시그니처', '산책', '전시', '전망'])
        assert any(word in text for word in ['미식', '식사', '음식'])


@pytest.mark.parametrize(('category', 'action_words'), [
    ('museum', ('관람', '전시')), ('shinto_shrine', ('안내', '예절')),
    ('buddhist_temple', ('안내', '예절')), ('park', ('산책', '동선')),
    ('national_park', ('산책', '탐방', '동선')), ('observation_deck', ('전망', '안내')),
    ('convention_center', ('방문 안내', '현장 안내', '행사 안내')),
    ('tourist_attraction', ('안내', '동선')),
    ('japanese_restaurant', ('메뉴', '재료', '양')), ('ramen_restaurant', ('메뉴', '재료', '양')),
    ('hamburger_restaurant', ('메뉴', '재료', '양')), ('tonkatsu_restaurant', ('메뉴', '재료', '양')),
    ('sushi_restaurant', ('메뉴', '재료', '양')), ('restaurant', ('메뉴', '재료', '양')),
])
def test_schedule_memo_has_type_action_and_preserves_numeric_cost(category, action_words):
    from app.agent.course_graph import Candidate, _schedule_day
    from app.agent.tools.verified_maps import VerifiedPlace

    venue = VerifiedPlace(stop_with_type(category).place, periods=None)
    day = _schedule_day([(Candidate(name=venue.place.placeName, stay_minutes=60, cost=17000), venue)], [], date(2026, 10, 5), 1)
    stop = day.stops[0]
    assert any(action in stop.memo for action in action_words)
    assert any(action in stop.memo for action in ['확인', '선택', '살펴', '보고', '지켜'])
    assert '영업시간 미확인' in stop.memo
    assert not any(claim in stop.memo for claim in ['계획용 추정치', '실제 가격', '유명', '조용', '무료', '예약 필수', '촬영 가능', '행사가 열려'])
    assert stop.arrivalTime == '09:00' and stop.stayMinutes == 60 and stop.cost == 17000
    assert stop.transportToNext.type == 'none'


@pytest.mark.parametrize('periods', [
    [{'open': {'day': 1, 'hour': 8}, 'close': {'day': 1, 'hour': 18}}],
    [{'open': {'day': 0, 'hour': 0}}],
    [{'open': {'day': 0, 'hour': 20}, 'close': {'day': 1, 'hour': 12}}],
])
def test_known_hours_do_not_repeat_blanket_reservation_or_false_closing_notice(periods):
    from app.agent.course_graph import Candidate, _schedule_day
    from app.agent.tools.verified_maps import VerifiedPlace

    venue = VerifiedPlace(stop_with_type('museum').place, periods=periods)
    day = _schedule_day([(Candidate(name=venue.place.placeName, stay_minutes=60), venue)], [], date(2026, 10, 5), 1)
    memo = day.stops[0].memo
    assert any(action in memo for action in ['관람', '전시'])
    assert not any(blanket in memo for blanket in ['예약 가능 여부', '공휴일·임시휴무', '영업시간 미확인', '마감', '최종 입장'])


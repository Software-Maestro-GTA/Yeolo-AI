"""Check preference evidence and schedules without visit-copy templates."""

from datetime import date

import pytest

from app.agent.course_state import Candidate
from app.agent.course_state import ScheduledStopSchema as StopSchema
from app.agent.tools.verified_maps import InternalPlaceSchema, VerifiedPlace
from app.schemas.course import DayItinerarySchema
from app.schemas.taste_profile import (
    ActivityPreferenceSchema,
    FoodPreferenceSchema,
    PreferredLocationTypeSchema,
    TasteProfileSchema,
    TravelPurposeSchema,
)
from app.services.course_planning import _schedule_day
from app.services.course_reasons import preference_evidence


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
        'place': InternalPlaceSchema(placeId=f'verified-{index}', placeName=f'검증 장소 {index}',
                                     category=category, latitude=35.67, longitude=139.76),
        'transportToNext': {'type': 'walking', 'distance': 700, 'minutes': 12,
                            'memo': '확인된 경로'},
    })


def content_day(*categories):
    return DayItinerarySchema(day=1, date='2026-10-05',
                              stops=[stop_with_type(category, index) for index, category in enumerate(categories, 1)])



@pytest.mark.parametrize('score', [1, 2, 3, 4, 5])
def test_only_explicit_high_compatible_scores_become_evidence(score):
    profile = profile_with_scores(travelPurpose__culturalExperience=score, travelPurpose__natureExploration=5)
    evidence = preference_evidence(profile, 'museum')
    assert [(value, rule.label) for value, rule in evidence] == ([(score, '문화 체험')] if score >= 4 else [])
    assert preference_evidence(profile, 'unknown_provider_type') == []


@pytest.mark.parametrize('category', ['restaurant', 'ramen_restaurant', 'sushi_restaurant'])
def test_food_types_share_gourmet_evidence_without_claiming_dietary_safety(category):
    profile = profile_with_scores(travelPurpose__gourmet=5, foodPreference__dietaryRestriction=5)
    assert [rule.label for _, rule in preference_evidence(profile, category)] == ['미식']


@pytest.mark.parametrize('category', ['museum', 'park', 'ramen_restaurant', 'buddhist_temple', 'unknown_provider_type'])
def test_schedule_discards_draft_prose_and_does_not_create_visit_tips(category):
    venue = VerifiedPlace(stop_with_type(category).place)
    candidate = Candidate(name=venue.place.placeName, stay_minutes=60, cost=17000,
                          reason='유명한 옥상 정원에서 왕실 유물을 무료로 관람하세요')
    day = _schedule_day([(candidate, venue)], [], date(2026, 10, 5), 1)
    stop = day.stops[0]
    assert stop.memo == stop.reason == ''
    assert stop.arrivalTime == '09:00' and stop.stayMinutes == 60 and stop.cost == 17000
    assert stop.transportToNext.type == 'none'


@pytest.mark.parametrize('periods', [
    [{'open': {'day': 1, 'hour': 8}, 'close': {'day': 1, 'hour': 18}}],
    [{'open': {'day': 0, 'hour': 0}}],
    [{'open': {'day': 0, 'hour': 20}, 'close': {'day': 1, 'hour': 12}}],
])
def test_known_opening_hours_do_not_create_template_copy(periods):
    venue = VerifiedPlace(stop_with_type('museum').place, periods=periods)
    day = _schedule_day([(Candidate(name=venue.place.placeName, stay_minutes=60), venue)], [], date(2026, 10, 5), 1)
    assert day.stops[0].memo == day.stops[0].reason == ''

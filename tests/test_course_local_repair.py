"""Verify original-order five-place recovery before new Maps or model calls."""

from datetime import date
from unittest.mock import AsyncMock

import pytest

from app.agent.course_state import Candidate, CourseDraft, DraftDay
from app.agent.tools.verified_maps import (
    Destination,
    InternalPlaceSchema,
    InternalTransportSchema,
    VerifiedPlace,
)
from app.schemas.course import CourseRequestSchema
from app.services.course_history import CourseHistory
from app.services.course_planning import _day_plans, _proposal_floor, _schedule_day


def retained_visits():
    roles = [('a', 'none', 90), ('lunch', 'lunch', 60), ('b', 'none', 90), ('dinner', 'dinner', 60), ('c', 'none', 90), ('breakfast', 'breakfast', 60)]
    return [
        (Candidate(name=name, meal=meal, stay_minutes=stay), VerifiedPlace(InternalPlaceSchema(
            placeId='places/' + name, placeName=name, category='museum' if meal == 'none' else 'restaurant',
            address='대한민국 서울', latitude=37.57, longitude=126.98,
        ))) for name, meal, stay in roles
    ]


def actual_route():
    return InternalTransportSchema(type='transit', distance=1500, minutes=50, cost=None)


def test_original_floor_keeps_three_core_and_two_explicit_meals_in_proposal_order():
    visits = retained_visits()
    floor = _proposal_floor(visits, date(2027, 1, 10))
    assert [candidate.name for candidate, _ in floor] == ['a', 'lunch', 'b', 'dinner', 'c']
    assert all(item is visits[index] for item, index in zip(floor, [0, 1, 2, 3, 4], strict=True))
    day = _schedule_day(floor, [actual_route()] * 4, date(2027, 1, 10), 1)
    assert len(day.stops) == 5 and day.stops[-1].arrivalTime == '19:30'
    assert all(stop.stayMinutes == 90 for stop in day.stops if stop.place.category == 'museum')


@pytest.mark.parametrize('invalid', ['duplicate_id', 'missing_id', 'missing_core', 'missing_meal', 'wrong_meal_category', 'reversed_meals', 'closed_hours', 'stay_too_long'])
def test_original_floor_never_invents_or_loosens_invalid_visit_constraints(invalid):
    visits = retained_visits()
    if invalid == 'duplicate_id':
        visits[4][1].place.placeId = 'a'
    elif invalid == 'missing_id':
        visits[0][1].place.placeId = ''
    elif invalid == 'missing_core':
        visits[4][1].place.category = 'coffee_shop'
    elif invalid == 'missing_meal':
        visits[3][0].meal = 'none'
    elif invalid == 'wrong_meal_category':
        visits[1][1].place.category = 'museum'
    elif invalid == 'reversed_meals':
        visits[1], visits[3] = visits[3], visits[1]
    elif invalid == 'closed_hours':
        visits[0] = (visits[0][0], VerifiedPlace(visits[0][1].place, periods=[]))
    else:
        for candidate, venue in visits:
            if venue.place.category == 'museum':
                candidate.stay_minutes = 240
    assert _proposal_floor(visits, date(2027, 1, 10)) == []


@pytest.mark.asyncio
async def test_six_queried_candidates_recover_safe_five_without_expansion_or_another_model(mocker, tmp_path):
    from app.agent.course_graph import build_course_graph

    request = CourseRequestSchema.model_validate({
        'userId': '550e8400-e29b-41d4-a716-446655440000', 'language': 'ko', 'mbti': 'INTJ',
        'tripCondition': {'destinationCountry': '대한민국', 'destinationCity': '서울', 'startDate': '2027-01-10', 'totalDays': 1, 'budget': 300000},
    })
    visits = retained_visits()
    plans = _day_plans(visits, date(2027, 1, 10), 6, 3, set(), daily_budget=300000)
    assert plans
    for plan in plans:
        with pytest.raises(ValueError):
            _schedule_day(plan, [actual_route()] * (len(plan) - 1), date(2027, 1, 10), 1)
    llm = mocker.patch('app.agent.course_nodes.draft_candidates', new_callable=AsyncMock,
                       return_value=CourseDraft(title='서울 여행', reason='문화 체험', days=[DraftDay(candidates=[candidate for candidate, _ in visits])]))
    provider = mocker.Mock()
    provider.resolve_destination = AsyncMock(return_value=Destination('KR', 37.3, 126.7, 37.8, 127.3))
    facts = {candidate.name: venue for candidate, venue in visits}
    provider.search = AsyncMock(side_effect=lambda candidate, destination: facts[candidate.name])
    provider.route = AsyncMock(return_value=actual_route())
    updates = [update async for update in build_course_graph(provider, CourseHistory(tmp_path / 'history.sqlite3')).astream(
        {'request': request, 'attempt': 0}, stream_mode='updates',
    )]
    course = next(update['finalize_course']['course'] for update in updates if 'finalize_course' in update)
    assert [stop.place.placeName for stop in course.itinerary.days[0].stops] == ['a', 'lunch', 'b', 'dinner', 'c']
    assert len(course.itinerary.days[0].stops) == 5
    assert provider.search.await_count == 6
    llm.assert_awaited_once()
    assert not any(any(name in update for name in ('expand_places', 'refill', 'repair')) for update in updates)

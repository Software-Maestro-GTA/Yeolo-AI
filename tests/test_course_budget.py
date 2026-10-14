"""Verify soft budget choices and arithmetic use only known numeric KRW costs."""

from datetime import date
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agent.course_state import Candidate
from app.agent.tools.verified_maps import (
    InternalPlaceSchema,
    InternalTransportSchema,
    VerifiedPlace,
)
from app.schemas.course import CourseRequestSchema
from app.services.course_budget import budget_rank, course_cost, daily_place_budget
from app.services.course_planning import _day_plans, _schedule_day, _visit_shortage
from app.services.myrealtrip_quotes import QuoteItem, TripQuotes


def request(budget=300000, days=3):
    return CourseRequestSchema.model_validate({
        'userId': '550e8400-e29b-41d4-a716-446655440000', 'language': 'ko', 'mbti': 'INTJ',
        'tripCondition': {
            'destinationCountry': '대한민국', 'destinationCity': '서울',
            'startDate': '2026-10-15', 'totalDays': days, 'budget': budget,
        },
    })


def visit(name, cost, meal='none'):
    return (
        Candidate(name=name, meal=meal, cost=cost, stay_minutes=30),
        VerifiedPlace(InternalPlaceSchema(
            placeId=name, placeName=name, category='museum' if meal == 'none' else 'restaurant',
            address='대한민국 서울', latitude=37.57, longitude=126.98,
        )),
    )


def alternatives():
    return [
        visit('paid-a', 20000), visit('lunch', 10000, 'lunch'),
        visit('paid-b', 20000), visit('dinner', 10000, 'dinner'),
        visit('paid-c', 20000), visit('free-a', 0), visit('free-b', 0), visit('free-c', 0),
    ]


@pytest.mark.parametrize(('budget', 'expected'), [
    (None, (False, 0.0)), (10000, (False, 0.0)), (5000, (False, 0.0)),
    (4000, (True, 1000.0)), (0, (True, 5000.0)),
])
def test_budget_rank_is_soft_and_within_budget_options_tie(budget, expected):
    assert budget_rank([visit('museum', 5000)], budget) == expected
    assert budget_rank([visit('free-museum', 0)], 10000) == budget_rank([visit('paid-museum', 9999)], 10000)


@pytest.mark.parametrize('invalid', [float('nan'), float('inf'), float('-inf'), -1])
def test_nonfinite_or_negative_budget_targets_are_rejected(invalid):
    with pytest.raises(ValueError, match='Invalid daily budget'):
        budget_rank([visit('museum', 5000)], invalid)
    with pytest.raises(ValidationError):
        request(budget=invalid)


def test_budget_prefers_complete_affordable_plan_and_original_order_cannot_override_it():
    choices = alternatives()
    original = _day_plans(choices, date(2026, 10, 15), 5, 3, set())[0]
    assert sum(candidate.cost for candidate, _ in original) == 80000
    plans = _day_plans(choices, date(2026, 10, 15), 5, 3, set(), daily_budget=20000)
    best = plans[0]
    assert budget_rank(best, 20000) == (False, 0.0)
    assert sum(candidate.cost for candidate, _ in best) == 20000
    assert _visit_shortage(best) == (0, 0)
    day = _schedule_day(best, [InternalTransportSchema(type='walking', distance=100, minutes=2, cost=0)] * 4, date(2026, 10, 15), 1)
    assert len(day.stops) == 5
    assert all(stop.arrivalTime for stop in day.stops)
    wire = day.model_dump(mode='json')
    assert 'memo' not in wire
    assert all('arrivalTime' not in stop for stop in wire['stops'])
    assert wire['stops'][-1]['transportToNext'] == {'type': 'none', 'distance': 0.0, 'minutes': 0, 'memo': None}
    assert all('영업시간 미확인' not in stop.memo for stop in day.stops)


def test_low_budget_never_discards_a_valid_five_place_course():
    plans = _day_plans(alternatives(), date(2026, 10, 15), 5, 3, set(), daily_budget=1)
    assert plans and _visit_shortage(plans[0]) == (0, 0)
    assert budget_rank(plans[0], 1) == (True, 19999.0)


def test_visit_floor_stays_ahead_of_cheaper_four_place_plan():
    choices = [visit('a', 0), visit('lunch', 1000, 'lunch'), visit('b', 0), visit('dinner', 1000, 'dinner'), visit('c', 50000)]
    plans = _day_plans(choices, date(2026, 10, 15), 5, 2, set(), daily_budget=2000)
    assert len(plans[0]) == 5 and _visit_shortage(plans[0]) == (0, 0)
    assert sum(candidate.cost for candidate, _ in plans[0]) == 52000


@pytest.mark.parametrize(('flight', 'lodging', 'expected'), [
    (QuoteItem('available', 100000), QuoteItem('available', 50000), 50000),
    (QuoteItem('available', 100000), QuoteItem('unavailable'), 200000 / 3),
    (QuoteItem('unavailable'), QuoteItem('unavailable'), 100000),
    (QuoteItem('not_applicable'), QuoteItem('available', 60000), 80000),
    (QuoteItem('available', 400000), QuoteItem('unavailable'), 0),
])
def test_remaining_daily_budget_subtracts_only_known_quotes(flight, lodging, expected):
    assert daily_place_budget(request(), TripQuotes(flight, lodging)) == pytest.approx(expected)
    assert daily_place_budget(request(), None) == 100000


@pytest.mark.parametrize('unknown_fare', [None, float('nan'), float('inf'), -1, True, '2000'])
def test_cost_subtotal_includes_known_route_fare_and_never_invents_unknown_fare(unknown_fare):
    days = [SimpleNamespace(stops=[
        SimpleNamespace(cost=12000, transportToNext=SimpleNamespace(cost=2000)),
        SimpleNamespace(cost=8000, transportToNext=SimpleNamespace(cost=unknown_fare)),
        SimpleNamespace(cost=0, transportToNext=SimpleNamespace()),
    ])]
    assert course_cost(days) == 22000


def test_schedule_cost_uses_internal_known_fares_before_public_serialization():
    selected = [visit('museum', 10000), visit('restaurant', 12000, 'lunch')]
    day = _schedule_day(selected, [InternalTransportSchema(type='transit', distance=800, minutes=10, cost=1500)], date(2026, 10, 15), 1)
    assert course_cost([day]) == 23500
    assert 'cost' not in day.model_dump(mode='json')['stops'][0]['transportToNext']

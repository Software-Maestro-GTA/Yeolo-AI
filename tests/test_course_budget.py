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
from app.services.course_budget import (
    budget_guidance,
    budget_rank,
    course_cost,
    daily_place_budget,
    proposal_budget_rank,
)
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


def visit(name, cost, meal='none', tier='standard'):
    return (
        Candidate(name=name, meal=meal, cost=cost, budget_tier=tier, stay_minutes=30),
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
    (None, (False, 0.0, 0.0, 0)), (10000, (False, 0.0, 0.0, -1)),
    (5000, (False, 0.0, 0.0, -1)), (4000, (True, 1000.0, 0.0, -1)),
    (0, (True, 5000.0, 0.0, -1)),
])
def test_budget_rank_keeps_over_budget_options_eligible(budget, expected):
    assert budget_rank([visit('museum', 5000)], budget) == expected


def test_higher_price_does_not_win_within_the_same_spending_band():
    assert budget_rank([visit('museum', 5000)], 100000) == budget_rank([visit('museum', 90000)], 100000)
    anchor = visit('premium-experience', 50000, tier='premium')
    assert budget_rank([anchor, visit('free-park', 0)], 500000) == budget_rank(
        [anchor, visit('another-expensive-activity', 400000, tier='premium')], 500000
    )


@pytest.mark.parametrize('invalid', [float('nan'), float('inf'), float('-inf'), -1])
def test_nonfinite_or_negative_budget_targets_are_rejected(invalid):
    with pytest.raises(ValueError, match='Invalid daily budget'):
        budget_rank([visit('museum', 5000)], invalid)
    with pytest.raises(ValueError, match='Invalid daily budget'):
        proposal_budget_rank([visit('museum', 5000)[0]], invalid)
    with pytest.raises(ValidationError):
        request(budget=invalid)


def test_budget_prefers_complete_affordable_plan_and_original_order_cannot_override_it():
    choices = alternatives()
    original = _day_plans(choices, date(2026, 10, 15), 5, 3, set())[0]
    assert sum(candidate.cost for candidate, _ in original) == 80000
    plans = _day_plans(choices, date(2026, 10, 15), 5, 3, set(), daily_budget=20000)
    best = plans[0]
    assert budget_rank(best, 20000)[:2] == (False, 0.0)
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
    assert budget_rank(plans[0], 1)[:2] == (True, 19999.0)


def test_visit_floor_stays_ahead_of_cheaper_four_place_plan():
    choices = [visit('a', 0), visit('lunch', 1000, 'lunch'), visit('b', 0), visit('dinner', 1000, 'dinner'), visit('c', 50000)]
    plans = _day_plans(choices, date(2026, 10, 15), 5, 2, set(), daily_budget=2000)
    assert len(plans[0]) == 5 and _visit_shortage(plans[0]) == (0, 0)
    assert sum(candidate.cost for candidate, _ in plans[0]) == 52000


def estimated_cost(amount):
    return QuoteItem('estimated', amount, source='offline-budget-test', reference_date='2026-10-19')


@pytest.mark.parametrize(('flight', 'lodging', 'expected'), [
    (QuoteItem('available', 100000), QuoteItem('available', 50000), 50000),
    (QuoteItem('available', 100000), estimated_cost(75000), 125000 / 3),
    (estimated_cost(150000), estimated_cost(60000), 30000),
    (QuoteItem('not_applicable'), QuoteItem('available', 60000), 80000),
    (QuoteItem('available', 400000), estimated_cost(60000), 0),
])
def test_daily_allowance_subtracts_resolved_quotes_and_estimates_once(flight, lodging, expected):
    assert daily_place_budget(request(), TripQuotes(flight, lodging)) == pytest.approx(expected)
    assert daily_place_budget(request(), None) == 100000


def test_not_applicable_travel_components_do_not_reserve_activity_budget():
    quotes = TripQuotes(QuoteItem('not_applicable'), QuoteItem('not_applicable'))
    assert daily_place_budget(request(days=1), quotes) == 300000
    assert daily_place_budget(request(days=1)) == 300000
    # The product allocation never turns an unknown quote into a stated price.
    unknown = TripQuotes(QuoteItem('unavailable'), QuoteItem('unavailable'))
    daily_place_budget(request(), unknown)
    assert unknown.total_krw == 0


def test_guidance_uses_destination_and_allowance_without_global_price_bands():
    low, high = request(500000), request(10000000)
    low_daily, high_daily = daily_place_budget(low), daily_place_budget(high)
    cheap = [visit('local-lunch', 10000, 'lunch', 'economy')[0],
             visit('local-dinner', 15000, 'dinner', 'economy')[0]]
    premium = [visit('special-lunch', 100000, 'lunch', 'premium')[0],
               visit('special-dinner', 150000, 'dinner', 'premium')[0]]
    assert proposal_budget_rank(cheap, low_daily) < proposal_budget_rank(premium, low_daily)
    assert proposal_budget_rank(premium, high_daily) < proposal_budget_rank(cheap, high_daily)
    for candidate_request in (low, high):
        guidance = budget_guidance(candidate_request)
        assert '대한민국 / 서울' in guidance
        assert f'{daily_place_budget(candidate_request):.0f}' in guidance
        assert '해당 도시' in guidance
        assert 'budget_tier:' not in guidance
    assert '부풀리거나' in budget_guidance(high)


@pytest.mark.parametrize(('price_multiplier', 'expected_meal_tier'), [(1, 'premium'), (10, 'economy')])
def test_same_budget_buys_different_local_levels_without_modifying_candidate_prices(price_multiplier, expected_meal_tier):
    choices = [visit('free-a', 0), visit('free-b', 0), visit('free-c', 0)]
    for meal in ('lunch', 'dinner'):
        choices.extend([
            visit(f'value-{meal}', 2000 * price_multiplier, meal, 'economy'),
            visit(f'premium-{meal}', 15000 * price_multiplier, meal, 'premium'),
        ])
    original_costs = {candidate.name: candidate.cost for candidate, _ in choices}
    best = _day_plans(choices, date(2026, 10, 15), 5, 3, set(), daily_budget=50000)[0]
    assert {candidate.budget_tier for candidate, _ in best if candidate.meal != 'none'} == {expected_meal_tier}
    assert len(best) == 5 and _visit_shortage(best) == (0, 0)
    assert all(candidate.cost == original_costs[candidate.name] for candidate, _ in best)


def test_affordable_premium_dinner_does_not_require_a_premium_lunch():
    choices = [visit('free-a', 0), visit('free-b', 0), visit('free-c', 0),
               visit('local-lunch', 5000, 'lunch', 'economy'),
               visit('local-dinner', 5000, 'dinner', 'economy'),
               visit('special-dinner', 30000, 'dinner', 'premium')]
    best = _day_plans(choices, date(2026, 10, 15), 5, 3, set(), daily_budget=50000)[0]
    assert {candidate.name for candidate, _ in best if candidate.meal != 'none'} == {'local-lunch', 'special-dinner'}
    assert all(candidate.cost == 0 for candidate, _ in best if candidate.meal == 'none')


def test_affordability_compares_joint_meals_before_their_tiers():
    cheap = [visit('value-lunch', 10000, 'lunch', 'economy')[0],
             visit('value-dinner', 10000, 'dinner', 'economy')[0]]
    premium = [visit('special-lunch', 40000, 'lunch', 'premium')[0],
               visit('special-dinner', 40000, 'dinner', 'premium')[0]]
    assert all(candidate.cost < 50000 for candidate in premium)
    assert proposal_budget_rank(cheap, 50000) < proposal_budget_rank(premium, 50000)


def test_only_verified_tourism_counts_as_the_selected_paid_highlight():
    restaurant = visit('not-a-tourism-experience', 10000, tier='premium')
    restaurant[1].place.category = 'restaurant'
    assert proposal_budget_rank([restaurant[0]], 50000)[3] == -3
    assert budget_rank([restaurant], 50000)[3] == 0


def test_budget_changes_selected_experiences_without_removing_free_highlights():
    choices = [
        visit('free-a', 0), visit('local-lunch', 10000, 'lunch', 'economy'),
        visit('free-b', 0), visit('local-dinner', 15000, 'dinner', 'economy'),
        visit('free-c', 0), visit('special-lunch', 100000, 'lunch', 'premium'),
        visit('special-dinner', 150000, 'dinner', 'premium'),
        visit('special-experience', 80000, tier='premium'),
    ]
    low = _day_plans(choices, date(2026, 10, 15), 5, 3, set(), daily_budget=50000)[0]
    high = _day_plans(choices, date(2026, 10, 15), 5, 3, set(), daily_budget=500000)[0]
    assert {candidate.name for candidate, _ in low} == {
        'free-a', 'free-b', 'free-c', 'local-lunch', 'local-dinner',
    }
    assert {'special-lunch', 'special-dinner', 'special-experience'} <= {
        candidate.name for candidate, _ in high
    }
    assert len(high) == 5 and sum(candidate.cost == 0 for candidate, _ in high) == 2
    assert sum(candidate.cost for candidate, _ in high) == 330000
    assert _visit_shortage(low) == _visit_shortage(high) == (0, 0)


@pytest.mark.parametrize('experience_minutes', [120, 180])
def test_longer_premium_meals_and_experiences_survive_bounded_search(experience_minutes):
    choices = [
        visit('free-a', 0), visit('standard-lunch', 10000, 'lunch'),
        visit('free-b', 0), visit('standard-dinner', 15000, 'dinner'),
        visit('free-c', 0), visit('special-lunch', 100000, 'lunch', 'premium'),
        visit('special-dinner', 150000, 'dinner', 'premium'),
        visit('special-experience', 80000, tier='premium'),
    ]
    for candidate, _ in choices:
        if candidate.budget_tier == 'premium':
            candidate.stay_minutes = experience_minutes if candidate.meal == 'none' else 90
    plans = _day_plans(choices, date(2026, 10, 15), 5, 3, set(), daily_budget=500000)
    best = plans[0]
    assert {'special-lunch', 'special-dinner', 'special-experience'} <= {
        candidate.name for candidate, _ in best
    }
    assert len(best) == 5 and _visit_shortage(best) == (0, 0)
    assert sum(candidate.cost for candidate, _ in best) == 330000
    # The preference is valid with real travel time, not merely zero-travel search.
    day = _schedule_day(
        best, [InternalTransportSchema(type='walking', distance=100, minutes=2)] * 4,
        date(2026, 10, 15), 1,
    )
    assert len(day.stops) == 5


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

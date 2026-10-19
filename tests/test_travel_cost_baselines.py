"""Check complete travel estimates without network calls or budget-shaped prices."""

from dataclasses import replace

import pytest

from app.agent.tools.verified_maps import Destination
from app.schemas.course import CourseRequestSchema
from app.services.myrealtrip_quotes import (
    AirportIdentity,
    LodgingIdentity,
    QuoteItem,
    TripQuotes,
)
from app.services.travel_cost_baselines import (
    BASELINE_REFERENCE_DATE,
    BASELINE_VERSION,
    baseline_trip_quotes,
    merge_trip_quotes,
)


def request(*, budget: int = 500_000, days: int = 3, city: str = '도쿄') -> CourseRequestSchema:
    return CourseRequestSchema.model_validate({
        'userId': '550e8400-e29b-41d4-a716-446655440000', 'language': 'ko', 'mbti': 'INTJ',
        'tripCondition': {
            'destinationCountry': '일본', 'destinationCity': city,
            'startDate': '2027-01-10', 'totalDays': days, 'budget': budget,
        },
    })


def destination(country: str = 'JP', city: str = 'Tokyo') -> Destination:
    return Destination(country, 35.0, 139.0, 36.0, 140.0, verified_city_name=city)


def test_missing_overseas_quotes_include_return_air_and_all_nights():
    quotes = baseline_trip_quotes(request(), destination())
    assert quotes.transport_mode == 'air'
    assert quotes.flight.amount_krw == 350_000
    assert quotes.lodging.amount_krw == 180_000
    assert quotes.total_krw == 530_000
    assert quotes.estimated_items == ('flight', 'lodging')
    for item in (quotes.flight, quotes.lodging):
        assert item.status == 'estimated'
        assert item.source == f'{BASELINE_VERSION}:JP/tokyo'
        assert item.reference_date == BASELINE_REFERENCE_DATE


def test_budget_selects_fixed_product_levels_without_scaling_their_prices():
    low = baseline_trip_quotes(request(budget=500_000), destination())
    nearby = baseline_trip_quotes(request(budget=600_000), destination())
    high = baseline_trip_quotes(request(budget=10_000_000), destination())
    assert low.flight.amount_krw == nearby.flight.amount_krw == 350_000
    assert low.lodging.amount_krw == nearby.lodging.amount_krw == 180_000
    assert high.flight.amount_krw == 1_200_000
    assert high.lodging.amount_krw == 800_000
    assert high.flight.reason == 'air_round_trip_business'
    assert high.lodging.reason == 'one_room_2_nights_premium'


def test_too_small_or_zero_budget_preserves_required_costs():
    zero = baseline_trip_quotes(request(budget=0), destination())
    small = baseline_trip_quotes(request(budget=10_000), destination())
    assert zero == small
    assert zero.total_krw == 530_000


def test_city_policy_is_country_scoped_and_verified_name_wins():
    actual_tokyo = baseline_trip_quotes(request(city='런던'), destination())
    different_country = baseline_trip_quotes(request(city='도쿄'), destination('US', 'Tokyo'))
    assert actual_tokyo.flight.source.endswith(':JP/tokyo')
    assert different_country.flight.source.endswith(':US')
    assert different_country.flight.amount_krw == 1_400_000


def test_verified_iso_country_does_not_come_from_request_country_text():
    supplied = request()
    supplied.tripCondition.destinationCountry = '대한민국'
    quotes = baseline_trip_quotes(supplied, destination())
    assert quotes.transport_mode == 'air'
    assert quotes.flight.source.endswith(':JP/tokyo')


@pytest.mark.parametrize(('country', 'city', 'expected_source', 'flight'), [
    ('VN', 'Da Nang', 'VN/danang', 400_000),
    ('VN', 'Đà Nẵng', 'VN/danang', 400_000),
    ('JP', 'Ｏｓａｋａ', 'JP/osaka', 300_000),
    ('JP', 'Unknown City', 'JP', 350_000),
    ('ZZ', 'Unknown City', 'global', 2_000_000),
])
def test_exact_normalization_and_fallbacks_keep_required_amounts(country, city, expected_source, flight):
    quotes = baseline_trip_quotes(request(), destination(country, city))
    assert quotes.flight.source == f'{BASELINE_VERSION}:{expected_source}'
    assert quotes.flight.amount_krw == flight
    assert quotes.lodging.amount_krw > 0


def test_request_city_only_selects_policy_when_verified_city_is_absent():
    fallback = baseline_trip_quotes(request(city='도쿄'), destination(city=''))
    verified_other = baseline_trip_quotes(request(city='도쿄'), destination(city='Kyoto'))
    assert fallback.flight.source.endswith(':JP/tokyo')
    assert verified_other.flight.source.endswith(':JP')


@pytest.mark.parametrize(('city', 'mode', 'amount'), [
    ('Seoul', 'none', None),
    ('서울특별시', 'none', None),
    ('Busan', 'ground', 120_000),
    ('Daejeon', 'ground', 100_000),
    ('Jeju', 'air', 180_000),
    ('Seogwipo-si', 'air', 180_000),
])
def test_domestic_travel_selects_one_applicable_return_mode(city, mode, amount):
    quotes = baseline_trip_quotes(request(budget=0), destination('KR', city))
    assert quotes.transport_mode == mode
    assert quotes.flight.amount_krw == amount
    assert quotes.lodging.status == 'estimated'
    assert quotes.flight.status == ('not_applicable' if mode == 'none' else 'estimated')


def test_jeju_district_uses_verified_viewport_without_new_city_alias():
    jeju = Destination('KR', 33.2, 126.3, 33.5, 126.6, verified_city_name='Aewol-eup')
    quotes = baseline_trip_quotes(request(), jeju)
    assert quotes.transport_mode == 'air'
    assert quotes.flight.source.endswith(':KR/jeju')
    broad = replace(jeju, north=38.0)
    assert baseline_trip_quotes(request(), broad).transport_mode == 'ground'


def test_day_trip_has_no_hotel_but_keeps_return_transport():
    quotes = baseline_trip_quotes(request(days=1), destination('KR', 'Busan'))
    assert quotes.lodging.status == 'not_applicable'
    assert quotes.lodging.amount_krw is None
    assert quotes.total_krw == quotes.flight.amount_krw == 120_000


def test_passengers_multiply_return_travel_but_not_one_room():
    single = baseline_trip_quotes(request(budget=0, days=5), destination(), adults=1)
    pair = baseline_trip_quotes(request(budget=0, days=5), destination(), adults=2)
    assert pair.flight.amount_krw == 2 * single.flight.amount_krw
    assert pair.lodging.amount_krw == single.lodging.amount_krw == 4 * 90_000


@pytest.mark.parametrize('invalid', [True, False, 0, -1, 1.5, '2', None])
def test_invalid_adult_configuration_falls_back_to_one(invalid):
    assert baseline_trip_quotes(request(), destination(), adults=invalid) == baseline_trip_quotes(request(), destination())


@pytest.mark.parametrize('missing_status', ['unavailable', 'not_applicable'])
def test_merge_fills_required_missing_item_and_preserves_exact_live_total(missing_status):
    baseline = baseline_trip_quotes(request(), destination())
    actual = QuoteItem('available', 123_456, lodging_identity=LodgingIdentity(123, '실조회 호텔'))
    live = TripQuotes(QuoteItem(missing_status), actual)
    merged = merge_trip_quotes(baseline, live)
    assert merged.flight is baseline.flight
    assert merged.lodging is actual
    assert merged.total_krw == 350_000 + 123_456


def test_missing_price_keeps_matching_hotel_on_estimate_without_claiming_live_rate():
    baseline = baseline_trip_quotes(request(), destination())
    identity = LodgingIdentity(123, '지도에서 확인할 호텔')
    live = TripQuotes(QuoteItem('unavailable'), QuoteItem('unavailable', lodging_identity=identity))
    merged = merge_trip_quotes(baseline, live)
    assert merged.lodging == replace(baseline.lodging, lodging_identity=identity)
    assert merged.lodging.status == 'estimated'
    assert merged.total_krw == baseline.total_krw and merged.verified_total_krw == 0
    assert merge_trip_quotes(baseline, merged) == merged
    day_trip = baseline_trip_quotes(request(days=1), destination())
    assert merge_trip_quotes(day_trip, live).lodging is day_trip.lodging


def test_merge_preserves_live_flight_and_only_estimates_missing_lodging():
    baseline = baseline_trip_quotes(request(), destination())
    flight = QuoteItem('available', 432_100)
    merged = merge_trip_quotes(baseline, TripQuotes(flight, QuoteItem('unavailable')))
    assert merged.flight is flight
    assert merged.lodging is baseline.lodging
    assert merged.total_krw == 432_100 + 180_000
    assert merged.estimated_items == ('lodging',)


def test_unpriced_airport_identity_survives_only_on_required_air_baseline():
    baseline = baseline_trip_quotes(request(), destination())
    identity = AirportIdentity('NRT', '나리타 국제공항', 'JP', 'Narita International Airport')
    live = TripQuotes(QuoteItem('unavailable', airport_identity=identity), QuoteItem('unavailable'))
    merged = merge_trip_quotes(baseline, live)
    assert merged.flight == replace(baseline.flight, airport_identity=identity)
    assert merged.total_krw == baseline.total_krw and merged.verified_total_krw == 0
    assert merged.flight.status == 'estimated' and merge_trip_quotes(baseline, merged) == merged
    for city in ('Seoul', 'Busan'):
        domestic = baseline_trip_quotes(request(), destination('KR', city))
        rejected = merge_trip_quotes(domestic, live)
        assert rejected.flight is domestic.flight and rejected.flight.airport_identity is None


def test_provider_failure_preserves_complete_baseline():
    baseline = baseline_trip_quotes(request(), destination())
    assert merge_trip_quotes(baseline, None) == baseline
    assert merge_trip_quotes(baseline, TripQuotes(QuoteItem('unavailable'), QuoteItem('unavailable'))) == baseline


def test_merge_rejects_live_airfare_for_ground_trip_without_discarding_hotel():
    baseline = baseline_trip_quotes(request(), destination('KR', 'Busan'))
    actual_hotel = QuoteItem('available', 150_000)
    live = TripQuotes(QuoteItem('available', 500_000), actual_hotel, transport_mode='air')
    merged = merge_trip_quotes(baseline, live)
    assert merged.transport_mode == 'ground'
    assert merged.flight is baseline.flight
    assert merged.lodging is actual_hotel
    assert merged.total_krw == 270_000


def test_merge_never_adds_irrelevant_flight_or_hotel_for_seoul_day_trip():
    baseline = baseline_trip_quotes(request(days=1), destination('KR', 'Seoul'))
    unrelated = TripQuotes(QuoteItem('available', 500_000), QuoteItem('available', 200_000))
    merged = merge_trip_quotes(baseline, unrelated)
    assert merged.total_krw == 0
    assert merged.flight.status == merged.lodging.status == 'not_applicable'


def test_merge_is_idempotent_and_never_adds_estimates_to_live_prices():
    baseline = baseline_trip_quotes(request(), destination())
    live = TripQuotes(QuoteItem('available', 310_000), QuoteItem('available', 140_000))
    merged = merge_trip_quotes(baseline, live)
    assert merged.total_krw == 450_000
    assert merge_trip_quotes(baseline, merged) == merged

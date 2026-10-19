"""Complete travel budgets with versioned planning prices, never fabricated quotes.

These KRW amounts are service policy estimates, not surveyed country averages or
downloaded supplier data. Verified provider quotes supersede applicable estimates.
"""

import unicodedata
from dataclasses import dataclass, replace

from app.agent.tools.verified_maps import Destination
from app.schemas.course import CourseRequestSchema
from app.services.course_budget import FLIGHT_BUDGET_SHARE, LODGING_BUDGET_SHARE
from app.services.myrealtrip_quotes import QuoteItem, TripQuotes

BASELINE_VERSION = 'travel-cost-policy-v1'
BASELINE_REFERENCE_DATE = '2026-10-19'


@dataclass(frozen=True)
class _Prices:
    """Fixed per-adult return fares and per-room nightly planning amounts."""

    air: tuple[int, int]
    stay: tuple[int, int, int]
    ground: tuple[int, int] = (100_000, 160_000)


# Changes to these product policy values require a version/date and review;
# there is no periodic download, inferred market average or runtime data file.
_COUNTRY_PRICES: dict[str, _Prices] = {
    'KR': _Prices((180_000, 450_000), (60_000, 100_000, 250_000)),
    'JP': _Prices((350_000, 1_200_000), (80_000, 140_000, 350_000)),
    'VN': _Prices((400_000, 1_400_000), (35_000, 80_000, 220_000)),
    'TH': _Prices((450_000, 1_600_000), (40_000, 90_000, 250_000)),
    'SG': _Prices((600_000, 2_200_000), (120_000, 220_000, 550_000)),
    'TW': _Prices((350_000, 1_200_000), (70_000, 130_000, 300_000)),
    'HK': _Prices((400_000, 1_500_000), (100_000, 180_000, 450_000)),
    'CN': _Prices((350_000, 1_200_000), (60_000, 120_000, 300_000)),
    'US': _Prices((1_400_000, 5_500_000), (130_000, 250_000, 650_000)),
    'FR': _Prices((1_200_000, 4_800_000), (100_000, 180_000, 450_000)),
    'GB': _Prices((1_350_000, 5_200_000), (120_000, 220_000, 550_000)),
    'DE': _Prices((1_200_000, 4_800_000), (100_000, 180_000, 450_000)),
    'AU': _Prices((1_100_000, 4_500_000), (110_000, 200_000, 500_000)),
}
_GLOBAL_PRICES = _Prices((2_000_000, 7_000_000), (180_000, 320_000, 800_000))

# Alias matching is exact after Unicode normalization and is scoped by the
# verified ISO country. These are policy selectors, not place identity evidence.
_CITY_PRICES: dict[tuple[str, str], tuple[str, _Prices]] = {}


def _normalize(value: str) -> str:
    return ''.join(char for char in unicodedata.normalize('NFKC', value).casefold() if char.isalnum())


def _city(country: str, identifier: str, aliases: tuple[str, ...], prices: _Prices) -> None:
    for alias in aliases:
        _CITY_PRICES[country, _normalize(alias)] = (identifier, prices)


_city('JP', 'tokyo', ('Tokyo', 'Tokyo Metropolis', '도쿄', '東京都', '東京'),
      _Prices((350_000, 1_200_000), (90_000, 160_000, 400_000)))
_city('JP', 'osaka', ('Osaka', 'Osaka City', '오사카', '大阪市', '大阪'),
      _Prices((300_000, 1_000_000), (80_000, 130_000, 300_000)))
_city('VN', 'danang', ('Da Nang', 'Đà Nẵng', '다낭'),
      _Prices((400_000, 1_400_000), (30_000, 70_000, 220_000)))
_city('TH', 'bangkok', ('Bangkok', '방콕', 'กรุงเทพมหานคร'),
      _Prices((450_000, 1_600_000), (40_000, 90_000, 250_000)))
_city('SG', 'singapore', ('Singapore', '싱가포르', '新加坡'), _COUNTRY_PRICES['SG'])
_city('US', 'newyork', ('New York', 'New York City', '뉴욕'),
      _Prices((1_600_000, 6_000_000), (200_000, 380_000, 900_000)))
_city('FR', 'paris', ('Paris', '파리'),
      _Prices((1_200_000, 4_800_000), (140_000, 260_000, 700_000)))
_city('GB', 'london', ('London', '런던'),
      _Prices((1_350_000, 5_200_000), (160_000, 300_000, 800_000)))
_city('KR', 'seoul', ('Seoul', 'Seoul Special City', '서울', '서울특별시'), _COUNTRY_PRICES['KR'])
_city('KR', 'busan', ('Busan', 'Busan Metropolitan City', '부산', '부산광역시'),
      _Prices((180_000, 450_000), (60_000, 110_000, 280_000), (120_000, 180_000)))
_city('KR', 'jeju', ('Jeju', 'Jeju City', 'Jeju-si', 'Jeju-do', '제주', '제주시', '제주도', '제주특별자치도',
                    'Seogwipo', 'Seogwipo-si', '서귀포', '서귀포시'),
      _Prices((180_000, 450_000), (60_000, 110_000, 300_000)))


def _policy(destination: Destination, requested_city: str) -> tuple[str, _Prices, str]:
    country = destination.country_code
    city_name = destination.verified_city_name or requested_city
    match = _CITY_PRICES.get((country, _normalize(city_name)))
    if match is not None:
        identifier, prices = match
        return f'{country}/{identifier}', prices, identifier
    # A verified Jeju viewport also covers names of its districts without a
    # growing list of locality exceptions. Mainland viewports do not overlap it.
    if country == 'KR' and (
        33.0 <= destination.south <= destination.north <= 33.7
        and 125.9 <= destination.west <= destination.east <= 127.1
    ):
        _, prices = _CITY_PRICES['KR', _normalize('Jeju')]
        return 'KR/jeju', prices, 'jeju'
    return country if country in _COUNTRY_PRICES else 'global', _COUNTRY_PRICES.get(country, _GLOBAL_PRICES), ''


def _choose(prices: tuple[int, ...], allowance: float, multiplier: int) -> tuple[int, int]:
    affordable = [index for index, amount in enumerate(prices) if amount * multiplier <= allowance]
    index = max(affordable, default=0)
    return prices[index] * multiplier, index


def _estimate(amount: int, *, source: str, reason: str) -> QuoteItem:
    return QuoteItem(
        status='estimated', amount_krw=amount, reason=reason,
        source=f'{BASELINE_VERSION}:{source}', reference_date=BASELINE_REFERENCE_DATE,
    )


def baseline_trip_quotes(
    request: CourseRequestSchema, destination: Destination, *, adults: int = 1,
) -> TripQuotes:
    """Price required return travel and overnight stays using service policy.

    Args:
        request: Validated KRW budget and trip duration. Budget chooses among
            fixed product levels and never multiplies or invents their prices.
        destination: Country and city identity verified by Maps. Only its ISO
            country code selects the country policy; city keys are country scoped.
        adults: Adult travelers for return transport, defaulting to one for an
            unusable configuration. Lodging always assumes one room.
    Returns:
        Estimates with policy version/date. Seoul needs no intercity transport;
        a single-day trip needs no lodging. All other required amounts are
        positive even when the user's budget cannot cover the baseline level.
        Seoul is the departure city, with ICN international/GMP domestic air.
    """
    count = adults if type(adults) is int and adults > 0 else 1
    trip = request.tripCondition
    source, prices, city = _policy(destination, trip.destinationCity)
    mode = 'none' if destination.country_code == 'KR' and city == 'seoul' else (
        'ground' if destination.country_code == 'KR' and city != 'jeju' else 'air'
    )
    if mode == 'none':
        transport = QuoteItem('not_applicable', reason='same_origin_city')
    else:
        fares = prices.ground if mode == 'ground' else prices.air
        amount, level = _choose(fares, trip.budget * FLIGHT_BUDGET_SHARE, count)
        label = ('standard', 'first') if mode == 'ground' else ('economy', 'business')
        transport = _estimate(amount, source=source, reason=f'{mode}_round_trip_{label[level]}')
    nights = trip.totalDays - 1
    if nights == 0:
        lodging = QuoteItem('not_applicable', reason='single_day')
    else:
        amount, level = _choose(prices.stay, trip.budget * LODGING_BUDGET_SHARE, nights)
        lodging = _estimate(amount, source=source, reason=f'one_room_{nights}_nights_{("value", "standard", "premium")[level]}')
    return TripQuotes(flight=transport, lodging=lodging, transport_mode=mode)


def merge_trip_quotes(baseline: TripQuotes, live: TripQuotes | None) -> TripQuotes:
    """Prefer matching real prices and fill each missing required component once.

    Args:
        baseline: Complete policy result defining applicability and travel mode.
        live: Supplier result, or None when the lookup could not execute.
    Returns:
        Available provider items are preserved, including their exact amount.
        Nonapplicable baseline components stay excluded; an airfare result can
        never replace ground travel. Missing/estimated provider results use the
        baseline. A matching supplier hotel or unambiguous airport can survive
        a missing price; its identity is retained on the estimate for a separate
        Maps check. This never makes a baseline amount a live rate. Cancellation is the
        caller's responsibility.
    """
    def select(name: str) -> QuoteItem:
        fallback = getattr(baseline, name)
        if fallback.status == 'not_applicable' or live is None:
            return fallback
        if name == 'flight' and baseline.transport_mode != live.transport_mode:
            return fallback
        quoted = getattr(live, name)
        if quoted.status == 'available':
            return quoted
        if name == 'lodging' and quoted.status in {'unavailable', 'estimated'} and quoted.lodging_identity is not None:
            return replace(fallback, lodging_identity=quoted.lodging_identity)
        if (name == 'flight' and baseline.transport_mode == 'air'
                and quoted.status in {'unavailable', 'estimated'} and quoted.airport_identity is not None):
            return replace(fallback, airport_identity=quoted.airport_identity)
        return fallback

    return TripQuotes(flight=select('flight'), lodging=select('lodging'), transport_mode=baseline.transport_mode)

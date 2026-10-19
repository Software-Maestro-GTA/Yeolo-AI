"""MyRealTrip quotes and explicit cost estimates with separate provenance.

The official contracts are published at https://docs.myrealtrip.com/llms-full.txt.
Flight search uses the public MCP; authenticated partner airport autocomplete
establishes destination airport identity without a local airport data snapshot.
Lodging accepts the observed structured detail contract only when dates, guests,
property region and an available room
all match. Numeric room totals are never derived from nightly price strings.
The provider returns verified quotes only; baseline estimates are created by
the caller and remain distinguishable in the shared result types.
"""

import asyncio
import json
import logging
import math
import re
import unicodedata
from collections.abc import Awaitable
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import date
from itertools import count
from typing import Any, Literal, Self

import httpx

logger = logging.getLogger(__name__)
MCP_URL = 'https://mcp-servers.myrealtrip.com/mcp'
PARTNER_BASE_URL = 'https://partner-ext-api.myrealtrip.com'
PROTOCOL_VERSION = '2025-03-26'
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_AIRPORT_CANDIDATES = 3


@dataclass(frozen=True)
class QuoteRequest:
    """Verified destination and explicit dates for optional travel quotes.

    Args:
        country_code: Country ISO2 already established by the Maps provider.
        country: User's destination country name, retained for caller context.
        city: Destination city used for airport and accommodation lookup.
        start_date: Departure/check-in date.
        end_date: Return/check-out date; start plus total days minus one.
        flight_required: False only when the caller explicitly excludes flying.
        verified_country_name: Country name from verified geographic address components.
        verified_city_name: City name from the same verified geographic response.
        preferred_cabin: Budget policy's preferred flight cabin; economy is the fallback.
        preferred_stay_star_rating: Optional hotel class supported by the supplier.
        flight_budget_krw: Optional flight allowance; an overage triggers a cheaper fallback.
        lodging_budget_krw: Optional whole-stay allowance, never a fabricated price cap.
        origin_airport: Optional IATA departure override; otherwise the provider default.
    Raises:
        ValueError: Invalid destination identity, dates or verified region names.
    """

    country_code: str
    country: str
    city: str
    start_date: date
    end_date: date
    flight_required: bool = True
    verified_country_name: str | None = None
    verified_city_name: str | None = None
    preferred_cabin: Literal['ECONOMY', 'BUSINESS'] = 'ECONOMY'
    preferred_stay_star_rating: Literal['fourstar', 'fivestar'] | None = None
    flight_budget_krw: float | None = None
    lodging_budget_krw: float | None = None
    origin_airport: str | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r'[A-Z]{2}', self.country_code) or not self.city.strip():
            raise ValueError('Invalid quote destination')
        if self.end_date < self.start_date:
            raise ValueError('Invalid quote dates')
        if any(value is not None and (not isinstance(value, str) or not value.strip())
               for value in (self.verified_country_name, self.verified_city_name)):
            raise ValueError('Invalid verified quote destination names')
        if self.preferred_cabin not in ('ECONOMY', 'BUSINESS'):
            raise ValueError('Invalid preferred flight cabin')
        if self.preferred_stay_star_rating not in (None, 'fourstar', 'fivestar'):
            raise ValueError('Invalid preferred stay star rating')
        if self.origin_airport is not None and (not isinstance(self.origin_airport, str)
                                               or not re.fullmatch(r'[A-Z]{3}', self.origin_airport)):
            raise ValueError('Invalid quote origin airport')
        for amount in (self.flight_budget_krw, self.lodging_budget_krw):
            if amount is not None and (isinstance(amount, bool) or not isinstance(amount, (int, float))
                                       or not math.isfinite(amount) or amount < 0):
                raise ValueError('Invalid quote budget allowance')


@dataclass(frozen=True)
class AirportIdentity:
    """Supplier airport identity retained for a separate Maps Essentials check.

    Args:
        code: Actual airport IATA code, excluding the request's origin airport.
        name: Supplier airport name; never generated from an IATA code.
        country_code: Verified destination country ISO2.
        english_name: Optional English airport name from the same supplier record.
    Raises:
        ValueError: An invalid code or unusable supplied name.
    """

    code: str
    name: str
    country_code: str
    english_name: str | None = None

    def __post_init__(self) -> None:
        if (not isinstance(self.code, str) or not re.fullmatch(r'[A-Z]{3}', self.code)
                or not isinstance(self.country_code, str) or not re.fullmatch(r'[A-Z]{2}', self.country_code)
                or not isinstance(self.name, str) or not self.name.strip()
                or (self.english_name is not None
                    and (not isinstance(self.english_name, str) or not self.english_name.strip()))):
            raise ValueError('Invalid airport identity')


@dataclass(frozen=True)
class LodgingIdentity:
    """Supplier hotel identity retained for a separate Maps Essentials check.

    Args:
        gid: Positive MyRealTrip listing ID, never a Google place ID.
        name: Supplier listing name, without invented addresses or coordinates.
    Raises:
        ValueError: An unusable listing identifier or display name.
    """

    gid: int
    name: str

    def __post_init__(self) -> None:
        if type(self.gid) is not int or self.gid <= 0 or not isinstance(self.name, str) or not self.name.strip():
            raise ValueError('Invalid lodging identity')


@dataclass(frozen=True)
class QuoteItem:
    """Verified or estimated KRW amount with explicit lookup status.

    Estimated amounts require a nonempty source and a calendar reference date
    in YYYY-MM-DD format. Invalid amounts or estimate provenance raise ValueError.
    Missing amounts are unknown rather than free.
    """

    status: Literal['available', 'estimated', 'unavailable', 'not_applicable']
    amount_krw: int | None = None
    reason: str = ''
    source: str = ''
    reference_date: str = ''
    lodging_identity: LodgingIdentity | None = None
    airport_identity: AirportIdentity | None = None

    def __post_init__(self) -> None:
        if self.status not in {'available', 'estimated', 'unavailable', 'not_applicable'}:
            raise ValueError('Invalid quote status')
        if self.status in {'available', 'estimated'}:
            if type(self.amount_krw) is not int or self.amount_krw <= 0:
                raise ValueError('Invalid priced quote amount')
        elif self.amount_krw is not None:
            raise ValueError('Unavailable quote cannot supply an amount')
        if not isinstance(self.source, str) or not isinstance(self.reference_date, str):
            raise ValueError('Invalid quote provenance')  # noqa: TRY004 - all invalid quote data uses ValueError
        if self.lodging_identity is not None and (not isinstance(self.lodging_identity, LodgingIdentity)
                                                  or self.status == 'not_applicable'):
            raise ValueError('Invalid quote lodging identity')
        if self.airport_identity is not None and (not isinstance(self.airport_identity, AirportIdentity)
                                                  or self.status == 'not_applicable'):
            raise ValueError('Invalid quote airport identity')
        if self.status == 'estimated':
            if not self.source.strip() or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', self.reference_date):
                raise ValueError('Estimated quote requires a source and ISO reference date')
            try:
                date.fromisoformat(self.reference_date)
            except ValueError:
                raise ValueError('Invalid estimated quote reference date') from None


@dataclass(frozen=True)
class TripQuotes:
    """Transport and lodging costs, retaining verified and estimated subtotals.

    ``flight`` stores the intercity transport component for the selected mode.
    Invalid transport modes raise ValueError. No estimate is a verified quote.
    """

    flight: QuoteItem
    lodging: QuoteItem
    transport_mode: Literal['air', 'rail', 'ground', 'none'] = 'air'

    def __post_init__(self) -> None:
        if self.transport_mode not in {'air', 'rail', 'ground', 'none'}:
            raise ValueError('Invalid trip transport mode')
        if self.transport_mode != 'air' and self.flight.airport_identity is not None:
            raise ValueError('Non-air travel cannot include an airport identity')

    @property
    def total_krw(self) -> int:
        """Sum available quotes and documented estimates without filling missing amounts."""
        return sum(item.amount_krw for item in (self.flight, self.lodging) if item.amount_krw is not None)

    @property
    def verified_total_krw(self) -> int:
        """Sum only provider-verified available amounts, excluding all estimates."""
        return sum(item.amount_krw for item in (self.flight, self.lodging)
                   if item.status == 'available' and item.amount_krw is not None)

    @property
    def complete(self) -> bool:
        """Whether each component is priced or inapplicable; estimates count as priced."""
        return not self.missing_items

    @property
    def estimated_items(self) -> tuple[str, ...]:
        """Return component names whose amounts come from a documented estimate."""
        return tuple(name for name in ('flight', 'lodging') if getattr(self, name).status == 'estimated')

    @property
    def missing_items(self) -> tuple[str, ...]:
        """Return names of components that could not establish a usable quote."""
        return tuple(name for name in ('flight', 'lodging') if getattr(self, name).status == 'unavailable')


@dataclass
class _QuoteProgress:
    """Completed component facts belonging to one exact quote invocation."""

    request: QuoteRequest
    items: dict[str, QuoteItem]


class QuoteUnavailable(ValueError):
    """Sanitized failure reason; provider text, URLs and credentials are excluded."""


def _normalized(value: str) -> str:
    return ''.join(char for char in unicodedata.normalize('NFKC', value).casefold() if char.isalnum())


def _amount(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0 or value != int(value):
        return None
    return int(value)


def _flight_amount(data: dict[str, Any], arguments: dict[str, Any]) -> int:
    """Read actual matching round-trip item totals, never unrelated summaries."""
    result = data.get('result')
    if data.get('success') is not True or not isinstance(result, dict):
        raise QuoteUnavailable('provider_error')
    items = result.get('items')
    if not isinstance(items, list) or not items:
        raise QuoteUnavailable('no_options')
    prices: list[int] = []
    unsupported_currency = False
    for item in items:
        if not isinstance(item, dict) or item.get('tripType') != arguments['tripType']:
            continue
        route, travel, price = item.get('route'), item.get('travelInfo'), item.get('price')
        if not all(isinstance(value, dict) for value in (route, travel, price)):
            continue
        if any(route.get(field) != arguments[field] for field in ('origin', 'destination')):
            continue
        if any(travel.get(field) != arguments[field] for field in ('departDate', 'returnDate')):
            continue
        if price.get('currency') != 'KRW':
            unsupported_currency = True
            continue
        amount = _amount(price.get('total'))
        if amount is not None:
            prices.append(amount)
    if not prices:
        raise QuoteUnavailable('unsupported_currency' if unsupported_currency else 'price_not_verifiable')
    return min(prices)


def _validate_lodging_property(data: dict[str, Any], arguments: dict[str, Any], request: QuoteRequest) -> None:
    """Bind a supplier property to the exact dates, guests, listing and region.

    Pricing is deliberately separate: a missing amount does not erase a matching
    hotel identity. Inconsistent provider facts raise QuoteUnavailable.
    """
    echoed, property_data = data.get('searchParams'), data.get('property')
    if data.get('success') is not True or not all(isinstance(value, dict) for value in (echoed, property_data)):
        raise QuoteUnavailable('price_not_verifiable')
    for field, expected in arguments.items():
        actual = echoed.get(field)
        if type(actual) is not type(expected) or actual != expected:
            raise QuoteUnavailable('price_not_verifiable')
    region = property_data.get('region')
    if type(property_data.get('gid')) is not int or property_data['gid'] != arguments['gid'] or not isinstance(region, dict):
        raise QuoteUnavailable('price_not_verifiable')
    # Canonical geographic names come from the already verified Essentials
    # response, never from model translations, static aliases or fuzzy matching.
    expected_names = (('country', request.country, request.verified_country_name),
                      ('city', request.city, request.verified_city_name))
    if not all(isinstance(region.get(field), str) and _normalized(region[field]) in
               {_normalized(name) for name in (original, verified) if name}
               for field, original, verified in expected_names):
        raise QuoteUnavailable('unverified_property_region')
    if type(region.get('isDomestic')) is not bool or region['isDomestic'] != (request.country_code == 'KR'):
        raise QuoteUnavailable('unverified_property_region')


def _lodging_amount(data: dict[str, Any], arguments: dict[str, Any], request: QuoteRequest) -> int:
    """Validate the observed whole-stay room total against the exact request.

    Args:
        data: Structured ``getStayDetail`` result, not listing display text.
        arguments: Exact detail lookup dates, property and occupancy.
        request: Verified destination supplied by the caller.
    Returns:
        Lowest positive whole-period KRW total among available matching rooms.
    Raises:
        QuoteUnavailable: Missing, inconsistent, sold-out or non-KRW facts.
    """
    _validate_lodging_property(data, arguments, request)
    pricing = data.get('pricing')
    if not isinstance(pricing, dict):
        raise QuoteUnavailable('price_not_verifiable')
    if data.get('noRoomsAvailable') is not False or pricing.get('isSoldOut') is not False:
        raise QuoteUnavailable('no_options')
    rooms = data.get('rooms')
    if not isinstance(rooms, list) or not rooms:
        raise QuoteUnavailable('no_options')
    prices: list[int] = []
    for room in rooms:
        if not isinstance(room, dict) or not isinstance(room.get('roomName'), str) or not room['roomName'].strip():
            continue
        plan = room.get('ratePlan')
        if not isinstance(plan, dict) or plan.get('status') != 'ON_SALE':
            continue
        amount = plan.get('totalPrice')
        if type(amount) is not int or amount <= 0:
            continue
        currencies = [record['currency'] for record in (data, pricing, plan) if 'currency' in record]
        if currencies:
            if any(currency != 'KRW' for currency in currencies):
                continue
        else:
            # Text establishes the KRW unit only. Its digits are never parsed or
            # multiplied: ratePlan.totalPrice already covers the echoed period.
            texts = (pricing.get('priceText'), plan.get('averagePriceText'), plan.get('priceDescription'))
            if not any(isinstance(text, str) and re.search(r'\d[\d,]*(?:\.\d+)?\s*원(?:\b|/|$)', text) for text in texts):
                continue
        prices.append(amount)
    if not prices:
        raise QuoteUnavailable('price_not_verifiable')
    return min(prices)


def _lodging_is_sold_out(data: dict[str, Any]) -> bool:
    """Exclude explicit property or all-room sellouts from recommendation reuse."""
    pricing = data.get('pricing')
    if data.get('noRoomsAvailable') is True or (isinstance(pricing, dict) and pricing.get('isSoldOut') is True):
        return True
    rooms = data.get('rooms')
    return isinstance(rooms, list) and bool(rooms) and all(
        isinstance(room, dict) and isinstance(room.get('ratePlan'), dict)
        and room['ratePlan'].get('status') == 'SOLD_OUT'
        for room in rooms
    )


def _rpc_response(response: httpx.Response, identifier: int) -> dict[str, Any]:
    """Read only JSON-RPC JSON/SSE replies; never scrape display HTML or prose."""
    if len(response.content) > MAX_RESPONSE_BYTES:
        raise QuoteUnavailable('invalid_response')
    media_type = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
    messages: list[object] = []
    try:
        if media_type == 'application/json':
            messages.append(response.json())
        elif media_type == 'text/event-stream':
            data_lines: list[str] = []
            for line in [*response.text.splitlines(), '']:
                if not line and data_lines:
                    messages.append(json.loads('\n'.join(data_lines)))
                    data_lines = []
                elif line.startswith('data:'):
                    data_lines.append(line[5:].lstrip(' '))
        else:
            raise QuoteUnavailable('invalid_response')
    except (ValueError, UnicodeError):
        raise QuoteUnavailable('invalid_response') from None
    for message in messages:
        if not isinstance(message, dict) or type(message.get('id')) is not int or message['id'] != identifier:
            continue
        if message.get('jsonrpc') != '2.0' or 'error' in message or not isinstance(message.get('result'), dict):
            raise QuoteUnavailable('provider_error')
        return message['result']
    raise QuoteUnavailable('invalid_response')


def _tool_data(result: dict[str, Any]) -> dict[str, Any]:
    if result.get('isError'):
        raise QuoteUnavailable('provider_error')
    if isinstance(result.get('structuredContent'), dict):
        return result['structuredContent']
    for block in result.get('content', []):
        if not isinstance(block, dict) or block.get('type') != 'text' or not isinstance(block.get('text'), str):
            continue
        try:
            data = json.loads(block['text'])
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    raise QuoteUnavailable('price_not_verifiable')


class MyRealTripQuoteProvider:
    """Request-scoped, injectable async client for optional MyRealTrip prices.

    Args:
        client: Injected HTTP client; ownership stays with the caller.
        partner_api_key: Partner REST key for airport and optional lodging-region autocomplete.
        origin_airport: Explicit IATA departure airport under the service policy.
        adults: Explicit adult count, supplied to both flight and stay searches.
        children: Explicit child count, supplied to both searches.
        timeout_seconds: Common wall-clock budget for concurrent quote lookups.
        mcp_url: Official MCP endpoint, overridable for transport tests.
        partner_base_url: Official REST origin, overridable for transport tests.
    Raises:
        ValueError: Invalid explicit configuration.

    ``quote`` converts optional provider errors into component statuses and
    preserves cancellation. Hotel listing prices and average/nightly prices are
    never converted to a whole-stay total; only matching structured room totals
    from the detail response are accepted.
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        partner_api_key: str = '',
        origin_airport: str | None = None,
        adults: int = 1,
        children: int = 0,
        timeout_seconds: float = 8,
        mcp_url: str = MCP_URL,
        partner_base_url: str = PARTNER_BASE_URL,
    ) -> None:
        if origin_airport is not None and not re.fullmatch(r'[A-Z]{3}', origin_airport):
            raise ValueError('Invalid quote origin airport')
        if type(adults) is not int or adults < 1 or type(children) is not int or children < 0:
            raise ValueError('Invalid quote guest counts')
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError('Invalid quote timeout')
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))
        self.owns_client = client is None
        self.partner_api_key = partner_api_key
        self.origin_airport = origin_airport
        self.adults = adults
        self.children = children
        self.timeout_seconds = timeout_seconds
        self._cleanup_margin = min(.02, timeout_seconds / 10)
        self.mcp_url = mcp_url
        self.partner_base_url = partner_base_url.rstrip('/')
        self._initialize_lock = asyncio.Lock()
        self._initialized = False
        self._session_id: str | None = None
        self._identifiers = count(1)
        self._airport_cache: dict[str, list[dict[str, Any]]] = {}
        self._airport_locks: dict[str, asyncio.Lock] = {}
        self._region_cache: dict[tuple[str, str], str | None] = {}
        self._region_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._quote_progress: ContextVar[_QuoteProgress | None] = ContextVar('myrealtrip_quote_progress', default=None)
        self._latest_quote: _QuoteProgress | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        try:
            if self._session_id:
                try:
                    await self.client.delete(self.mcp_url, headers=self._headers(), timeout=min(1, self.timeout_seconds))
                except Exception:  # noqa: BLE001 - optional cleanup cannot discard a course
                    logger.warning('Optional MyRealTrip MCP session cleanup unavailable')
        finally:
            self._session_id = None
            self._initialized = False
            self._airport_cache.clear()
            self._airport_locks.clear()
            self._region_cache.clear()
            self._region_locks.clear()
            if self.owns_client:
                try:
                    await self.client.aclose()
                except Exception:  # noqa: BLE001 - optional client cleanup remains fail-open
                    logger.warning('Optional MyRealTrip HTTP client cleanup unavailable')

    def _headers(self) -> dict[str, str]:
        headers = {'Accept': 'application/json, text/event-stream', 'Content-Type': 'application/json'}
        if self._initialized:
            headers['MCP-Protocol-Version'] = PROTOCOL_VERSION
        if self._session_id:
            headers['Mcp-Session-Id'] = self._session_id
        return headers

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        identifier = next(self._identifiers)
        response = await self.client.post(self.mcp_url, headers=self._headers(), json={
            'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params,
        }, timeout=self.timeout_seconds)
        response.raise_for_status()
        if method == 'initialize':
            self._session_id = response.headers.get('Mcp-Session-Id')
        return _rpc_response(response, identifier)

    async def _initialize(self) -> None:
        async with self._initialize_lock:
            if self._initialized:
                return
            result = await self._rpc('initialize', {
                'protocolVersion': PROTOCOL_VERSION,
                'capabilities': {},
                'clientInfo': {'name': 'Yeolo-AI', 'version': '1.0'},
            })
            if result.get('protocolVersion') != PROTOCOL_VERSION:
                raise QuoteUnavailable('unsupported_protocol')
            self._initialized = True
            try:
                response = await self.client.post(self.mcp_url, headers=self._headers(), json={
                    'jsonrpc': '2.0', 'method': 'notifications/initialized',
                }, timeout=self.timeout_seconds)
                response.raise_for_status()
            except BaseException:
                self._initialized = False
                raise

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        await self._initialize()
        return _tool_data(await self._rpc('tools/call', {'name': name, 'arguments': arguments}))

    async def _lookup_airports(self, keyword: str) -> list[dict[str, Any]]:
        """Reuse successful request-local lookups; coalesce concurrent keywords."""
        if not self.partner_api_key:
            raise QuoteUnavailable('missing_partner_key')
        key = unicodedata.normalize('NFKC', keyword).strip().casefold()
        async with self._airport_locks.setdefault(key, asyncio.Lock()):
            if key not in self._airport_cache:
                response = await self.client.post(f'{self.partner_base_url}/v1/products/flight/airport-autocomplete',
                    headers={'Authorization': f'Bearer {self.partner_api_key}'},
                    json={'keyword': keyword, 'size': 20}, timeout=self.timeout_seconds)
                response.raise_for_status()
                payload = response.json()
                if (not isinstance(payload, dict) or not isinstance(payload.get('result'), dict)
                        or payload['result'].get('status') != 200 or not isinstance(payload.get('data'), dict)
                        or not isinstance(payload['data'].get('airports'), list)):
                    raise QuoteUnavailable('invalid_response')
                self._airport_cache[key] = payload['data']['airports']
            return self._airport_cache[key]

    def _airport_options(self, airports: list[dict[str, Any]], request: QuoteRequest) -> dict[str, AirportIdentity | None]:
        """Retain verified airport codes and optional documented display names.

        The official airport-autocomplete contract supplies airport.code,
        airport.koName and airport.enName. Missing or contradictory names do not
        prevent a fare lookup but cannot identify an itinerary airport stop.
        """
        options: dict[str, AirportIdentity | None] = {}
        conflicts: set[str] = set()
        origin = request.origin_airport or self.origin_airport
        for entry in airports:
            if not isinstance(entry, dict):
                continue
            airport, city, country = entry.get('airport'), entry.get('city'), entry.get('country')
            if not all(isinstance(value, dict) for value in (airport, city, country)):
                continue
            country_codes = {code for code in (entry.get('isoCode'), country.get('code')) if isinstance(code, str) and code}
            city_names = [city.get('koName'), city.get('enName')]
            if country_codes != {request.country_code}:
                continue
            expected_cities = {_normalized(name) for name in (request.city, request.verified_city_name) if name}
            if not any(isinstance(name, str) and _normalized(name) in expected_cities for name in city_names):
                continue
            code = airport.get('code')
            if not isinstance(code, str) or not re.fullmatch(r'[A-Z]{3}', code) or code == origin:
                continue
            names = [value.strip() if isinstance(value, str) else ''
                     for value in (airport.get('koName'), airport.get('enName'))]
            identity = AirportIdentity(code, names[0] or names[1], request.country_code, names[1] or None) if any(names) else None
            previous = options.get(code)
            if previous is not None and identity is not None and previous != identity:
                conflicts.add(code)
            options[code] = None if code in conflicts else identity or previous
        return options

    async def _airports(self, request: QuoteRequest) -> dict[str, AirportIdentity | None]:
        """Return actual same-city airport codes verified by live autocomplete.

        No local catalog or inferred country-wide substitute establishes airport
        proximity. A city without a confirmed airport simply has no flight quote;
        the optional quote boundary preserves the verified itinerary.
        """
        options = self._airport_options(await self._lookup_airports(request.verified_city_name or request.city), request)
        if not options:
            raise QuoteUnavailable('unverified_airport')
        return options

    async def _flight(
        self, request: QuoteRequest, deadline: float | None = None, *, cleanup_margin: float | None = None,
    ) -> QuoteItem:
        if not request.flight_required:
            return QuoteItem('not_applicable', reason='not_requested')
        origin = request.origin_airport or self.origin_airport
        if origin is None:
            raise QuoteUnavailable('missing_origin')
        airports = await self._airports(request)
        # Retain a sole verified airport even if fare lookup is unavailable.
        # Evaluate uniqueness before the bounded price-search candidate limit.
        sole_identity = next(iter(airports.values())) if len(airports) == 1 else None
        if sole_identity is not None:
            self._remember_item('flight', QuoteItem('unavailable', reason='price_not_verifiable', airport_identity=sole_identity))
        destinations = list(airports)[:MAX_AIRPORT_CANDIDATES]
        name = 'searchDomesticFlights' if request.country_code == 'KR' else 'searchInternationalFlights'
        passengers = {'adults': self.adults, 'children': self.children}
        if request.country_code != 'KR':
            passengers['infants'] = 0

        async def search(destination: str, cabin: str) -> QuoteItem:
            arguments = {
                'tripType': 'ROUND_TRIP', 'origin': origin,
                'destination': destination, 'departDate': request.start_date.isoformat(),
                'returnDate': request.end_date.isoformat(), 'passengers': passengers,
                'cabinClass': cabin, 'maxResults': 5,
            }
            data = await self._call_tool(name, arguments)
            return self._remember_item('flight', QuoteItem('available', _flight_amount(data, arguments),
                                                          airport_identity=airports[destination]))

        expires = deadline if deadline is not None else asyncio.get_running_loop().time() + self.timeout_seconds
        margin = self._cleanup_margin if cleanup_margin is None else cleanup_margin
        cabins = [request.preferred_cabin]
        if request.preferred_cabin != 'ECONOMY':
            cabins.append('ECONOMY')
        best: QuoteItem | None = None
        for cabin in cabins:
            attempt_deadline = self._preferred_deadline(expires) if cabin != 'ECONOMY' else expires
            results = await self._bounded_items(
                [search(destination, cabin) for destination in destinations], attempt_deadline, cleanup_margin=margin,
            )
            priced = [item for item in results if item.status == 'available' and item.amount_krw is not None]
            if priced:
                cheapest = min(priced, key=lambda item: item.amount_krw)
                if best is None or cheapest.amount_krw < best.amount_krw:
                    best = cheapest
                if request.flight_budget_krw is None or cheapest.amount_krw <= request.flight_budget_krw:
                    return best
            if asyncio.get_running_loop().time() >= expires - margin:
                break
        return best if best is not None else replace(results[0], airport_identity=sole_identity)

    @staticmethod
    def _preferred_deadline(deadline: float) -> float:
        """Reserve half of the remaining quote time for the existing default fallback."""
        now = asyncio.get_running_loop().time()
        return now + max(0, deadline - now) / 2

    async def _lodging_region_name(self, request: QuoteRequest) -> str | None:
        """Bridge names through one unambiguous live supplier city record.

        Args:
            request: Destination country/city names already established by Maps.
        Returns:
            Supplier's Korean city name, or None when identity is not certain.
            The region ID establishes uniqueness only; it is never passed into
            MCP tools, whose published request contract is different.
        Raises:
            asyncio.CancelledError: Caller cancellation remains immediate.
        """
        if not self.partner_api_key or not request.verified_country_name:
            return None
        keyword = request.verified_city_name or request.city
        if len(keyword) > 100:
            return None
        key = (_normalized(request.verified_country_name), _normalized(keyword))
        async with self._region_locks.setdefault(key, asyncio.Lock()):
            if key in self._region_cache:
                return self._region_cache[key]
            name = None
            try:
                response = await self.client.post(f'{self.partner_base_url}/v1/products/accommodation/region-autocomplete',
                    headers={'Authorization': f'Bearer {self.partner_api_key}'},
                    json={'keyword': keyword, 'isDomestic': request.country_code == 'KR'}, timeout=self.timeout_seconds)
                response.raise_for_status()
                payload = response.json()
                if (not isinstance(payload, dict) or not isinstance(payload.get('result'), dict)
                        or payload['result'].get('status') != 200 or not isinstance(payload.get('data'), dict)
                        or not isinstance(payload['data'].get('regions'), list)):
                    raise QuoteUnavailable('invalid_response')
                cities = {_normalized(value) for value in (request.city, request.verified_city_name) if value}
                matches: set[tuple[int, str]] = set()
                for region in payload['data']['regions']:
                    if not isinstance(region, dict) or region.get('type') != 'CITY':
                        continue
                    identifier, local_name, parent = region.get('regionId'), region.get('name'), region.get('subName')
                    if type(identifier) is not int or identifier <= 0 or not isinstance(local_name, str) or not local_name.strip():
                        continue
                    if not isinstance(parent, str) or _normalized(parent.rsplit(',', 1)[-1]) != key[0]:
                        continue
                    if not any(isinstance(value, str) and _normalized(value) in cities
                               for value in (local_name, region.get('enName'))):
                        continue
                    matches.add((identifier, local_name))
                if len(matches) == 1:
                    name = next(iter(matches))[1]
            except Exception:  # noqa: BLE001 - an optional name bridge cannot invalidate a matching quote
                logger.warning('Optional MyRealTrip lodging region lookup unavailable')
            self._region_cache[key] = name
            return name

    async def _lodging(
        self, request: QuoteRequest, deadline: float | None = None, *, cleanup_margin: float | None = None,
    ) -> QuoteItem:
        """Prefer the requested hotel class, keeping fallback inside the same deadline."""
        expires = deadline if deadline is not None else asyncio.get_running_loop().time() + self.timeout_seconds
        margin = self._cleanup_margin if cleanup_margin is None else cleanup_margin
        preferred_deadline = self._preferred_deadline(expires) if request.preferred_stay_star_rating is not None else expires
        result = await self._safe_item(self._lodging_options(request, preferred_deadline, cleanup_margin=margin))
        exceeds_allowance = (result.amount_krw is not None and request.lodging_budget_krw is not None
                             and result.amount_krw > request.lodging_budget_krw)
        if ((result.status != 'unavailable' and not exceeds_allowance) or request.preferred_stay_star_rating is None
                or asyncio.get_running_loop().time() >= expires - margin):
            return result
        fallback = await self._safe_item(self._lodging_options(
            replace(request, preferred_stay_star_rating=None), expires, cleanup_margin=margin,
        ))
        if result.amount_krw is not None:
            return fallback if fallback.amount_krw is not None and fallback.amount_krw < result.amount_krw else result
        return fallback if fallback.status != 'unavailable' or fallback.lodging_identity is not None else result

    async def _lodging_options(self, request: QuoteRequest, deadline: float, *, cleanup_margin: float) -> QuoteItem:
        """Choose the cheapest verified room from the supplier's requested hotel class."""
        if request.start_date == request.end_date:
            return QuoteItem('not_applicable', reason='single_day')
        arguments = {
            'keyword': request.city, 'checkIn': request.start_date.isoformat(),
            'checkOut': request.end_date.isoformat(), 'adultCount': self.adults,
            'childCount': self.children, 'isDomestic': request.country_code == 'KR',
            'page': 1, 'size': 5,
        }
        if request.preferred_stay_star_rating is not None:
            arguments['starRating'] = request.preferred_stay_star_rating
        try:
            async with asyncio.timeout_at(deadline - cleanup_margin):
                data = await self._call_tool('searchStays', arguments)
        except TimeoutError:
            raise QuoteUnavailable('timeout') from None
        stays = data.get('stays')
        if not isinstance(stays, list) or not stays:
            raise QuoteUnavailable('no_options')
        gids = []
        identities: dict[int, LodgingIdentity] = {}
        for stay in stays:
            gid = stay.get('gid') if isinstance(stay, dict) else None
            if type(gid) is int and gid > 0 and gid not in gids:
                gids.append(gid)
                name = stay.get('name')
                if isinstance(name, str) and name.strip():
                    identities[gid] = LodgingIdentity(gid, name.strip())
                if len(gids) == 2:
                    break
        if not gids:
            raise QuoteUnavailable('price_not_verifiable')

        async def detail(gid: int) -> QuoteItem:
            detail_arguments = {
                'gid': gid, 'checkIn': arguments['checkIn'], 'checkOut': arguments['checkOut'],
                'adultCount': self.adults, 'childCount': self.children,
            }
            response = await self._call_tool('getStayDetail', detail_arguments)
            matched_request = request
            try:
                _validate_lodging_property(response, detail_arguments, matched_request)
            except QuoteUnavailable as error:
                if str(error) != 'unverified_property_region':
                    raise
                # Already matching quotes need no additional REST request. A
                # bridge changes accepted names only; revalidate every price,
                # date, occupancy, property and domestic/overseas fact afterward.
                region_name = await self._lodging_region_name(request)
                if region_name is None:
                    raise
                matched_request = replace(request, verified_city_name=region_name)
                _validate_lodging_property(response, detail_arguments, matched_request)
            identity = None if _lodging_is_sold_out(response) else identities.get(gid)
            try:
                amount = _lodging_amount(response, detail_arguments, matched_request)
            except QuoteUnavailable as error:
                return self._remember_item('lodging', QuoteItem(
                    'unavailable', reason=str(error), lodging_identity=None if str(error) == 'no_options' else identity,
                ))
            return self._remember_item('lodging', QuoteItem('available', amount, lodging_identity=identity))

        results = await self._bounded_items([detail(gid) for gid in gids], deadline, cleanup_margin=cleanup_margin)
        priced = [item for item in results if item.status == 'available' and item.amount_krw is not None]
        if priced:
            return min(priced, key=lambda item: item.amount_krw)
        return next((item for item in results if item.lodging_identity is not None), results[0])

    async def _bounded_items(
        self, lookups: list[Awaitable[QuoteItem]], deadline: float | None, *, cleanup_margin: float | None = None,
    ) -> list[QuoteItem]:
        """Retain completed quotes and join slower alternatives before timeout."""
        loop = asyncio.get_running_loop()
        expires = deadline if deadline is not None else loop.time() + self.timeout_seconds
        margin = self._cleanup_margin if cleanup_margin is None else cleanup_margin
        tasks = [asyncio.create_task(self._safe_item(lookup)) for lookup in lookups]
        try:
            # Return the known alternatives before the enclosing quote deadline
            # so a slow second airport/hotel does not discard a completed price.
            remaining = max(0, expires - loop.time() - margin)
            done, _ = await asyncio.wait(tasks, timeout=remaining)
            return [task.result() if task in done else QuoteItem('unavailable', reason='timeout') for task in tasks]
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _safe_item(self, lookup: Awaitable[QuoteItem]) -> QuoteItem:
        try:
            return await lookup
        except QuoteUnavailable as error:
            return QuoteItem('unavailable', reason=str(error))
        except Exception:  # noqa: BLE001 - optional quoting cannot fail a verified itinerary
            return QuoteItem('unavailable', reason='provider_error')

    def _remember_item(self, name: str, item: QuoteItem) -> QuoteItem:
        """Publish verified leaf results before batch collection can be cancelled."""
        progress = self._quote_progress.get()
        if progress is not None:
            previous = progress.items[name]
            if previous.status == 'available':
                if item.status == 'available' and item.amount_krw < previous.amount_krw:
                    progress.items[name] = item
            else:
                identity_field = 'airport_identity' if name == 'flight' else 'lodging_identity'
                if item.status == 'available' or getattr(previous, identity_field) is None or getattr(item, identity_field) is not None:
                    progress.items[name] = item
        return item

    def completed_quotes(self, request: QuoteRequest) -> TripQuotes | None:
        """Recover completed prices only for the caller's exact request object.

        Args:
            request: The object passed to the latest ``quote`` invocation.
        Returns:
            Its completed component prices, or None for another/older request.
            Missing components stay unavailable; no network requests are made.
        """
        progress = self._latest_quote
        if progress is None or progress.request is not request:
            return None
        return TripQuotes(progress.items['flight'], progress.items['lodging'])

    async def quote(self, request: QuoteRequest, *, deadline: float | None = None) -> TripQuotes:
        """Look up flight/stay concurrently without failing a verified course.

        Completed facts are saved before collection. A caller timeout can recover
        them even when event-loop delay runs both timeout callbacks together.
        Each invocation resets the snapshot; child tasks inherit their own
        progress context so an older invocation cannot mutate the new snapshot.

        Args:
            request: Verified destination and explicit travel date range.
            deadline: Optional absolute event-loop deadline from the caller. The
                shorter of this and the provider timeout bounds all lookups.
        Returns:
            Component outcomes; unavailable amounts remain None.
        Raises:
            asyncio.CancelledError: Caller cancellation after child requests join.
        """
        names = ('flight', 'lodging')
        progress = _QuoteProgress(request, {
            'flight': QuoteItem('unavailable', reason='timeout') if request.flight_required
            else QuoteItem('not_applicable', reason='not_requested'),
            'lodging': QuoteItem('unavailable', reason='timeout') if request.start_date < request.end_date
            else QuoteItem('not_applicable', reason='single_day'),
        })
        self._latest_quote = progress
        loop = asyncio.get_running_loop()
        now = loop.time()
        expires = min(now + self.timeout_seconds, deadline if deadline is not None else float('inf'))
        remaining = expires - now
        if remaining <= 0:
            return TripQuotes(progress.items['flight'], progress.items['lodging'])
        margin = min(self._cleanup_margin, remaining / 10)
        # Nested batches already reserve their own cleanup margin relative to
        # this deadline. Reserve another margin for collecting those results and
        # joining stalled lookups before the node's outer timeout cancels us.
        collection_deadline = expires - margin
        async def lookup_item(name: str, lookup: Awaitable[QuoteItem]) -> QuoteItem:
            return self._remember_item(name, await self._safe_item(lookup))

        token = self._quote_progress.set(progress)
        tasks = [asyncio.create_task(lookup_item(name, lookup(request, collection_deadline, cleanup_margin=margin)))
                 for name, lookup in zip(names, (self._flight, self._lodging), strict=True)]
        try:
            done, _ = await asyncio.wait(tasks, timeout=max(0.0, collection_deadline - loop.time()))
            for task in done:
                task.result()  # Preserve cancellation from any completed child.
            outcomes = [progress.items[name] for name in names]
            for name, item in zip(names, outcomes, strict=True):
                if item.status == 'unavailable':
                    logger.warning('Optional MyRealTrip %s quote unavailable (%s)', name, item.reason)
            return TripQuotes(*outcomes)
        finally:
            try:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                self._quote_progress.reset(token)

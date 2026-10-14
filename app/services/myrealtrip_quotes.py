"""Optional MyRealTrip quotes; unverified prices never become a zero-cost quote.

The official contracts are published at https://docs.myrealtrip.com/llms-full.txt.
Flight search uses the public MCP; authenticated partner airport autocomplete
establishes destination airport identity. Lodging accepts the observed structured
detail contract only when dates, guests, property region and an available room
all match. Numeric room totals are never derived from nightly price strings.
"""

import asyncio
import json
import logging
import math
import re
import unicodedata
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import date
from itertools import count
from typing import Any, Literal, Self

import httpx

logger = logging.getLogger(__name__)
MCP_URL = 'https://mcp-servers.myrealtrip.com/mcp'
PARTNER_BASE_URL = 'https://partner-ext-api.myrealtrip.com'
PROTOCOL_VERSION = '2025-03-26'
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


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
    Raises:
        ValueError: Missing destination identity or inconsistent dates.
    """

    country_code: str
    country: str
    city: str
    start_date: date
    end_date: date
    flight_required: bool = True

    def __post_init__(self) -> None:
        if not re.fullmatch(r'[A-Z]{2}', self.country_code) or not self.city.strip():
            raise ValueError('Invalid quote destination')
        if self.end_date < self.start_date:
            raise ValueError('Invalid quote dates')


@dataclass(frozen=True)
class QuoteItem:
    """Amount and lookup status; missing amounts are unknown rather than free."""

    status: Literal['available', 'unavailable', 'not_applicable']
    amount_krw: int | None = None
    reason: str = ''

    def __post_init__(self) -> None:
        if self.status not in {'available', 'unavailable', 'not_applicable'}:
            raise ValueError('Invalid quote status')
        if self.status == 'available':
            if type(self.amount_krw) is not int or self.amount_krw <= 0:
                raise ValueError('Invalid available quote amount')
        elif self.amount_krw is not None:
            raise ValueError('Unavailable quote cannot supply an amount')


@dataclass(frozen=True)
class TripQuotes:
    """Independent flight and lodging outcomes used in the known-cost subtotal."""

    flight: QuoteItem
    lodging: QuoteItem

    @property
    def known_total_krw(self) -> int:
        """Sum supplied amounts without claiming the subtotal is complete."""
        return sum(item.amount_krw for item in (self.flight, self.lodging) if item.amount_krw is not None)

    @property
    def complete(self) -> bool:
        """Whether each requested component has a verified or inapplicable result."""
        return not self.missing_items

    @property
    def missing_items(self) -> tuple[str, ...]:
        """Return names of components that could not establish a usable quote."""
        return tuple(name for name in ('flight', 'lodging') if getattr(self, name).status == 'unavailable')


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
    echoed, property_data, pricing = data.get('searchParams'), data.get('property'), data.get('pricing')
    if data.get('success') is not True or not all(isinstance(value, dict) for value in (echoed, property_data, pricing)):
        raise QuoteUnavailable('price_not_verifiable')
    for field, expected in arguments.items():
        actual = echoed.get(field)
        if type(actual) is not type(expected) or actual != expected:
            raise QuoteUnavailable('price_not_verifiable')
    region = property_data.get('region')
    if type(property_data.get('gid')) is not int or property_data['gid'] != arguments['gid'] or not isinstance(region, dict):
        raise QuoteUnavailable('price_not_verifiable')
    if not all(isinstance(region.get(field), str) and _normalized(region[field]) == _normalized(expected)
               for field, expected in (('country', request.country), ('city', request.city))):
        raise QuoteUnavailable('unverified_property_region')
    if type(region.get('isDomestic')) is not bool or region['isDomestic'] != (request.country_code == 'KR'):
        raise QuoteUnavailable('unverified_property_region')
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
        partner_api_key: Partner REST key used only for airport autocomplete.
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
        self.mcp_url = mcp_url
        self.partner_base_url = partner_base_url.rstrip('/')
        self._initialize_lock = asyncio.Lock()
        self._initialized = False
        self._session_id: str | None = None
        self._identifiers = count(1)

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

    async def _airports(self, request: QuoteRequest) -> list[str]:
        if not self.partner_api_key:
            raise QuoteUnavailable('missing_partner_key')
        response = await self.client.post(f'{self.partner_base_url}/v1/products/flight/airport-autocomplete',
            headers={'Authorization': f'Bearer {self.partner_api_key}'},
            json={'keyword': request.city, 'size': 20}, timeout=self.timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get('result', {}).get('status') != 200:
            raise QuoteUnavailable('invalid_response')
        airports = payload.get('data', {}).get('airports', [])
        if not isinstance(airports, list):
            raise QuoteUnavailable('invalid_response')
        codes: list[str] = []
        for entry in airports:
            if not isinstance(entry, dict):
                continue
            airport, city, country = entry.get('airport'), entry.get('city'), entry.get('country')
            if not all(isinstance(value, dict) for value in (airport, city, country)):
                continue
            country_codes = {code for code in (entry.get('isoCode'), country.get('code')) if isinstance(code, str) and code}
            city_names = [city.get('koName'), city.get('enName')]
            if country_codes != {request.country_code} or not any(isinstance(name, str) and _normalized(name) == _normalized(request.city) for name in city_names):
                continue
            code = airport.get('code')
            if isinstance(code, str) and re.fullmatch(r'[A-Z]{3}', code) and code != self.origin_airport and code not in codes:
                codes.append(code)
        if not codes:
            raise QuoteUnavailable('unverified_airport')
        return codes[:3]

    async def _flight(self, request: QuoteRequest, deadline: float | None = None) -> QuoteItem:
        if not request.flight_required:
            return QuoteItem('not_applicable', reason='not_requested')
        if self.origin_airport is None:
            raise QuoteUnavailable('missing_origin')
        destinations = await self._airports(request)
        name = 'searchDomesticFlights' if request.country_code == 'KR' else 'searchInternationalFlights'
        passengers = {'adults': self.adults, 'children': self.children}
        if request.country_code != 'KR':
            passengers['infants'] = 0

        async def search(destination: str) -> QuoteItem:
            arguments = {
                'tripType': 'ROUND_TRIP', 'origin': self.origin_airport,
                'destination': destination, 'departDate': request.start_date.isoformat(),
                'returnDate': request.end_date.isoformat(), 'passengers': passengers,
                'cabinClass': 'ECONOMY', 'maxResults': 5,
            }
            data = await self._call_tool(name, arguments)
            return QuoteItem('available', _flight_amount(data, arguments))

        results = await self._bounded_items([search(destination) for destination in destinations], deadline)
        prices = [item.amount_krw for item in results if item.status == 'available' and item.amount_krw is not None]
        if not prices:
            return results[0]
        return QuoteItem('available', min(prices))

    async def _lodging(self, request: QuoteRequest, deadline: float | None = None) -> QuoteItem:
        if request.start_date == request.end_date:
            return QuoteItem('not_applicable', reason='single_day')
        arguments = {
            'keyword': request.city, 'checkIn': request.start_date.isoformat(),
            'checkOut': request.end_date.isoformat(), 'adultCount': self.adults,
            'childCount': self.children, 'isDomestic': request.country_code == 'KR',
            'page': 1, 'size': 5,
        }
        data = await self._call_tool('searchStays', arguments)
        stays = data.get('stays')
        if not isinstance(stays, list) or not stays:
            raise QuoteUnavailable('no_options')
        gids = []
        for stay in stays:
            gid = stay.get('gid') if isinstance(stay, dict) else None
            if type(gid) is int and gid > 0 and gid not in gids:
                gids.append(gid)
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
            return QuoteItem('available', _lodging_amount(response, detail_arguments, request))

        results = await self._bounded_items([detail(gid) for gid in gids], deadline)
        prices = [item.amount_krw for item in results if item.status == 'available' and item.amount_krw is not None]
        return QuoteItem('available', min(prices)) if prices else results[0]

    async def _bounded_items(self, lookups: list[Awaitable[QuoteItem]], deadline: float | None) -> list[QuoteItem]:
        """Retain completed quotes and join slower alternatives before timeout."""
        loop = asyncio.get_running_loop()
        expires = deadline if deadline is not None else loop.time() + self.timeout_seconds
        tasks = [asyncio.create_task(self._safe_item(lookup)) for lookup in lookups]
        try:
            # Return the known alternatives before the enclosing quote deadline
            # so a slow second airport/hotel does not discard a completed price.
            remaining = max(0, expires - loop.time() - min(.02, self.timeout_seconds / 10))
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

    async def quote(self, request: QuoteRequest) -> TripQuotes:
        """Look up flight/stay concurrently without failing a verified course.

        Args:
            request: Verified destination and explicit travel date range.
        Returns:
            Component outcomes; unavailable amounts remain None.
        Raises:
            asyncio.CancelledError: Caller cancellation after child requests join.
        """
        names = ('flight', 'lodging')
        deadline = asyncio.get_running_loop().time() + self.timeout_seconds
        tasks = [asyncio.create_task(self._safe_item(lookup(request, deadline))) for lookup in (self._flight, self._lodging)]
        try:
            done, _ = await asyncio.wait(tasks, timeout=self.timeout_seconds)
            outcomes = [task.result() if task in done else QuoteItem('unavailable', reason='timeout') for task in tasks]
            for name, item in zip(names, outcomes, strict=True):
                if item.status == 'unavailable':
                    logger.warning('Optional MyRealTrip %s quote unavailable (%s)', name, item.reason)
            return TripQuotes(*outcomes)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

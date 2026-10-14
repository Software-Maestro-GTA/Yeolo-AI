"""Request-local Maps usage counters and public list-price estimates.

Rates are USD per request at the first paid tier, checked on 2026-10-14.
Free allowances, volume/contract discounts, taxes and LLM costs are excluded.
Attempt estimates include errors/cancellations whose actual billing is unknown.
No credentials, queries, identifiers or provider responses are retained here.
Source: https://developers.google.com/maps/billing-and-pricing/pricing
"""

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from app.services.route_guidance import ROUTE_GUIDANCE_FIELDS, ROUTE_METRIC_FIELDS

PRICING_DATE = '2026-10-14'
UNIT_PRICES_USD = {
    'text_search_ids': 0.0,
    'text_search_pro': 0.032,
    'text_search_enterprise': 0.035,
    'place_details_ids': 0.0,
    'place_details_essentials': 0.005,
    'place_details_pro': 0.017,
    'place_details_enterprise': 0.020,
    'routes_essentials': 0.005,
}
_PLACE_FIELDS = {'id', 'displayName', 'formattedAddress', 'addressComponents', 'location', 'businessStatus', 'primaryType', 'types', 'rating', 'regularOpeningHours', 'viewport'}
_ENTERPRISE_FIELDS = {'rating', 'regularOpeningHours'}

# Place Details tiers differ from Text Search: coordinates, address and types
# are Essentials here. Photo metadata belongs to IDs Only, while an actual
# /photos/.../media request is a separate unsupported endpoint, never $0.005.
# https://developers.google.com/maps/documentation/places/web-service/place-details#fieldmask
PLACE_DETAILS_IDS_FIELDS = frozenset({'attributions', 'consumerAlert', 'id', 'movedPlace', 'movedPlaceId', 'name', 'photos'})
PLACE_DETAILS_ESSENTIALS_FIELDS = frozenset({'addressComponents', 'addressDescriptor', 'adrFormatAddress', 'formattedAddress', 'location', 'plusCode', 'postalAddress', 'shortFormattedAddress', 'types', 'viewport'})
PLACE_DETAILS_PRO_FIELDS = frozenset({'accessibilityOptions', 'businessStatus', 'containingPlaces', 'displayName', 'googleMapsLinks', 'googleMapsTypeLabel', 'googleMapsUri', 'iconBackgroundColor', 'iconMaskBaseUri', 'openingDate', 'primaryType', 'primaryTypeDisplayName', 'pureServiceAreaBusiness', 'subDestinations', 'timeZone', 'utcOffsetMinutes'})
PLACE_DETAILS_ENTERPRISE_FIELDS = frozenset({'currentOpeningHours', 'currentSecondaryOpeningHours', 'internationalPhoneNumber', 'nationalPhoneNumber', 'priceLevel', 'priceRange', 'rating', 'regularOpeningHours', 'regularSecondaryOpeningHours', 'transitStation', 'userRatingCount', 'websiteUri'})
_DETAIL_FIELDS = PLACE_DETAILS_IDS_FIELDS | PLACE_DETAILS_ESSENTIALS_FIELDS | PLACE_DETAILS_PRO_FIELDS | PLACE_DETAILS_ENTERPRISE_FIELDS


def _mask_fields(options: dict[str, Any], query: str) -> set[str] | None:
    """Require one actual header mask and no conflicting URL/parameter mask."""
    headers = options.get('headers', {})
    if not isinstance(headers, Mapping):
        return None
    masks = [value for name, value in headers.items() if isinstance(name, str) and name.lower() == 'x-goog-fieldmask']
    if len(masks) != 1 or not isinstance(masks[0], str) or not masks[0]:
        return None
    # These APIs also accept field-mask query parameters. Their precedence with
    # duplicate headers is outside our request contract; do not underprice it.
    if any(name.lower() in {'fields', '$fields'} for name, _ in parse_qsl(query)):
        return None
    params = options.get('params', {})
    if params and not isinstance(params, Mapping):
        return None
    if isinstance(params, Mapping) and any(isinstance(name, str) and name.lower() in {'fields', '$fields'} for name in params):
        return None
    return set(masks[0].split(','))


def _details_sku(fields: set[str]) -> str:
    """Use the highest requested known tier; unknown fields stay unpriced."""
    if not fields or not fields <= _DETAIL_FIELDS:
        return 'unknown'
    if fields & PLACE_DETAILS_ENTERPRISE_FIELDS:
        return 'place_details_enterprise'
    if fields & PLACE_DETAILS_PRO_FIELDS:
        return 'place_details_pro'
    if fields & PLACE_DETAILS_ESSENTIALS_FIELDS:
        return 'place_details_essentials'
    return 'place_details_ids'


def classify_maps_sku(method: str, url: str, options: dict[str, Any]) -> str:
    """Classify the supported actual request masks; unfamiliar requests stay unknown.

    Args:
        method: HTTP method.
        url: Provider endpoint, used for classification only.
        options: HTTP arguments including headers and JSON body.
    Returns:
        A supported SKU key or ``unknown``; input data is never stored.
    """
    endpoint = urlsplit(url)
    fields = _mask_fields(options, endpoint.query)
    if endpoint.scheme != 'https' or fields is None:
        return 'unknown'
    if endpoint.netloc == 'places.googleapis.com' and method == 'GET' and re.fullmatch(r'/v1/places/[A-Za-z0-9_-]+', endpoint.path):
        return _details_sku(fields)
    if endpoint.netloc == 'places.googleapis.com' and method == 'POST' and endpoint.path == '/v1/places:searchText':
        if fields == {'places.id'}:
            return 'text_search_ids'
        if fields and all(field.startswith('places.') for field in fields):
            selected = {field.removeprefix('places.') for field in fields}
            if selected <= _PLACE_FIELDS:
                return 'text_search_enterprise' if selected & _ENTERPRISE_FIELDS else 'text_search_pro'
    if endpoint.netloc == 'routes.googleapis.com' and endpoint.path == '/directions/v2:computeRoutes' and method == 'POST':
        body = options.get('json', {})
        if not isinstance(body, Mapping):
            return 'unknown'
        if fields in (ROUTE_METRIC_FIELDS, ROUTE_METRIC_FIELDS | ROUTE_GUIDANCE_FIELDS) and set(body) <= {'origin', 'destination', 'travelMode', 'departureTime', 'languageCode'} and body.get('travelMode') in {'WALK', 'TRANSIT', 'DRIVE'} and ('languageCode' not in body or isinstance(body['languageCode'], str) and re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', body['languageCode'])):
            return 'routes_essentials'
    return 'unknown'


class MapsCostMetrics:
    """Accumulate numeric counters on the request's event loop, without user data."""

    def __init__(self) -> None:
        self._counts: dict[str, dict[str, int]] = {}

    def _increment(self, sku: str, counter: str) -> None:
        bucket = self._counts.setdefault(sku, dict.fromkeys(('wrapper_calls', 'requests', 'responses_200', 'errors', 'cache_hits'), 0))
        bucket[counter] += 1

    def record_call(self, sku: str) -> None:
        """Count an adapter call, including cache and lock/semaphore waiters."""
        self._increment(sku, 'wrapper_calls')

    def record_request(self, sku: str) -> None:
        """Count immediately before the actual HTTP send, after acquiring capacity."""
        self._increment(sku, 'requests')

    def record_response(self, sku: str, status_code: int) -> None:
        """Count HTTP 200 responses independently of JSON or semantic validation."""
        if status_code == 200:
            self._increment(sku, 'responses_200')

    def record_error(self, sku: str) -> None:
        """Count failed HTTP/JSON responses or cancelled in-flight sends once."""
        self._increment(sku, 'errors')

    def record_cache_hit(self, sku: str) -> None:
        """Count successful request-local cache reuse, with no additional charge."""
        self._increment(sku, 'cache_hits')

    def snapshot(self) -> dict[str, Any]:
        """Return detached numeric statistics; estimates do not establish billing.

        Unknown SKU attempts make the total estimate incomplete. The total is the
        known priced subtotal; unknown entries retain a null unit/estimated price.
        """
        skus = {}
        attempted, successful = 0.0, 0.0
        complete = True
        for sku, counts in sorted(self._counts.items()):
            price = UNIT_PRICES_USD.get(sku)
            estimate = None if price is None else round(counts['requests'] * price, 6)
            success = None if price is None else round(counts['responses_200'] * price, 6)
            skus[sku] = {**counts, 'unit_price_usd': price, 'estimated_list_cost_usd': estimate, 'successful_list_cost_usd': success}
            if price is None and counts['requests']:
                complete = False
            attempted += estimate or 0
            successful += success or 0
        return {'pricing_date': PRICING_DATE, 'currency': 'USD', 'estimated_list_cost_usd': round(attempted, 6), 'successful_list_cost_usd': round(successful, 6), 'estimate_complete': complete, 'skus': skus}

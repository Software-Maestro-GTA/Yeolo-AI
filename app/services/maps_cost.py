"""Request-local Maps usage counters and public list-price estimates.

Rates are USD per request at the first paid tier, checked on 2026-10-02.
Free allowances, volume/contract discounts, taxes and LLM costs are excluded.
Attempt estimates include errors/cancellations whose actual billing is unknown.
No credentials, queries, identifiers or provider responses are retained here.
Source: https://developers.google.com/maps/billing-and-pricing/pricing
"""

from typing import Any
from urllib.parse import urlsplit

PRICING_DATE = '2026-10-02'
UNIT_PRICES_USD = {
    'text_search_ids': 0.0,
    'text_search_pro': 0.032,
    'text_search_enterprise': 0.035,
    'place_details_enterprise': 0.020,
    'routes_essentials': 0.005,
}
_PLACE_FIELDS = {'id', 'displayName', 'formattedAddress', 'addressComponents', 'location', 'businessStatus', 'primaryType', 'types', 'rating', 'regularOpeningHours', 'viewport'}
_ENTERPRISE_FIELDS = {'rating', 'regularOpeningHours'}


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
    headers = {key.lower(): value for key, value in options.get('headers', {}).items()}
    fields = set(headers.get('x-goog-fieldmask', '').split(','))
    if endpoint.netloc == 'places.googleapis.com':
        if method == 'POST' and endpoint.path == '/v1/places:searchText':
            if fields == {'places.id'}:
                return 'text_search_ids'
            if fields and all(field.startswith('places.') for field in fields):
                selected = {field.removeprefix('places.') for field in fields}
                if selected <= _PLACE_FIELDS:
                    return 'text_search_enterprise' if selected & _ENTERPRISE_FIELDS else 'text_search_pro'
        if method == 'GET' and endpoint.path.startswith('/v1/places/') and fields <= _PLACE_FIELDS and fields & _ENTERPRISE_FIELDS:
            return 'place_details_enterprise'
    if endpoint.netloc == 'routes.googleapis.com' and endpoint.path == '/directions/v2:computeRoutes' and method == 'POST':
        body = options.get('json', {})
        if fields == {'routes.duration', 'routes.distanceMeters'} and set(body) <= {'origin', 'destination', 'travelMode', 'departureTime'} and body.get('travelMode') in {'WALK', 'TRANSIT', 'DRIVE'}:
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

"""Strict provider boundary for course generation; failures never invent facts."""

import asyncio
import math
import re
import unicodedata
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Self
from urllib.parse import parse_qsl, unquote, urlsplit

import httpx

from app.core.config import settings
from app.schemas.course import (
    PhotoAttributionSchema,
    PhotoAuthorSchema,
    PlaceSchema,
    TransportToNextSchema,
)
from app.services.maps_cost import MapsCostMetrics, classify_maps_sku
from app.services.route_guidance import (
    MAX_MEMO_LENGTH,
    ROUTE_FIELD_MASK,
    fallback_route_guidance,
    format_route_guidance,
)

PLACE_DETAIL_FIELDS = 'id,displayName,formattedAddress,addressComponents,location,businessStatus,primaryType,types,rating,regularOpeningHours'


class MapsProviderError(ValueError):
    """Sanitized provider failure used to choose bounded recovery.

    Args:
        kind: Transient outage, authorization failure, invalid response/request,
            or a valid response that supplies no route. ``route_data`` means
            unusable route facts and permits safe alternatives, unlike config errors.
        status_code: HTTP status when available; provider text is never retained.
    """

    def __init__(self, kind: Literal['transient', 'unauthorized', 'invalid', 'no_route', 'route_data'], status_code: int | None = None) -> None:
        self.kind = kind
        self.status_code = status_code
        super().__init__(f'Maps provider {kind} failure')

    @property
    def transient(self) -> bool:
        """Whether this failure permits a later bounded provider retry."""
        return self.kind == 'transient'


class NoRouteError(MapsProviderError):
    """A successful provider response did not supply a supported route."""

    def __init__(self) -> None:
        super().__init__('no_route')


def _route_metrics(data: dict) -> tuple[float, float]:
    """Validate actual route facts, separate from configuration and optional steps."""
    routes = data.get('routes', [])
    if not isinstance(routes, list):
        raise MapsProviderError('route_data')
    if not routes:
        raise NoRouteError()
    try:
        raw = routes[0]
        duration = raw['duration']
        distance_raw = raw['distanceMeters']
        if not isinstance(duration, str) or not re.fullmatch(r'\d+(?:\.\d+)?s', duration) or isinstance(distance_raw, bool) or not isinstance(distance_raw, (int, float)):
            raise MapsProviderError('route_data')
        seconds, distance = float(duration[:-1]), float(distance_raw)
        if not math.isfinite(seconds) or not math.isfinite(distance) or seconds <= 0 or distance <= 0:
            raise MapsProviderError('route_data')
        return seconds, distance
    except (KeyError, IndexError, TypeError, OverflowError):
        raise MapsProviderError('route_data') from None


def _cacheable_response(data: dict, url: str, options: dict[str, Any]) -> bool:
    """Admit nonempty, structurally usable responses to the request cache."""
    if url.endswith(':computeRoutes'):
        try:
            _route_metrics(data)
        except NoRouteError:
            return False
        return True
    if 'places' in data:
        places = data['places']
        if not isinstance(places, list) or any(not isinstance(place, dict) for place in places):
            raise MapsProviderError('invalid')
        mask = options.get('headers', {}).get('X-Goog-FieldMask', '')
        for place in places:
            if ('id' in place or mask == 'places.id') and (not isinstance(place.get('id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]+', place['id'])):
                if mask == 'places.id':
                    raise MapsProviderError('invalid')
                # Full venue records can be individually filtered, but the
                # malformed response must not become a reusable success.
                return False
        return bool(places)
    if url.startswith('https://places.googleapis.com/v1/places/') and data.get('id') != url.rsplit('/', 1)[-1]:
        raise MapsProviderError('invalid')
    return bool(data)


@dataclass(frozen=True)
class Destination:
    """Geocoded country and city viewport used to reject unrelated search hits."""

    country_code: str
    south: float
    west: float
    north: float
    east: float
    place_id: str = ''

    def contains(self, latitude: float, longitude: float) -> bool:
        """Check a point, including viewports that cross the date line."""
        longitude_ok = self.west <= longitude <= self.east if self.west <= self.east else longitude >= self.west or longitude <= self.east
        return self.south <= latitude <= self.north and longitude_ok


@dataclass(frozen=True)
class VerifiedPlace:
    """Provider facts and machine-readable regular opening periods (None unknown)."""

    place: PlaceSchema
    periods: list[dict] | None = None


@dataclass(frozen=True)
class VerifiedPhoto:
    """Actual provider media URI and structured source/author attribution."""

    url: str
    attribution: PhotoAttributionSchema


def _photo_uri(value: Any, api_key: str, image: bool = False) -> str | None:
    """Accept credential-free HTTPS Google image/source links only."""
    if not isinstance(value, str) or not value or any(ord(char) < 32 or char.isspace() for char in value):
        return None
    value = 'https:' + value if value.startswith('//') else value
    decoded = unquote(value)
    if any(ord(char) < 32 for char in decoded) or (api_key and api_key in decoded):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        domains = ('googleusercontent.com',) if image else ('google.com', 'googleusercontent.com')
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.port not in {None, 443} or not any(host == domain or host.endswith('.' + domain) for domain in domains):
            return None
        secrets = {'key', 'apikey', 'api_key', 'access_token', 'token', 'credential', 'credentials'}
        if any(key.casefold() in secrets for key, _ in parse_qsl(parsed.query)):
            return None
        return value
    except ValueError:
        return None


def _photo_attribution(raw: dict, api_key: str) -> PhotoAttributionSchema | None:
    """Validate supplied source/author fields and retain them in dedicated DTOs."""
    source = _photo_uri(raw.get('googleMapsUri'), api_key)
    if source is None:
        return None
    authors = raw.get('authorAttributions', [])
    if not isinstance(authors, list):
        return None
    validated_authors = []
    for author in authors:
        if not isinstance(author, dict) or not isinstance(author.get('displayName'), str):
            return None
        name = ' '.join(re.sub(r'<[^>]*>', '', author['displayName']).split())
        if not name or (api_key and api_key in name):
            return None
        fields = {'displayName': name}
        for key in ('uri', 'photoUri'):
            if key in author:
                uri = _photo_uri(author[key], api_key, image=key == 'photoUri')
                if uri is None:
                    return None
                fields[key] = uri
        validated_authors.append(PhotoAuthorSchema(**fields))
    return PhotoAttributionSchema(googleMapsUri=source, authorAttributions=validated_authors)


def _country(components: list[dict]) -> str:
    for item in components:
        if 'country' in item.get('types', []):
            return item.get('shortText') or item.get('short_name', '')
    return ''


def _normalized(value: str) -> str:
    return ''.join(char for char in unicodedata.normalize('NFKC', value).casefold() if char.isalnum())


def _destination_query(value: str) -> str:
    """Clean search whitespace and Unicode width without translating names."""
    return ' '.join(unicodedata.normalize('NFKC', value).split())


def _destination_types(raw: dict) -> set[str]:
    """Return the record's own geographic types, never its parent types."""
    types = raw.get('types', [])
    return set(types) if isinstance(types, list) and all(isinstance(kind, str) for kind in types) else set()


def _destination_id(raw: dict) -> str | None:
    """Require the actual provider identifier character contract."""
    identifier = raw.get('id')
    return identifier if isinstance(identifier, str) and re.fullmatch(r'[A-Za-z0-9_-]+', identifier) else None


def _destination_country_code(raw: dict) -> str | None:
    """Require one consistent uppercase ISO2 country component code."""
    components = raw.get('addressComponents', [])
    if not isinstance(components, list):
        return None
    codes: set[str] = set()
    for component in components:
        if not isinstance(component, dict):
            return None
        types = component.get('types', [])
        if not isinstance(types, list):
            return None
        if 'country' not in types:
            continue
        code = component.get('shortText')
        if not isinstance(code, str) or not re.fullmatch(r'[A-Z]{2}', code):
            return None
        codes.add(code)
    return next(iter(codes)) if len(codes) == 1 else None


def _destination_bounds(raw: dict) -> tuple[float, float, float, float] | None:
    """Validate finite numeric viewport coordinates, including date-line bounds."""
    try:
        viewport = raw['viewport']
        values = (viewport['low']['latitude'], viewport['low']['longitude'], viewport['high']['latitude'], viewport['high']['longitude'])
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
            return None
        south, west, north, east = values
        if not -90 <= south <= north <= 90 or not -180 <= west <= 180 or not -180 <= east <= 180:
            return None
        return tuple(float(value) for value in values)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _same_name(candidate: Any, name: str) -> bool:
    # Parenthetical official aliases are safe only when the remaining title matches
    # exactly. Substrings would confuse e.g. a museum with its nearby gift shop.
    actual_names = {_normalized(name), _normalized(re.sub(r'\([^)]*\)', '', name))}
    return any(_normalized(proposed) in actual_names for proposed in (candidate.name, candidate.english_name) if proposed.strip())


def _geographic_area(types: list[str]) -> bool:
    """Reject area centroids and address/road records as individual visit venues."""
    excluded = {'neighborhood', 'locality', 'postal_town', 'colloquial_area', 'country', 'continent', 'political', 'route', 'intersection', 'geocode', 'street_address', 'plus_code'}
    return any(kind in excluded or kind.startswith(('sublocality', 'administrative_area', 'postal_code')) for kind in types)


def meal_category_supported(category: str, meal: str) -> bool:
    """Require a concrete meal-serving business, consistently across all stages.

    A market, generic food tag, or dessert venue does not establish lunch/dinner
    service. Primary business types take precedence over broad secondary tags.
    """
    if meal == 'none':
        return True
    if category == 'breakfast_restaurant':
        return meal == 'breakfast'
    if category == 'dessert_restaurant':
        return False
    if category == 'brunch_restaurant':
        return meal in {'breakfast', 'lunch'}
    if category in {'restaurant', 'food_court', 'meal_takeaway', 'noodle_shop', 'sandwich_shop'} or category.endswith('_restaurant'):
        return True
    return meal == 'breakfast' and category in {'cafe', 'coffee_shop', 'bakery'}


def _transit_node(types: list[str]) -> bool:
    """A transport access point cannot stand in for a similarly named attraction."""
    excluded = {'bus_stop', 'bus_station', 'train_station', 'subway_station', 'transit_station', 'light_rail_station', 'transit_depot', 'ferry_terminal', 'taxi_stand', 'airport', 'international_airport', 'regional_airport', 'heliport'}
    return any(kind in excluded for kind in types)


def individual_place_category_supported(category: str) -> bool:
    """Reject geographic records and transport access points at adapter boundaries."""
    return not (_geographic_area([category]) or _transit_node([category]))


def _place_category(raw: dict) -> str:
    """Prefer the primary business; otherwise retain a specific provider type."""
    if raw.get('primaryType'):
        return raw['primaryType']
    types = raw.get('types', [])
    return next((kind for kind in types if kind not in {'point_of_interest', 'establishment', 'food', 'store'}), next(iter(types), 'place'))


def _verified_place(raw: dict, destination: Destination, meal: str, candidate: Any | None = None) -> VerifiedPlace | None:
    """Validate provider venue facts, optionally requiring a proposed exact name.

    Discovery uses official provider names directly; candidate verification keeps
    its strict identity check. Invalid facts return no venue and invent nothing.
    """
    try:
        types = raw.get('types', []) + [raw.get('primaryType', '')]
        if _geographic_area(types) or _transit_node(types):
            return None
        category = _place_category(raw)
        if not meal_category_supported(category, meal):
            return None
        name = raw['displayName']['text']
        identifier, address = raw.get('id'), raw.get('formattedAddress')
        if not isinstance(name, str) or not name.strip() or not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', identifier) or not isinstance(address, str) or not address.strip():
            return None
        latitude, longitude = float(raw['location']['latitude']), float(raw['location']['longitude'])
        if not all(math.isfinite(value) for value in (latitude, longitude)) or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            return None
        if not destination.contains(latitude, longitude) or _country(raw.get('addressComponents', [])) != destination.country_code:
            return None
        if raw.get('businessStatus', '').startswith('CLOSED') or (candidate is not None and not _same_name(candidate, name)):
            return None
        hours = raw.get('regularOpeningHours', {})
        rating = raw.get('rating')
        if rating is not None and (not isinstance(rating, (int, float)) or not math.isfinite(rating) or not 0 <= rating <= 5):
            rating = None
        place = PlaceSchema(placeId=identifier, placeName=name, placeEngName=name if name.isascii() and re.search('[A-Za-z]', name) else '', category=category, address=address, latitude=latitude, longitude=longitude, rating=rating, photoUrl='', openingHours=hours.get('weekdayDescriptions') or [])
        return VerifiedPlace(place, hours.get('periods'))
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return None


class VerifiedMapsProvider:
    """Request-scoped client with bounded, deduplicated asynchronous Maps calls.

    Args:
        client: Optional injected HTTP client; caller retains ownership.
        api_key: Optional configured Google Maps key.
        concurrency: Maximum active provider requests.

    Raises:
        ValueError: Provider cannot establish a destination, venue, or route.
    """

    def __init__(self, client: httpx.AsyncClient | None = None, api_key: str | None = None, concurrency: int = 6) -> None:
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(6))
        self.owns_client = client is None
        self.api_key = settings.GOOGLE_MAPS_API_KEY if api_key is None else api_key
        self.semaphore = asyncio.Semaphore(max(1, min(concurrency, 8)))
        self.cache: dict[str, dict] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.metrics = MapsCostMetrics()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> None:
        if self.owns_client:
            await self.client.aclose()

    async def _request(self, method: str, url: str, *, use_cache: bool = True, **kwargs: Any) -> dict:
        import json
        sku = classify_maps_sku(method, url, kwargs)
        self.metrics.record_call(sku)
        key = json.dumps([method, url, kwargs], sort_keys=True) if use_cache else None
        lock = self.locks.setdefault(key, asyncio.Lock()) if use_cache else nullcontext()
        async with lock:
            if use_cache and key in self.cache:
                self.metrics.record_cache_hit(sku)
                return self.cache[key]
            for attempt in range(2):
                sent = False
                try:
                    async with self.semaphore:
                        self.metrics.record_request(sku)
                        sent = True
                        response = await self.client.request(method, url, **kwargs)
                        self.metrics.record_response(sku, response.status_code)
                        status = response.status_code
                        if status in {401, 403}:
                            raise MapsProviderError('unauthorized', status)
                        if status == 429 or 500 <= status <= 599:
                            raise MapsProviderError('transient', status)
                        if status >= 400 or status < 200 or status >= 300:
                            raise MapsProviderError('invalid', status)
                        try:
                            data = response.json()
                        except ValueError:
                            kind = 'route_data' if url.endswith(':computeRoutes') else 'invalid'
                            raise MapsProviderError(kind, status) from None
                        if isinstance(data, dict) and 'error' in data:
                            raise MapsProviderError('invalid', status)
                        if not isinstance(data, dict):
                            kind = 'route_data' if url.endswith(':computeRoutes') else 'invalid'
                            raise MapsProviderError(kind, status)
                        cacheable = use_cache and _cacheable_response(data, url, kwargs)
                except asyncio.CancelledError:
                    if sent:
                        self.metrics.record_error(sku)
                    raise
                except (httpx.TimeoutException, httpx.NetworkError):
                    self.metrics.record_error(sku)
                    failure = MapsProviderError('transient')
                except MapsProviderError as error:
                    self.metrics.record_error(sku)
                    failure = error
                except (httpx.HTTPError, ValueError):
                    self.metrics.record_error(sku)
                    # Do not surface exception URLs: geocoding URLs contain a key.
                    raise MapsProviderError('invalid') from None
                else:
                    if cacheable:
                        self.cache[key] = data
                    return data
                if not failure.transient or attempt == 1:
                    raise failure from None
                # Capacity is released during backoff; cancellation stays prompt.
                await asyncio.sleep(.2)
        raise MapsProviderError('transient')

    async def photo(self, place_id: str) -> VerifiedPhoto | None:
        """Fetch one fresh actual place image with complete available attribution.

        Args:
            place_id: A final verified provider ID, optionally prefixed places/.
        Returns:
            Actual media URL and source DTO, or None for optional photo failures.
        Raises:
            asyncio.CancelledError: External cancellation is always propagated.
        """
        identifier = place_id.removeprefix('places/')
        if not re.fullmatch(r'[A-Za-z0-9_-]+', identifier):
            return None
        try:
            metadata = await self._request('GET', f'https://places.googleapis.com/v1/places/{identifier}', use_cache=False, headers={'X-Goog-Api-Key': self.api_key, 'X-Goog-FieldMask': 'id,photos'})
            if metadata.get('id') != identifier or not isinstance(metadata.get('photos', []), list):
                return None
            for raw in metadata.get('photos', []):
                if not isinstance(raw, dict):
                    continue
                name = raw.get('name', '')
                if not isinstance(name, str) or not re.fullmatch(rf'places/{re.escape(identifier)}/photos/[A-Za-z0-9_-]+', name):
                    continue
                attribution = _photo_attribution(raw, self.api_key)
                if attribution is None:
                    continue
                media = await self._request('GET', f'https://places.googleapis.com/v1/{name}/media', use_cache=False, headers={'X-Goog-Api-Key': self.api_key}, params={'maxWidthPx': 1200, 'skipHttpRedirect': 'true'})
                if 'name' in media and media['name'] != name + '/media':
                    return None
                uri = _photo_uri(media.get('photoUri'), self.api_key, image=True)
                avatars = {_photo_uri(author.get('photoUri'), self.api_key, image=True) for author in raw.get('authorAttributions', [])}
                if uri in avatars:
                    return None
                return VerifiedPhoto(uri, attribution) if uri is not None else None
            return None
        except (ValueError, httpx.HTTPError, KeyError, TypeError, AttributeError):
            return None

    async def resolve_destination(self, country: str, city: str) -> Destination:
        """Resolve a single actual geographic entity without comparing its names.

        Args:
            country: Original country search text, cleaned only for Unicode space.
            city: Original city search text, cleaned only for Unicode space.
        Returns:
            Verified country code, destination ID and finite viewport bounds.
        Raises:
            ValueError: Blank input, missing geographic facts or ambiguous results.
            MapsProviderError: Provider request or authorization failure.

        Names returned by Maps may be in a different language. Its interpretation
        remains subject to own geographic type, identity, country and boundary
        validation; this does not establish an exact administrative polygon.
        """
        country_query, city_query = _destination_query(country), _destination_query(city)
        if not country_query or not city_query:
            raise ValueError('Destination search text is empty')
        url = 'https://places.googleapis.com/v1/places:searchText'
        headers = {'X-Goog-Api-Key': self.api_key, 'X-Goog-FieldMask': 'places.id,places.displayName,places.addressComponents,places.types,places.viewport'}
        async def lookup(query: str, language: str) -> dict:
            return await self._request('POST', url, headers=headers, json={'textQuery': query, 'languageCode': language, 'pageSize': 5})
        country_data, city_data = await asyncio.gather(
            lookup(country_query, 'ko' if re.search('[가-힣]', country_query) else 'en'),
            lookup(f'{city_query}, {country_query}', 'ko' if re.search('[가-힣]', city_query) else 'en'),
            return_exceptions=True,
        )
        results = (country_data, city_data)
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
        failures = [result for result in results if isinstance(result, MapsProviderError)]
        if failures:
            # A permanent boundary failure must not be hidden by another outage.
            raise min(failures, key=lambda failure: failure.transient)
        for result in results:
            if isinstance(result, BaseException):
                raise MapsProviderError('invalid') from None
        countries: list[tuple[str, str]] = []
        for raw in country_data.get('places', []):
            identifier, code = _destination_id(raw), _destination_country_code(raw)
            if 'country' in _destination_types(raw) and identifier is not None and code is not None:
                countries.append((identifier, code))
        if len(countries) != 1:
            raise ValueError('Country could not be unambiguously verified')
        country_id, expected = countries[0]
        # Query whitespace cleanup cannot establish identical input intent.
        same_input = unicodedata.normalize('NFKC', country).casefold().strip() == unicodedata.normalize('NFKC', city).casefold().strip()
        geographic_types = {'locality', 'postal_town', 'administrative_area_level_1', 'administrative_area_level_2'}
        matches: list[Destination] = []
        for raw in city_data.get('places', []):
            identifier, types = _destination_id(raw), _destination_types(raw)
            if identifier is None or _destination_country_code(raw) != expected:
                continue
            if 'country' in types:
                if not same_input or identifier != country_id:
                    continue
            elif not types & geographic_types:
                continue
            bounds = _destination_bounds(raw)
            if bounds is None:
                continue
            matches.append(Destination(expected, *bounds, place_id=identifier))
        if len(matches) != 1:
            raise ValueError('City could not be unambiguously verified')
        return matches[0]

    async def search(self, candidate: Any, destination: Destination) -> VerifiedPlace:
        """Resolve a named candidate only when identity, region, and facts match."""
        headers = {
            'X-Goog-Api-Key': self.api_key,
            'X-Goog-FieldMask': 'places.id',
        }
        queries = [(candidate.name, 'ko')]
        if candidate.english_name and _normalized(candidate.english_name) != _normalized(candidate.name):
            queries.append((candidate.english_name, 'en'))
        for query, language in queries:
            body = {
                'textQuery': query,
                'languageCode': language,
                'pageSize': 5,
                'locationBias': {'rectangle': {'low': {'latitude': destination.south, 'longitude': destination.west}, 'high': {'latitude': destination.north, 'longitude': destination.east}}},
            }
            search_url = 'https://places.googleapis.com/v1/places:searchText'
            data = await self._request('POST', search_url, headers=headers, json=body)
            entries = data.get('places', [])
            if not isinstance(entries, list) or any(not isinstance(entry, dict) or not isinstance(entry.get('id'), str) or not re.fullmatch(r'[A-Za-z0-9_-]+', entry['id']) for entry in entries):
                raise ValueError('Invalid Maps place ID response')
            ids = {entry['id'] for entry in entries}
            if not ids:
                continue
            if len(ids) == 1:
                place_id = next(iter(ids))
                details = await self._request('GET', f'https://places.googleapis.com/v1/places/{place_id}', headers={'X-Goog-Api-Key': self.api_key, 'X-Goog-FieldMask': PLACE_DETAIL_FIELDS}, params={'languageCode': language})
                if details.get('id') != place_id:
                    raise ValueError('Maps detail identity does not match the requested place')
                data = {'places': [details]}
            else:
                # Preserve the original multi-result ambiguity checks. A first ID
                # is never selected just because its detail request would be cheap.
                full_headers = {**headers, 'X-Goog-FieldMask': ','.join(f'places.{field}' for field in PLACE_DETAIL_FIELDS.split(','))}
                data = await self._request('POST', search_url, headers=full_headers, json=body)
            matches: dict[str, VerifiedPlace] = {}
            for raw in data.get('places', []):
                verified = _verified_place(raw, destination, candidate.meal, candidate)
                if verified is not None:
                    matches[verified.place.placeId] = verified
            if len(matches) > 1:
                raise ValueError(f'Ambiguous named venue: {candidate.name}')
            if matches:
                return next(iter(matches.values()))
        raise ValueError(f'No verified named venue: {candidate.name}')

    async def discover_meals(self, destination: Destination, anchor: VerifiedPlace) -> list[VerifiedPlace]:
        """Discover at most five actual meal venues within 1 km of an attraction.

        Args:
            destination: Verified country/city boundary.
            anchor: Verified nearby visit whose coordinates bound the search.
        Returns:
            Unique provider restaurants supporting lunch and dinner, nearest first.
            An empty list means no suitable verified restaurant was supplied.
        Raises:
            MapsProviderError: Invalid anchor or provider failure.
        """
        from app.services.course_routing import distance_meters

        latitude, longitude = anchor.place.latitude, anchor.place.longitude
        if not all(math.isfinite(value) for value in (latitude, longitude)) or not -90 <= latitude <= 90 or not -180 <= longitude <= 180 or not destination.contains(latitude, longitude):
            raise MapsProviderError('invalid')
        data = await self._request('POST', 'https://places.googleapis.com/v1/places:searchText', headers={
            'X-Goog-Api-Key': self.api_key,
            'X-Goog-FieldMask': ','.join(f'places.{field}' for field in PLACE_DETAIL_FIELDS.split(',')),
        }, json={
            'textQuery': '음식점' if destination.country_code == 'KR' else 'restaurants',
            'languageCode': 'ko' if destination.country_code == 'KR' else 'en',
            'pageSize': 10,
            'locationBias': {'circle': {'center': {'latitude': latitude, 'longitude': longitude}, 'radius': 1000}},
        })
        matches: dict[str, VerifiedPlace] = {}
        for raw in data.get('places', []):
            verified = _verified_place(raw, destination, 'lunch')
            if verified is not None and meal_category_supported(verified.place.category, 'dinner') and distance_meters(anchor, verified) <= 1000:
                matches[verified.place.placeId] = verified
        return sorted(matches.values(), key=lambda venue: distance_meters(anchor, venue))[:5]

    async def route(self, origin: VerifiedPlace, destination: VerifiedPlace, mode: str = 'walking', departure_time: datetime | None = None) -> TransportToNextSchema:
        """Return provider route metrics at an explicitly supplied transit departure.

        Args:
            origin: Verified departure venue.
            destination: Verified arrival venue.
            mode: Existing public transport type.
            departure_time: Aware travel-date timestamp for transit, if known.
        Returns:
            Actual provider metrics and optional, sanitized navigation guidance.
        Raises:
            NoRouteError: Valid provider response supplies no route.
            MapsProviderError: Authorization, request, response, or outage error.
        """
        modes = {'walking': 'WALK', 'transit': 'TRANSIT', 'driving': 'DRIVE', 'taxi': 'DRIVE'}
        if mode not in modes:
            raise MapsProviderError('invalid')
        body = {'origin': {'placeId': origin.place.placeId.removeprefix('places/')}, 'destination': {'placeId': destination.place.placeId.removeprefix('places/')}, 'travelMode': modes[mode], 'languageCode': 'ko'}
        if departure_time is not None:
            if departure_time.tzinfo is None or departure_time.utcoffset() is None:
                raise MapsProviderError('invalid')
            if mode == 'transit':
                body['departureTime'] = departure_time.astimezone(UTC).isoformat().replace('+00:00', 'Z')
        data = await self._request('POST', 'https://routes.googleapis.com/directions/v2:computeRoutes', headers={
            'X-Goog-Api-Key': self.api_key, 'X-Goog-FieldMask': ROUTE_FIELD_MASK,
        }, json=body)
        seconds, distance = _route_metrics(data)
        # Guidance is optional: only validated numeric route facts determine
        # feasibility. Cancellation remains observable (it is BaseException).
        try:
            memo = format_route_guidance(data, mode, destination.place.placeName, departure_time)
            if not isinstance(memo, str) or not memo.strip() or len(memo) > MAX_MEMO_LENGTH:
                raise ValueError('Unusable optional route guidance')
        except Exception:  # noqa: BLE001 - optional presentation must not invalidate verified metrics
            memo = fallback_route_guidance(mode, destination.place.placeName, departure_time)
        return TransportToNextSchema(type=mode, distance=distance, minutes=math.ceil(seconds / 60), cost=0 if mode == 'walking' else None, memo=memo)

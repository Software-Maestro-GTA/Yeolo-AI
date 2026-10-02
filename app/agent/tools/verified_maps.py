"""Strict provider boundary for course generation; failures never invent facts."""

import asyncio
import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Self

import httpx

from app.core.config import settings
from app.schemas.course import PlaceSchema, TransportToNextSchema
from app.services.maps_cost import MapsCostMetrics, classify_maps_sku

PLACE_DETAIL_FIELDS = 'id,displayName,formattedAddress,addressComponents,location,businessStatus,primaryType,types,rating,regularOpeningHours'


@dataclass(frozen=True)
class Destination:
    """Geocoded country and city viewport used to reject unrelated search hits."""

    country_code: str
    south: float
    west: float
    north: float
    east: float

    def contains(self, latitude: float, longitude: float) -> bool:
        """Check a point, including viewports that cross the date line."""
        longitude_ok = self.west <= longitude <= self.east if self.west <= self.east else longitude >= self.west or longitude <= self.east
        return self.south <= latitude <= self.north and longitude_ok


@dataclass(frozen=True)
class VerifiedPlace:
    """Provider facts and machine-readable regular opening periods (None unknown)."""

    place: PlaceSchema
    periods: list[dict] | None = None


def _country(components: list[dict]) -> str:
    for item in components:
        if 'country' in item.get('types', []):
            return item.get('shortText') or item.get('short_name', '')
    return ''


def _normalized(value: str) -> str:
    return ''.join(char for char in unicodedata.normalize('NFKC', value).casefold() if char.isalnum())


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
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(12))
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

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict:
        import json
        sku = classify_maps_sku(method, url, kwargs)
        self.metrics.record_call(sku)
        key = json.dumps([method, url, kwargs], sort_keys=True)
        async with self.locks.setdefault(key, asyncio.Lock()):
            if key in self.cache:
                self.metrics.record_cache_hit(sku)
                return self.cache[key]
            async with self.semaphore:
                try:
                    self.metrics.record_request(sku)
                    response = await self.client.request(method, url, **kwargs)
                    self.metrics.record_response(sku, response.status_code)
                    response.raise_for_status()
                    data = response.json()
                    if not isinstance(data, dict) or data.get('error'):
                        raise ValueError('Invalid Maps provider response')
                except asyncio.CancelledError:
                    self.metrics.record_error(sku)
                    raise
                except (httpx.HTTPError, ValueError):
                    self.metrics.record_error(sku)
                    # Do not surface exception URLs: geocoding URLs contain a key.
                    raise ValueError('Maps provider request failed') from None
                self.cache[key] = data
                return data

    async def resolve_destination(self, country: str, city: str) -> Destination:
        """Resolve country and city; require matching country components and bounds."""
        url = 'https://places.googleapis.com/v1/places:searchText'
        headers = {'X-Goog-Api-Key': self.api_key, 'X-Goog-FieldMask': 'places.id,places.displayName,places.addressComponents,places.types,places.viewport'}
        async def lookup(query: str, language: str) -> dict:
            return await self._request('POST', url, headers=headers, json={'textQuery': query, 'languageCode': language, 'pageSize': 5})
        country_data, city_data = await asyncio.gather(
            lookup(country, 'ko' if re.search('[가-힣]', country) else 'en'),
            lookup(f'{city}, {country}', 'ko' if re.search('[가-힣]', city) else 'en'),
            return_exceptions=True,
        )
        if isinstance(country_data, BaseException) or isinstance(city_data, BaseException):
            raise ValueError('Destination provider lookup failed')  # noqa: TRY004 - collected provider failure
        codes = set()
        for raw in country_data.get('places', []):
            for component in raw.get('addressComponents', []):
                if 'country' in component.get('types', []) and _normalized(country) in {_normalized(component.get('longText', '')), _normalized(component.get('shortText', ''))}:
                    codes.add(component.get('shortText'))
        if len(codes) != 1:
            raise ValueError('Country could not be verified')
        expected = codes.pop()
        matches = []
        for raw in city_data.get('places', []):
            if _country(raw.get('addressComponents', [])) != expected or not set(raw.get('types', [])) & {'locality', 'administrative_area_level_1', 'administrative_area_level_2', 'postal_town'}:
                continue
            city_names = [raw.get('displayName', {}).get('text', '')]
            city_names.extend(component.get('longText', '') for component in raw.get('addressComponents', []) if set(component.get('types', [])) & {'locality', 'administrative_area_level_1', 'administrative_area_level_2', 'postal_town'})
            if not any(_normalized(city) == _normalized(name) for name in city_names):
                continue
            matches.append(raw)
        try:
            if len(matches) != 1:
                raise ValueError('City could not be unambiguously verified')
            viewport = matches[0]['viewport']
            result = Destination(expected, viewport['low']['latitude'], viewport['low']['longitude'], viewport['high']['latitude'], viewport['high']['longitude'])
            if not all(math.isfinite(value) for value in (result.south, result.west, result.north, result.east)) or not -90 <= result.south <= result.north <= 90 or not all(-180 <= value <= 180 for value in (result.west, result.east)):
                raise ValueError('Invalid destination bounds')
            return result
        except (KeyError, IndexError, TypeError):
            raise ValueError('Incomplete destination response') from None

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
                try:
                    types = raw.get('types', []) + [raw.get('primaryType', '')]
                    if _geographic_area(types) or _transit_node(types):
                        continue
                    category = _place_category(raw)
                    if not meal_category_supported(category, candidate.meal):
                        continue
                    name = raw['displayName']['text']
                    latitude, longitude = float(raw['location']['latitude']), float(raw['location']['longitude'])
                    if not all(math.isfinite(value) for value in (latitude, longitude)) or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
                        continue
                    if not destination.contains(latitude, longitude) or _country(raw.get('addressComponents', [])) != destination.country_code:
                        continue
                    if raw.get('businessStatus', '').startswith('CLOSED') or not _same_name(candidate, name):
                        continue
                    if not raw.get('id') or not raw.get('formattedAddress'):
                        continue
                    hours = raw.get('regularOpeningHours', {})
                    rating = raw.get('rating')
                    if rating is not None and (not isinstance(rating, (int, float)) or not math.isfinite(rating) or not 0 <= rating <= 5):
                        rating = None
                    place = PlaceSchema(placeId=raw['id'], placeName=name, placeEngName=name if name.isascii() and re.search('[A-Za-z]', name) else '', category=category, address=raw['formattedAddress'], latitude=latitude, longitude=longitude, rating=rating, photoUrl='', openingHours=hours.get('weekdayDescriptions') or [])
                    matches[place.placeId] = VerifiedPlace(place, hours.get('periods'))
                except (KeyError, TypeError, ValueError):
                    continue
            if len(matches) > 1:
                raise ValueError(f'Ambiguous named venue: {candidate.name}')
            if matches:
                return next(iter(matches.values()))
        raise ValueError(f'No verified named venue: {candidate.name}')

    async def route(self, origin: VerifiedPlace, destination: VerifiedPlace, mode: str = 'walking') -> TransportToNextSchema:
        """Return provider-confirmed distance/time; absent or invalid routes fail."""
        modes = {'walking': 'WALK', 'transit': 'TRANSIT', 'driving': 'DRIVE', 'taxi': 'DRIVE'}
        if mode not in modes:
            raise ValueError('Unsupported transport mode')
        data = await self._request('POST', 'https://routes.googleapis.com/directions/v2:computeRoutes', headers={
            'X-Goog-Api-Key': self.api_key, 'X-Goog-FieldMask': 'routes.duration,routes.distanceMeters',
        }, json={'origin': {'placeId': origin.place.placeId.removeprefix('places/')}, 'destination': {'placeId': destination.place.placeId.removeprefix('places/')}, 'travelMode': modes[mode]})
        try:
            raw = data['routes'][0]
            duration = raw['duration']
            if not isinstance(duration, str) or not re.fullmatch(r'\d+(?:\.\d+)?s', duration):
                raise ValueError('Invalid route duration')
            seconds, distance = float(duration[:-1]), float(raw['distanceMeters'])
            if not math.isfinite(seconds) or not math.isfinite(distance) or seconds <= 0 or distance <= 0:
                raise ValueError('Invalid route metrics')
        except (KeyError, IndexError, TypeError):
            raise ValueError('No verified route') from None
        label = {'walking': '도보', 'transit': '대중교통', 'driving': '차량', 'taxi': '택시'}[mode]
        return TransportToNextSchema(type=mode, distance=distance, minutes=math.ceil(seconds / 60), cost=0 if mode == 'walking' else None, memo=f'{origin.place.placeName} → {destination.place.placeName}: {label}. 조회 시점 경로 예상치이며 여행일 운행·혼잡·요금은 재확인이 필요합니다.')

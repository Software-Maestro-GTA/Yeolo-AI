"""Strict provider boundary for course generation; failures never invent facts."""

import asyncio
import logging
import math
import re
import unicodedata
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, Self

import httpx
from pydantic import Field

from app.core.config import settings
from app.schemas.course import (
    PlaceSchema,
    TransportToNextSchema,
)
from app.schemas.course_anchors import AirportCandidate
from app.services.maps_cost import MapsCostMetrics, classify_maps_sku
from app.services.myrealtrip_quotes import AirportIdentity
from app.services.place_categories import AIRPORT_TYPES, LODGING_TYPES
from app.services.route_guidance import (
    MAX_MEMO_LENGTH,
    ROUTE_FIELD_MASK,
    format_route_guidance,
    navigation_source,
)

# Text Search retrieves free provider IDs. These two detail masks request only
# Essentials facts; names, primary types and operating status require Pro.
PLACE_SEARCH_MASK = 'places.id'
PLACE_DETAIL_FIELDS = 'id,formattedAddress,addressComponents,location,types'
DESTINATION_DETAIL_FIELDS = 'id,addressComponents,types,viewport'
MAX_SEARCH_IDS = 5
MAX_PROPOSED_AIRPORT_DISTANCE_METERS = 200_000
logger = logging.getLogger(__name__)


class MapsProviderError(ValueError):
    """Sanitized provider failure used to choose bounded recovery.

    Args:
        kind: Transient outage, authorization failure, invalid response/request,
            or a valid response that supplies no route. ``not_found`` identifies
            a deleted detail ID; ``place_data`` identifies unusable detail facts.
            These permit skipping a venue without hiding request/configuration
            errors. ``route_data`` similarly permits safe route alternatives.
        status_code: HTTP status when available; provider text is never retained.
    """

    def __init__(self, kind: Literal['transient', 'unauthorized', 'invalid', 'not_found', 'place_data', 'no_route', 'route_data'], status_code: int | None = None) -> None:
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


def _cacheable_response(data: dict, url: str) -> bool:
    """Admit nonempty, structurally usable responses to the request cache."""
    if url.endswith(':computeRoutes'):
        try:
            _route_metrics(data)
        except NoRouteError:
            return False
        return True
    if 'places' in data:
        places = data['places']
        if not isinstance(places, list):
            raise MapsProviderError('invalid')
        if any(not isinstance(place, dict) or _destination_id(place) is None for place in places):
            # Individual malformed IDs are filtered before details requests, but
            # a partially invalid response must not become a cached success.
            return False
        return bool(places)
    if url.startswith('https://places.googleapis.com/v1/places/') and data.get('id') != url.rsplit('/', 1)[-1]:
        raise MapsProviderError('place_data')
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
    verified_country_name: str = ''
    verified_city_name: str = ''

    def contains(self, latitude: float, longitude: float) -> bool:
        """Check a point, including viewports that cross the date line."""
        longitude_ok = self.west <= longitude <= self.east if self.west <= self.east else longitude >= self.west or longitude <= self.east
        return self.south <= latitude <= self.north and longitude_ok


def _distance_to_destination(latitude: float, longitude: float, destination: Destination) -> float:
    """Measure airport proximity to a city viewport, including date-line crossings.

    This geographic bound checks candidate plausibility only. It cannot establish
    road access, an airport's service area or available flights.
    """
    if destination.contains(latitude, longitude):
        return 0.0
    longitude_inside = (destination.west <= longitude <= destination.east if destination.west <= destination.east
                        else longitude >= destination.west or longitude <= destination.east)
    delta = 0.0 if longitude_inside else min(abs((longitude - edge + 180) % 360 - 180)
                                            for edge in (destination.west, destination.east))
    latitude_radians, delta_radians = math.radians(latitude), math.radians(delta)
    # Project onto the nearest bounding meridian on the sphere, then restrict
    # latitude to the viewport. Both corners cover a projection beyond a pole.
    projected = math.degrees(math.atan2(math.sin(latitude_radians), math.cos(latitude_radians) * math.cos(delta_radians)))
    nearest = min(max(projected, destination.south), destination.north)
    distances = []
    for boundary_latitude in (destination.south, destination.north, nearest):
        boundary_radians = math.radians(boundary_latitude)
        haversine = (math.sin((latitude_radians - boundary_radians) / 2) ** 2
                     + math.cos(latitude_radians) * math.cos(boundary_radians) * math.sin(delta_radians / 2) ** 2)
        distances.append(6_371_000 * 2 * math.asin(math.sqrt(max(0.0, min(1.0, haversine)))))
    return min(distances)


class InternalPlaceSchema(PlaceSchema):
    """Minimal public venue plus provider facts used only during planning.

    ``category`` retains the provider type for meal/visit/copy verification; its
    inherited serializer exposes the app group with explicit public context only.
    ``address`` is an Essentials provider fact. ``placeEngName`` retains the
    model's optional search name; neither name is officially verified here.
    These internal fields are not part of the public API response.
    """

    category: str = Field(..., description='Actual provider type for internal validation; serialized as an app category')
    providerTypes: list[str] = Field(default_factory=list, exclude=True, description='Actual Essentials types for display grouping only')
    address: str = Field(default='', description='Provider address for internal area matching')
    placeEngName: str = Field(default='', description='Optional proposed English search name; not officially verified')


class InternalTransportSchema(TransportToNextSchema):
    """Actual route with optional internal fare used by cost calculation."""

    cost: int | None = Field(default=None, ge=0, description='Known route fare in KRW; None is unknown')
    navigationSource: list[dict[str, str]] = Field(default_factory=list, exclude=True, description='Request-local original navigation for optional copy translation')


@dataclass(frozen=True)
class VerifiedPlace:
    """Essentials venue facts; operating status and opening periods are unknown."""

    place: PlaceSchema
    periods: list[dict] | None = None


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


def _destination_component_name(raw: dict, geographic_types: set[str]) -> str:
    """Keep a single supplied administrative name for cross-provider matching.

    Args:
        raw: Existing Essentials geographic detail response in a requested locale.
        geographic_types: Entity types whose own address component may name it.
    Returns:
        The one matching nonblank ``longText``, or an empty string when missing
        or ambiguous. No display-name lookup, translation, or alias is invented.
    """
    components = raw.get('addressComponents', [])
    if not isinstance(components, list):
        return ''
    names = set()
    for component in components:
        if not isinstance(component, dict):
            continue
        types, name = component.get('types', []), component.get('longText')
        if isinstance(types, list) and all(isinstance(kind, str) for kind in types) and geographic_types.intersection(types) and isinstance(name, str):
            cleaned = _destination_query(name)
            if cleaned:
                names.add(cleaned)
    return next(iter(names)) if len(names) == 1 else ''


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


def _geographic_area(types: list[str]) -> bool:
    """Reject area centroids and address/road records as individual visit venues."""
    excluded = {'neighborhood', 'locality', 'postal_town', 'colloquial_area', 'country', 'continent', 'political', 'route', 'intersection', 'geocode', 'street_address', 'plus_code'}
    return any(kind in excluded or kind.startswith(('sublocality', 'administrative_area', 'postal_code')) for kind in types)


def meal_category_supported(category: str, meal: str) -> bool:
    """Require a concrete meal-serving business, consistently across all stages.

    A market, generic food tag, or dessert venue does not establish lunch/dinner
    service. Restricted meal categories cannot be promoted by generic food tags.
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


TOURISM_CATEGORIES = frozenset({
    'museum', 'art_gallery', 'cultural_center', 'historical_landmark', 'historical_place', 'monument',
    'heritage_museum', 'history_museum', 'buddhist_temple', 'shinto_shrine', 'hindu_temple', 'mosque',
    'church', 'synagogue', 'library', 'park', 'national_park', 'state_park', 'nature_preserve',
    'botanical_garden', 'garden', 'hiking_area', 'beach', 'wildlife_park', 'aquarium', 'zoo',
    'planetarium', 'observation_deck', 'tourist_attraction', 'amusement_park', 'theme_park', 'water_park',
    'shopping_mall', 'department_store', 'shopping_center', 'clothing_store', 'book_store',
    'gift_shop', 'souvenir_store', 'market', 'spa', 'wellness_center', 'massage', 'sauna', 'yoga_studio',
    'night_club', 'bar', 'pub', 'wine_bar', 'dance_club', 'convention_center',
    'sports_complex', 'stadium', 'bowling_alley', 'golf_course', 'ski_resort',
    'performing_arts_theater', 'movie_theater',
})


def tourism_category_supported(category: str) -> bool:
    """Admit concrete provider categories that can serve as sightseeing visits."""
    return category in TOURISM_CATEGORIES


def individual_travel_category_supported(category: str) -> bool:
    """Require an actual sightseeing, meal or refreshment venue category.

    Args:
        category: Concrete type selected from an Essentials provider response.

    Returns:
        Whether the supplied category supports a course visit. Generic tags and
        unrelated businesses cannot consume a named candidate's first search hit.
    """
    refreshment_categories = {
        'cafe', 'coffee_shop', 'tea_house', 'bakery', 'dessert_shop',
        'dessert_restaurant', 'ice_cream_shop', 'chocolate_shop', 'pastry_shop',
    }
    return (
        tourism_category_supported(category)
        or meal_category_supported(category, 'breakfast')
        or meal_category_supported(category, 'lunch')
        or category in refreshment_categories
    )


def _place_category(raw: dict) -> str:
    """Select a supplied type without guessing a missing primary business type.

    A secondary generic restaurant tag cannot establish meal service for a
    market, dessert shop, breakfast-only shop or shopping complex. Mixed food
    types also keep an actual restaurant from being counted as a core attraction.
    """
    types = raw.get('types', [])
    restrictive = {
        'market', 'shopping_mall', 'shopping_center', 'supermarket',
        'breakfast_restaurant', 'dessert_restaurant', 'brunch_restaurant',
        'cafe', 'coffee_shop', 'bakery',
    }
    restricted = next((kind for kind in types if kind in restrictive), None)
    if restricted is not None:
        return restricted
    food_type = next((kind for kind in types if meal_category_supported(kind, 'lunch')), None)
    if food_type is not None:
        return food_type
    return next((kind for kind in types if kind and kind not in {'point_of_interest', 'establishment', 'food', 'store'}), '')


def _verified_place(raw: dict, destination: Destination, meal: str, candidate: Any, *, lodging: bool = False, airport: bool = False) -> VerifiedPlace | None:
    """Validate Essentials facts and preserve the model's proposed display names.

    Args:
        raw: Details returned for an actual ID from the free text search.
        destination: Verified country and finite destination viewport.
        meal: Proposed visit role checked against the supplied provider types.
        candidate: Named model proposal; its category is never used as evidence.
        lodging: Require a concrete lodging type instead of a sightseeing visit.
        airport: Require a concrete airport type without limiting its location to
            the city viewport. Generated candidates need a separate proximity check.
    Returns:
        Actual ID, category, coordinates and address with the proposed name, or
        None when venue facts fail. Official name/status/hours are not verified.
    """
    try:
        types = raw.get('types', [])
        if not isinstance(types, list) or any(not isinstance(kind, str) for kind in types):
            return None
        if _geographic_area(types) or (not airport and _transit_node(types)):
            return None
        lodging_category = next((kind for kind in types if kind in LODGING_TYPES), '')
        if airport:
            category = next((kind for kind in types if kind in AIRPORT_TYPES), '')
            if not category or meal != 'none' or lodging:
                return None
        elif lodging:
            if not lodging_category or meal != 'none':
                return None
            category = lodging_category
        else:
            category = _place_category(raw)
            if not category or not individual_travel_category_supported(category) or not meal_category_supported(category, meal):
                return None
        name = candidate.name
        identifier, address = raw.get('id'), raw.get('formattedAddress')
        if not isinstance(name, str) or not name.strip() or not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', identifier) or not isinstance(address, str) or not address.strip():
            return None
        coordinates = (raw['location']['latitude'], raw['location']['longitude'])
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in coordinates):
            return None
        latitude, longitude = map(float, coordinates)
        if not all(math.isfinite(value) for value in (latitude, longitude)) or not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
            return None
        if (not airport and not destination.contains(latitude, longitude)) or _destination_country_code(raw) != destination.country_code:
            return None
        place = InternalPlaceSchema(placeId=identifier, placeName=name, placeEngName=candidate.english_name or '', category=category, providerTypes=types, address=address, latitude=latitude, longitude=longitude)
        return VerifiedPlace(place)
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        return None


def _airport_search_match(name_ids: list[str], code_ids: list[str]) -> str | None:
    """Require matching leading hits; shared lower ranks cannot establish a name."""
    if not name_ids or not code_ids:
        return None
    if name_ids[0] == code_ids[0]:
        return code_ids[0]
    return None


class VerifiedMapsProvider:
    """Request-scoped client with bounded, deduplicated asynchronous Maps calls.

    Args:
        client: Optional injected HTTP client; caller retains ownership.
        api_key: Optional configured Google Maps key.
        concurrency: Maximum active provider requests.
        language: Legacy BCP 47 input; display and route instructions use Korean.

    Raises:
        ValueError: Provider cannot establish a destination, venue, or route.
    """

    def __init__(self, client: httpx.AsyncClient | None = None, api_key: str | None = None, concurrency: int = 6, language: str = 'ko') -> None:
        if not isinstance(language, str) or not re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', language):
            raise ValueError('Invalid Maps language code')
        self.language = 'ko'
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
        detail_request = method == 'GET' and url.startswith('https://places.googleapis.com/v1/places/')
        data_error_kind = 'route_data' if url.endswith(':computeRoutes') else 'place_data' if detail_request else 'invalid'
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
                        if detail_request and status in {404, 410}:
                            raise MapsProviderError('not_found', status)
                        if status >= 400 or status < 200 or status >= 300:
                            raise MapsProviderError('invalid', status)
                        try:
                            data = response.json()
                        except ValueError:
                            raise MapsProviderError(data_error_kind, status) from None
                        if isinstance(data, dict) and 'error' in data:
                            raise MapsProviderError('invalid', status)
                        if not isinstance(data, dict):
                            raise MapsProviderError(data_error_kind, status)
                        cacheable = use_cache and _cacheable_response(data, url)
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

    async def _search_ids(self, query: str, *, language: str, location_bias: dict | None = None) -> list[str]:
        """Retrieve at most five distinct IDs without requesting paid search fields."""
        body = {'textQuery': query, 'languageCode': language, 'pageSize': MAX_SEARCH_IDS}
        if location_bias is not None:
            body['locationBias'] = location_bias
        data = await self._request('POST', 'https://places.googleapis.com/v1/places:searchText',
            headers={'X-Goog-Api-Key': self.api_key, 'X-Goog-FieldMask': PLACE_SEARCH_MASK}, json=body)
        entries = data.get('places', [])
        if not isinstance(entries, list):
            raise MapsProviderError('invalid')
        identifiers = dict.fromkeys(identifier for entry in entries
            if isinstance(entry, dict) and (identifier := _destination_id(entry)) is not None)
        return list(identifiers)[:MAX_SEARCH_IDS]

    async def _details(self, identifier: str, *, destination: bool = False, language: str | None = None) -> dict:
        """Fetch strictly Essentials fields and require the requested response ID."""
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', identifier):
            raise MapsProviderError('invalid')
        data = await self._request('GET', f'https://places.googleapis.com/v1/places/{identifier}',
            headers={'X-Goog-Api-Key': self.api_key, 'X-Goog-FieldMask': DESTINATION_DETAIL_FIELDS if destination else PLACE_DETAIL_FIELDS},
            params={'languageCode': language or self.language})
        if data.get('id') != identifier:
            raise MapsProviderError('place_data')
        return data

    async def _destination_records(self, query: str, *, language: str = 'ko') -> dict:
        """Join geographic facts, excluding deleted IDs but preserving uncertainty.

        A definitive missing ID cannot be the destination. Other failed detail
        requests could conceal another valid entity, so their failures propagate
        instead of making an incomplete response appear unambiguous.
        """
        identifiers = await self._search_ids(query, language=self.language)
        # Reuse the existing country/city detail requests for Korean country and
        # English city identifiers; venue/navigation language is unchanged.
        tasks = [asyncio.create_task(self._details(identifier, destination=True, language=language)) for identifier in identifiers]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
        failures = [result for result in results if isinstance(result, MapsProviderError) and result.kind != 'not_found']
        if failures:
            raise min(failures, key=lambda failure: failure.transient)
        if any(isinstance(result, BaseException) and not isinstance(result, MapsProviderError) for result in results):
            raise MapsProviderError('invalid')
        return {'places': [result for result in results if isinstance(result, dict)]}

    async def resolve_destination(self, country: str, city: str) -> Destination:
        """Resolve one real geographic entity using ID search and Essentials facts.

        Args:
            country: Original country search text, cleaned for Unicode whitespace.
            city: Original city search text, cleaned for Unicode whitespace.
        Returns:
            Actual country code, destination ID and finite viewport bounds.
        Raises:
            ValueError: Blank input, missing facts or distinct ambiguous entities.
            MapsProviderError: Provider request, identity or authorization failure.

        Official names are unavailable in Essentials. Geographic type, country
        identity and boundary validation do not establish an administrative polygon.
        """
        country_query, city_query = _destination_query(country), _destination_query(city)
        if not country_query or not city_query:
            raise ValueError('Destination search text is empty')
        # Query whitespace cleanup cannot establish identical input intent.
        same_input = unicodedata.normalize('NFKC', country).casefold().strip() == unicodedata.normalize('NFKC', city).casefold().strip()
        # Country-as-city destinations can share one detail result as before;
        # their Korean administrative name already matches the lodging locale.
        city_language = 'ko' if same_input else 'en'
        tasks = [asyncio.create_task(self._destination_records(query, language=language))
                 for query, language in ((country_query, 'ko'), (f'{city_query}, {country_query}', city_language))]
        try:
            country_data, city_data = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
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
        countries: list[tuple[str, str, str]] = []
        for raw in country_data.get('places', []):
            identifier, code = _destination_id(raw), _destination_country_code(raw)
            if 'country' in _destination_types(raw) and identifier is not None and code is not None:
                countries.append((identifier, code, _destination_component_name(raw, {'country'})))
        if len(countries) != 1:
            raise ValueError('Country could not be unambiguously verified')
        country_id, expected, country_name = countries[0]
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
            city_name = _destination_component_name(raw, {'country'} if 'country' in types else types & geographic_types)
            matches.append(Destination(expected, *bounds, place_id=identifier, verified_country_name=country_name, verified_city_name=city_name))
        if len(matches) != 1:
            raise ValueError('City could not be unambiguously verified')
        return matches[0]

    async def search(self, candidate: Any, destination: Destination) -> VerifiedPlace:
        """Bind named proposals to scoped, corroborated IDs using Essentials only.

        Args:
            candidate: Model-proposed name, optional English alias and meal role.
            destination: Already verified country and city viewport.
        Returns:
            Provider ID/types/coordinates/address with the proposed display name.
        Raises:
            ValueError: Names resolve to different leading IDs or no suitable
                venue remains. These candidate failures permit existing refills.
            MapsProviderError: Global request, authorization, or outage failure.

        A distinct alias must resolve to the same leading ID before any paid
        details lookup. Unmatched aliases are not fallbacks for each other. For
        legacy proposals without an alias, bounded single-query validation stays
        available. Agreement reduces fuzzy matches but cannot establish an
        official display name, which Google does not supply in Essentials.
        """
        return await self._search_place(candidate, destination)

    async def search_lodging(self, candidate: Any, destination: Destination) -> VerifiedPlace:
        """Verify a named lodging using the same free IDs and Essentials facts.

        Args:
            candidate: Named lodging proposal with an optional English alias.
                Legacy candidates must have ``meal='none'`` when they specify a
                meal role. The caller establishes the name's provenance.
            destination: Verified country and city viewport.
        Returns:
            Verified lodging ID, type, address and coordinates with its supplied
            name. Reservation availability is not established by Maps.
        Raises:
            ValueError: The leading search hit is not a usable lodging in the
                destination. Lower-ranked hotels cannot replace its identity.
            MapsProviderError: Global request, authorization, or outage failure.
        """
        return await self._search_place(candidate, destination, lodging=True)

    async def search_airport(self, identity: AirportIdentity, destination: Destination) -> VerifiedPlace | None:
        """Bind a supplier airport name and IATA code to one Essentials record.

        Args:
            identity: Actual destination airport from the flight supplier, already
                verified as serving this city. A generated name is not accepted.
            destination: Verified destination country and city. Airports serving a
                city may be outside its viewport, so no city bias is applied here.
        Returns:
            Same-country airport facts when a name search agrees with the leading
            IATA result, otherwise None. One supplied English alias can recover a
            localized-name mismatch. Neither flight times nor terminal numbers
            are established. Official Google display names are not requested.
        Raises:
            MapsProviderError: Global request, authorization or provider outage.
            asyncio.CancelledError: Caller cancellation remains observable.
        """
        if not isinstance(identity, AirportIdentity) or identity.country_code != destination.country_code:
            if isinstance(identity, AirportIdentity):
                logger.info('Course airport verification rejected: iata=%s reason=country_mismatch', identity.code)
            return None
        return await self._search_airport_record(identity, identity.code, destination)

    async def search_airport_candidate(self, candidate: AirportCandidate, destination: Destination) -> VerifiedPlace | None:
        """Verify a proposed nearby airport without requiring a supplier quote.

        Args:
            candidate: Model-proposed name and IATA code; neither is supplier
                confirmation of connectivity or reservation availability.
            destination: Verified country and city viewport. Candidates may lie
                outside the city, within 200 km of its nearest viewport point.
        Returns:
            Same-country, nearby airport facts after the name and code searches
            establish one airport identity, or None for a conflicting or distant
            record. A supplied English alias may recover localized-name ranking.
        Raises:
            MapsProviderError: Global request, authorization or provider outage.
            asyncio.CancelledError: Caller cancellation remains observable.
        """
        if not isinstance(candidate, AirportCandidate):
            return None
        verified = await self._search_airport_record(candidate, candidate.iata_code, destination)
        if verified is None:
            return None
        if _distance_to_destination(verified.place.latitude, verified.place.longitude, destination) > MAX_PROPOSED_AIRPORT_DISTANCE_METERS:
            logger.info('Course airport verification rejected: iata=%s reason=outside_destination_proximity', candidate.iata_code)
            return None
        return verified

    async def _search_airport_record(self, proposal: AirportIdentity | AirportCandidate, code: str, destination: Destination) -> VerifiedPlace | None:
        """Share airport ID correlation and Essentials retrieval across provenances."""
        country = destination.verified_country_name or destination.country_code
        queries = (
            (f'{_destination_query(proposal.name)}, {_destination_query(country)}', self.language),
            (f'{code} airport, {destination.country_code}', 'en'),
        )
        # Lower shared IDs can describe a terminal or another real airport. They
        # never prove which name belongs to the IATA-leading record.
        tasks = [asyncio.create_task(self._search_ids(query, language=language))
                 for query, language in queries]
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
        failures = [result for result in results if isinstance(result, MapsProviderError)]
        if failures:
            raise min(failures, key=lambda failure: failure.transient)
        if any(isinstance(result, BaseException) for result in results):
            raise MapsProviderError('invalid') from None
        name_ids, code_ids = results
        if not code_ids:
            logger.info('Course airport verification rejected: iata=%s reason=empty_code_result', code)
            return None
        identifier = _airport_search_match(name_ids, code_ids)
        ambiguous = len(set(name_ids).intersection(code_ids)) > 1
        alias = proposal.english_name
        if identifier is None and alias and _normalized(alias) != _normalized(proposal.name):
            alias_ids = await self._search_ids(
                f'{_destination_query(alias)}, {_destination_query(country)}', language='en',
            )
            identifier = _airport_search_match(alias_ids, code_ids)
            ambiguous = ambiguous or len(set(alias_ids).intersection(code_ids)) > 1
            if identifier is not None:
                # The failed localized name is not evidence for this ID. Retain
                # only the matched name; the existing display node translates it.
                proposal = (proposal.model_copy(update={'name': alias})
                            if isinstance(proposal, AirportCandidate) else replace(proposal, name=alias))
                logger.info('Course airport verification recovered: iata=%s source=english_alias', code)
        if identifier is None:
            reason = 'ambiguous_ids' if ambiguous else 'empty_name_result' if not name_ids else 'name_code_mismatch'
            logger.info('Course airport verification rejected: iata=%s reason=%s', code, reason)
            return None
        try:
            raw = await self._details(identifier)
        except MapsProviderError as error:
            if error.kind in {'not_found', 'place_data'}:
                logger.info('Course airport verification rejected: iata=%s reason=%s', code, error.kind)
                return None
            raise
        verified = _verified_place(raw, destination, 'none', proposal, airport=True)
        if verified is None:
            logger.info('Course airport verification rejected: iata=%s reason=invalid_airport_facts', code)
        return verified

    async def _search_place(self, candidate: Any, destination: Destination, *, lodging: bool = False) -> VerifiedPlace:
        """Share scoped retrieval and field masks while keeping venue roles distinct."""
        names = [(candidate.name, self.language)]
        if candidate.english_name and _normalized(candidate.english_name) != _normalized(candidate.name):
            names.append((candidate.english_name, 'en'))
        context = [destination.verified_city_name,
                   destination.verified_country_name or destination.country_code]
        bias = {'rectangle': {'low': {'latitude': destination.south, 'longitude': destination.west},
                              'high': {'latitude': destination.north, 'longitude': destination.east}}}
        queries = [(', '.join(_destination_query(part) for part in [name, *context] if part.strip()), language)
                   for name, language in names]
        # Both names are independent retrieval evidence; do not pay for details
        # of a fuzzy hit until their first provider IDs agree.
        if len(queries) == 1:
            query, language = queries[0]
            results = [await self._search_ids(query, language=language, location_bias=bias)]
        else:
            tasks = [asyncio.create_task(self._search_ids(query, language=language, location_bias=bias))
                     for query, language in queries]
            try:
                results = await asyncio.gather(*tasks, return_exceptions=True)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, asyncio.CancelledError):
                    raise result
            failures = [result for result in results if isinstance(result, MapsProviderError)]
            if failures:
                raise min(failures, key=lambda failure: failure.transient)
            if any(isinstance(result, BaseException) for result in results):
                raise MapsProviderError('invalid') from None
        identifiers = results[0]
        if len(results) == 2:
            if not identifiers or not results[1] or identifiers[0] != results[1][0]:
                logger.info('Course venue identity rejected: reason=alias_disagreement')
                raise ValueError('Proposed venue names do not resolve to the same leading place ID')
            identifiers = identifiers[:1]
        if lodging:
            # A failed supplier property must not inherit another hotel's ID.
            identifiers = identifiers[:1]
        for identifier in identifiers:
            try:
                # One output language per ID maximizes request-local sharing.
                raw = await self._details(identifier)
            except MapsProviderError as error:
                if error.kind not in {'not_found', 'place_data'}:
                    raise
                continue
            meal = getattr(candidate, 'meal', 'none') if lodging else candidate.meal
            verified = _verified_place(raw, destination, meal, candidate, lodging=lodging)
            if verified is not None:
                return verified
        logger.info('Course venue identity rejected: reason=unusable_essentials')
        raise ValueError(f'No verified venue facts for query: {candidate.name}')

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
        body = {'origin': {'placeId': origin.place.placeId.removeprefix('places/')}, 'destination': {'placeId': destination.place.placeId.removeprefix('places/')}, 'travelMode': modes[mode], 'languageCode': self.language}
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
            memo = format_route_guidance(data, mode, language=self.language)
            if memo is not None and (not isinstance(memo, str) or not memo.strip() or len(memo) > MAX_MEMO_LENGTH):
                raise ValueError('Unusable optional route guidance')
        except Exception:  # noqa: BLE001 - optional presentation must not invalidate verified metrics
            memo = None
        try:
            source = navigation_source(data)
        except Exception:  # noqa: BLE001 - optional sources must not invalidate verified metrics
            source = []
        return InternalTransportSchema(type=mode, distance=distance, minutes=math.ceil(seconds / 60), cost=0 if mode == 'walking' else None, memo=memo, navigationSource=source)

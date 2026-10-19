"""Verify supplier airports outside city bounds without relaxing tourist checks."""

import asyncio
import copy
import json

import httpx
import pytest

from app.agent.course_state import Candidate
from app.agent.tools.verified_maps import (
    Destination,
    MapsProviderError,
    VerifiedMapsProvider,
)
from app.schemas.course import PlaceSchema, StopSchema
from app.services.myrealtrip_quotes import AirportIdentity


@pytest.fixture
def airport_payload():
    """Return only Essentials facts for an airport outside the city viewport."""
    return {
        'id': 'verified-airport',
        'formattedAddress': 'Japan, Chiba, Narita',
        'addressComponents': [{'longText': 'Japan', 'shortText': 'JP', 'types': ['country']}],
        'location': {'latitude': 35.77, 'longitude': 140.39},
        'types': ['international_airport', 'airport', 'establishment', 'point_of_interest'],
    }


@pytest.fixture
def airport_identity():
    return AirportIdentity('NRT', '나리타 국제공항', 'JP', 'Narita International Airport')


@pytest.fixture
def airport_destination():
    return Destination('JP', 35.5, 139.5, 35.9, 139.9,
                       verified_country_name='일본', verified_city_name='Tokyo')


def airport_http_handler(payload, calls):
    """Capture serialized wire requests, including field masks and search bounds."""
    def respond(request):
        calls.append(request)
        if request.method == 'POST':
            assert request.url.path == '/v1/places:searchText'
            assert request.headers['X-Goog-FieldMask'] == 'places.id'
            return httpx.Response(200, json={'places': [{'id': payload['id']}]})
        assert request.url.path == '/v1/places/verified-airport'
        assert request.headers['X-Goog-FieldMask'] == 'id,formattedAddress,addressComponents,location,types'
        return httpx.Response(200, json=payload)
    return respond


@pytest.mark.asyncio
async def test_airport_outside_city_uses_correlated_free_ids_one_essentials_and_cache(
    airport_payload, airport_identity, airport_destination,
):
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(airport_http_handler(airport_payload, calls))) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        airport = await provider.search_airport(airport_identity, airport_destination)
        repeated = await provider.search_airport(airport_identity, airport_destination)
        metrics = provider.metrics.snapshot()
    assert airport is not None and repeated == airport
    assert not airport_destination.contains(airport.place.latitude, airport.place.longitude)
    searches = [json.loads(call.content) for call in calls if call.method == 'POST']
    assert {(body['textQuery'], body['languageCode']) for body in searches} == {
        ('나리타 국제공항, 일본', 'ko'), ('NRT airport, JP', 'en'),
    }
    assert all('locationBias' not in body and 'locationRestriction' not in body for body in searches)
    assert len(calls) == 3
    assert metrics['skus']['text_search_ids']['requests'] == 2
    assert metrics['skus']['place_details_essentials']['requests'] == 1
    assert set(metrics['skus']) == {'text_search_ids', 'place_details_essentials'}
    stop = StopSchema(sequence=1, stayMinutes=60, memo='입국 안내', reason='여행의 출발점',
                      cost=0, place=airport.place, transportToNext={'type': 'none'})
    public_place = stop.model_dump(context={'public_response': True})['place']
    assert public_place['category'] == 'AIRPORT'
    assert public_place['placeName'] == airport_identity.name
    assert public_place['placeId'] == 'verified-airport'
    assert PlaceSchema.model_validate(public_place).category == 'AIRPORT'


@pytest.mark.asyncio
async def test_airport_supplier_country_mismatch_avoids_maps_calls(airport_payload, airport_destination):
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(airport_http_handler(airport_payload, calls))) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        assert await provider.search_airport(AirportIdentity('ICN', '인천국제공항', 'KR'), airport_destination) is None
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['different_leading_ids', 'empty_code_result', 'different_order'])
async def test_airport_name_code_disagreement_cannot_pay_for_or_select_an_alternative(
    airport_identity, airport_destination, failure,
):
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == 'POST'
        body = json.loads(request.content)
        if not body['textQuery'].startswith('NRT airport,'):
            ids = ['verified-airport', 'another-airport']
        elif failure == 'empty_code_result':
            ids = []
        elif failure == 'different_order':
            ids = ['another-airport', 'verified-airport']
        else:
            ids = ['wrong-airport']
        return httpx.Response(200, json={'places': [{'id': value} for value in ids]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        assert await provider.search_airport(airport_identity, airport_destination) is None
    assert len(calls) == (2 if failure == 'empty_code_result' else 3)


@pytest.mark.asyncio
async def test_terminal_ranked_before_airport_requires_english_name_agreement(
    airport_payload, airport_identity, airport_destination,
):
    """The supplied English name can identify an airport ranked after a terminal."""
    calls = []

    def respond(request):
        calls.append(request)
        if request.method == 'POST':
            body = json.loads(request.content)
            ids = ['verified-airport'] if body['languageCode'] == 'en' else ['airport-terminal', 'verified-airport']
            assert request.headers['X-Goog-FieldMask'] == 'places.id'
            return httpx.Response(200, json={'places': [{'id': identifier} for identifier in ids]})
        assert request.url.path == '/v1/places/verified-airport'
        assert request.headers['X-Goog-FieldMask'] == 'id,formattedAddress,addressComponents,location,types'
        return httpx.Response(200, json=airport_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        airport = await provider.search_airport(airport_identity, airport_destination)
    assert airport is not None and airport.place.placeId == 'verified-airport'
    assert airport.place.placeName == airport_identity.english_name
    assert len(calls) == 4  # Three free searches; still only one Essentials request.


@pytest.mark.asyncio
async def test_supplier_english_alias_rescue_uses_matched_name_until_localization(
    airport_payload, airport_identity, airport_destination, caplog,
):
    """Do not attach an uncorrelated supplier display name to the rescued record."""
    calls = []

    def respond(request):
        calls.append(request)
        if request.method == 'POST':
            body = json.loads(request.content)
            ids = ['unrelated-terminal'] if body['languageCode'] == 'ko' else ['verified-airport']
            assert request.headers['X-Goog-FieldMask'] == 'places.id'
            return httpx.Response(200, json={'places': [{'id': identifier} for identifier in ids]})
        assert request.url.path == '/v1/places/verified-airport'
        assert request.headers['X-Goog-FieldMask'] == 'id,formattedAddress,addressComponents,location,types'
        return httpx.Response(200, json=airport_payload)

    with caplog.at_level('INFO', logger='app.agent.tools.verified_maps'):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = VerifiedMapsProvider(client=client, api_key='offline')
            airport = await provider.search_airport(airport_identity, airport_destination)
            assert await provider.search_airport(airport_identity, airport_destination) == airport
    assert airport is not None
    assert airport.place.placeName == airport_identity.english_name
    assert airport.place.placeEngName == airport_identity.english_name
    assert len(calls) == 4  # One extra free ID search; no extra paid request or repeated HTTP.
    assert 'iata=NRT source=english_alias' in caplog.text
    assert airport_identity.name not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [
    'wrong_country', 'missing_address', 'invalid_coordinates', 'nonfinite_coordinates',
    'wrong_id', 'non_airport', 'geographic_record', 'missing_type', 'deleted',
])
async def test_airport_requires_own_type_same_country_and_complete_essentials(
    airport_payload, airport_identity, airport_destination, failure,
):
    payload = copy.deepcopy(airport_payload)
    if failure == 'wrong_country':
        payload['addressComponents'][0]['shortText'] = 'KR'
    elif failure == 'missing_address':
        payload['formattedAddress'] = ''
    elif failure == 'invalid_coordinates':
        payload['location']['longitude'] = True
    elif failure == 'nonfinite_coordinates':
        payload['location']['latitude'] = float('nan')
    elif failure == 'wrong_id':
        payload['id'] = 'unrelated-airport'
    elif failure == 'non_airport':
        payload['types'] = ['restaurant', 'establishment']
    elif failure == 'geographic_record':
        payload['types'] = ['airport', 'locality']
    elif failure == 'missing_type':
        payload['types'] = ['establishment', 'point_of_interest']
    calls = []

    def respond(request):
        calls.append(request)
        if request.method == 'POST':
            return httpx.Response(200, json={'places': [{'id': 'verified-airport'}, {'id': 'fallback-airport'}]})
        assert request.url.path == '/v1/places/verified-airport'
        return httpx.Response(404 if failure == 'deleted' else 200,
                              content=json.dumps(payload), headers={'Content-Type': 'application/json'})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        assert await provider.search_airport(airport_identity, airport_destination) is None
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_airports_do_not_become_tourist_or_lodging_candidates(airport_payload, airport_identity):
    calls = []
    inside_airport = Destination('JP', 35.5, 139.5, 36, 141)
    async with httpx.AsyncClient(transport=httpx.MockTransport(airport_http_handler(airport_payload, calls))) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        candidate = Candidate(name=airport_identity.name)
        for search in (provider.search, provider.search_lodging):
            with pytest.raises(ValueError, match='No verified venue facts'):
                await search(candidate, inside_airport)
    assert len(calls) == 2  # The two roles reuse the request cache, not airport admission.


@pytest.mark.asyncio
async def test_airport_authorization_failure_remains_observable(airport_identity, airport_destination):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        with pytest.raises(MapsProviderError, match='unauthorized'):
            await provider.search_airport(airport_identity, airport_destination)


@pytest.mark.asyncio
async def test_airport_cancellation_joins_parallel_searches(airport_identity, airport_destination):
    started = asyncio.Event()
    active = 0

    async def respond(request):
        nonlocal active
        active += 1
        if active == 2:
            started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        task = asyncio.create_task(provider.search_airport(airport_identity, airport_destination))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert active == 0

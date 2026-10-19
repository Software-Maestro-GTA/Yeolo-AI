"""Check nearby proposed airports independently of supplier booking identities."""

import asyncio
import copy
import json

import httpx
import pytest

from app.agent.tools.verified_maps import (
    Destination,
    MapsProviderError,
    VerifiedMapsProvider,
)
from app.schemas.course_anchors import AirportCandidate
from app.services.myrealtrip_quotes import AirportIdentity


@pytest.fixture
def candidate():
    return AirportCandidate(name='나리타 국제공항', english_name='Narita International Airport', iata_code='NRT')


@pytest.fixture
def destination():
    return Destination('JP', 35.5, 139.5, 35.9, 139.9,
                       verified_country_name='일본', verified_city_name='Tokyo')


@pytest.fixture
def airport():
    return {
        'id': 'narita-airport',
        'formattedAddress': 'Japan, Chiba, Narita',
        'addressComponents': [{'longText': 'Japan', 'shortText': 'JP', 'types': ['country']}],
        'location': {'latitude': 35.77, 'longitude': 140.39},
        'types': ['international_airport', 'airport', 'establishment', 'point_of_interest'],
    }


def handler(payload, calls):
    """Capture actual HTTP masks while returning only Essentials facts."""
    def respond(request):
        calls.append(request)
        if request.method == 'POST':
            assert request.url.path == '/v1/places:searchText'
            assert request.headers['X-Goog-FieldMask'] == 'places.id'
            return httpx.Response(200, json={'places': [{'id': 'narita-airport'}]})
        assert request.url.path == '/v1/places/narita-airport'
        assert request.headers['X-Goog-FieldMask'] == 'id,formattedAddress,addressComponents,location,types'
        # json.dumps preserves invalid NaN data used to test rejection.
        return httpx.Response(200, content=json.dumps(payload), headers={'Content-Type': 'application/json'})
    return respond


@pytest.mark.asyncio
async def test_candidate_outside_city_shares_requests_with_supplier_without_requiring_it(candidate, destination, airport):
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler(airport, calls))) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        proposed = await provider.search_airport_candidate(candidate, destination)
        repeated = await provider.search_airport_candidate(candidate, destination)
        supplier = await provider.search_airport(AirportIdentity('NRT', candidate.name, 'JP', candidate.english_name), destination)
        metrics = provider.metrics.snapshot()
    assert proposed is not None and proposed == repeated == supplier
    assert proposed.place.placeId == 'narita-airport'
    assert proposed.place.placeName == candidate.name
    assert not destination.contains(proposed.place.latitude, proposed.place.longitude)
    assert len(calls) == 3
    bodies = [json.loads(request.content) for request in calls if request.method == 'POST']
    assert {body['textQuery'] for body in bodies} == {'나리타 국제공항, 일본', 'NRT airport, JP'}
    assert all('locationBias' not in body and 'locationRestriction' not in body for body in bodies)
    assert metrics['skus']['text_search_ids']['requests'] == 2
    assert metrics['skus']['place_details_essentials']['requests'] == 1
    assert set(metrics['skus']) == {'text_search_ids', 'place_details_essentials'}


@pytest.mark.asyncio
async def test_same_country_distant_airport_rejected_only_for_generated_candidate(candidate, destination, airport):
    airport['location'] = {'latitude': 34.43, 'longitude': 135.24}
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler(airport, calls))) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        assert await provider.search_airport_candidate(candidate, destination) is None
        # Supplier city matching is performed by its own boundary, so an existing
        # confirmed airport is not newly subjected to this candidate heuristic.
        assert await provider.search_airport(AirportIdentity('NRT', candidate.name, 'JP'), destination) is not None
    assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(('longitude', 'accepted'), [(179.5, True), (-179.5, True), (-177.5, True), (175, False)])
async def test_candidate_distance_handles_viewport_crossing_date_line(candidate, airport, longitude, accepted):
    destination = Destination('JP', -18, 178, -16, -178)
    airport['location'] = {'latitude': -17, 'longitude': longitude}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler(airport, []))) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        assert (await provider.search_airport_candidate(candidate, destination) is not None) is accepted


@pytest.mark.asyncio
@pytest.mark.parametrize(('latitude', 'longitude', 'accepted'), [(37.6, 139.7, True), (37.8, 139.7, False), (35.7, 141.9, True), (35.7, 142.2, False)])
async def test_airport_candidate_distance_is_from_viewport_edge(candidate, destination, airport, latitude, longitude, accepted):
    airport['location'] = {'latitude': latitude, 'longitude': longitude}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler(airport, []))) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        assert (await provider.search_airport_candidate(candidate, destination) is not None) is accepted


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['wrong_country', 'non_airport', 'geographic_record', 'missing_address', 'nonfinite_coordinates', 'wrong_id'])
async def test_candidate_requires_actual_airport_facts(candidate, destination, airport, failure):
    payload = copy.deepcopy(airport)
    if failure == 'wrong_country':
        payload['addressComponents'][0]['shortText'] = 'KR'
    elif failure == 'non_airport':
        payload['types'] = ['restaurant', 'establishment']
    elif failure == 'geographic_record':
        payload['types'] = ['airport', 'locality']
    elif failure == 'missing_address':
        payload['formattedAddress'] = ''
    elif failure == 'nonfinite_coordinates':
        payload['location']['latitude'] = float('nan')
    elif failure == 'wrong_id':
        payload['id'] = 'different-airport'
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler(payload, calls))) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        assert await provider.search_airport_candidate(candidate, destination) is None
    assert len(calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('iata_ids', [[], ['another-airport'], ['another-airport', 'narita-airport']])
async def test_conflicting_or_missing_iata_search_does_not_request_details(candidate, destination, iata_ids):
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == 'POST'
        identifiers = iata_ids if json.loads(request.content)['textQuery'].startswith('NRT airport,') else ['narita-airport']
        return httpx.Response(200, json={'places': [{'id': identifier} for identifier in identifiers]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        assert await provider.search_airport_candidate(candidate, destination) is None
    assert len(calls) == (3 if iata_ids else 2)


@pytest.mark.asyncio
async def test_generated_english_alias_recovers_empty_name_without_retaining_unverified_display(candidate, destination, airport):
    """The alias is checked against the code and becomes the sole display source."""
    calls = []

    def respond(request):
        calls.append(request)
        if request.method == 'POST':
            body = json.loads(request.content)
            ids = [] if body['languageCode'] == 'ko' else ['narita-airport']
            assert request.headers['X-Goog-FieldMask'] == 'places.id'
            return httpx.Response(200, json={'places': [{'id': identifier} for identifier in ids]})
        assert request.url.path == '/v1/places/narita-airport'
        assert request.headers['X-Goog-FieldMask'] == 'id,formattedAddress,addressComponents,location,types'
        return httpx.Response(200, json=airport)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        verified = await provider.search_airport_candidate(candidate, destination)
        metrics = provider.metrics.snapshot()
    assert verified is not None
    assert verified.place.placeName == candidate.english_name
    assert verified.place.placeEngName == candidate.english_name
    assert candidate.name == '나리타 국제공항'  # The original proposal is not mutated.
    assert len(calls) == 4
    assert metrics['skus']['text_search_ids']['requests'] == 3
    assert metrics['skus']['place_details_essentials']['requests'] == 1
    assert set(metrics['skus']) == {'text_search_ids', 'place_details_essentials'}


@pytest.mark.asyncio
async def test_ambiguous_common_airports_are_rejected_without_paying_for_details(candidate, destination, caplog):
    """Swapped airport rankings cannot justify picking one from ambiguous matches."""
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == 'POST'
        body = json.loads(request.content)
        ids = ['another-airport', 'narita-airport'] if body['textQuery'].startswith('NRT airport,') else ['narita-airport', 'another-airport']
        return httpx.Response(200, json={'places': [{'id': identifier} for identifier in ids]})

    with caplog.at_level('INFO', logger='app.agent.tools.verified_maps'):
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = VerifiedMapsProvider(client=client, api_key='offline')
            assert await provider.search_airport_candidate(candidate, destination) is None
    assert len(calls) == 3
    assert 'iata=NRT reason=ambiguous_ids' in caplog.text
    assert candidate.name not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('english_name', ['', 'Haneda Airport'])
async def test_other_airport_name_cannot_bind_a_shared_lower_ranked_iata_hit(destination, english_name):
    """Haneda's broader search results cannot label Narita as Haneda."""
    candidate = AirportCandidate(name='하네다 공항', english_name=english_name, iata_code='NRT')
    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == 'POST'  # The mismatch is rejected before paid details.
        query = json.loads(request.content)['textQuery']
        ids = ['narita-airport'] if query.startswith('NRT airport,') else ['haneda-airport', 'narita-airport']
        return httpx.Response(200, json={'places': [{'id': identifier} for identifier in ids]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        assert await provider.search_airport_candidate(candidate, destination) is None
    assert len(calls) == (3 if english_name else 2)


@pytest.mark.asyncio
async def test_alias_search_cancellation_does_not_continue_to_details(candidate, destination):
    """Cancellation while resolving a supplied alias closes the only active request."""
    started = asyncio.Event()
    active = 0
    calls = []

    async def respond(request):
        nonlocal active
        calls.append(request)
        assert request.method == 'POST'
        query = json.loads(request.content)['textQuery']
        if query.startswith(candidate.english_name):
            active += 1
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1
        identifiers = ['narita-airport'] if query.startswith('NRT airport,') else ['unrelated-terminal']
        return httpx.Response(200, json={'places': [{'id': identifier} for identifier in identifiers]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        task = asyncio.create_task(provider.search_airport_candidate(candidate, destination))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert active == 0 and len(calls) == 3


@pytest.mark.asyncio
async def test_candidate_search_does_not_hide_authentication_failure(candidate, destination):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        with pytest.raises(MapsProviderError, match='unauthorized'):
            await provider.search_airport_candidate(candidate, destination)


@pytest.mark.asyncio
async def test_candidate_cancellation_joins_both_searches(candidate, destination):
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
        task = asyncio.create_task(provider.search_airport_candidate(candidate, destination))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert active == 0

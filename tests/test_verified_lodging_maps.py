"""Keep nightly lodging verification within the existing Essentials boundary."""

import copy
import json

import httpx
import pytest

from app.agent.course_state import Candidate
from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider
from app.schemas.course import PlaceSchema, StopSchema
from app.schemas.course_anchors import NamedLodgingCandidate


@pytest.fixture
def lodging_payload():
    """Supply a named hotel's minimal provider facts without paid name fields."""
    return {
        'id': 'verified-hotel',
        'formattedAddress': '대한민국 서울 중구',
        'addressComponents': [{'longText': '대한민국', 'shortText': 'KR', 'types': ['country']}],
        'location': {'latitude': 37.56, 'longitude': 126.98},
        'types': ['hotel', 'lodging', 'establishment', 'point_of_interest'],
    }


def lodging_http_handler(payload, requests):
    """Capture real serialized requests and expose only the selected fields."""
    def respond(request):
        requests.append(request)
        if request.method == 'POST':
            assert request.url.path == '/v1/places:searchText'
            assert request.headers['X-Goog-FieldMask'] == 'places.id'
            return httpx.Response(200, json={'places': [{'id': 'verified-hotel'}, {'id': 'other-hotel'}]})
        assert request.url.path == '/v1/places/verified-hotel'
        assert request.headers['X-Goog-FieldMask'] == 'id,formattedAddress,addressComponents,location,types'
        return httpx.Response(200, json=payload)
    return respond


@pytest.mark.asyncio
async def test_named_lodging_uses_free_id_and_one_essentials_detail(lodging_payload):
    calls = []
    candidate = Candidate(name='공급자 서울 호텔', english_name='', meal='none')
    destination = Destination('KR', 37.3, 126.7, 37.8, 127.3,
                              verified_country_name='대한민국', verified_city_name='서울')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lodging_http_handler(lodging_payload, calls))) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        hotel = await provider.search_lodging(candidate, destination)
        metrics = provider.metrics.snapshot()
    assert len(calls) == 2
    assert json.loads(calls[0].content)['textQuery'] == '공급자 서울 호텔, 서울, 대한민국'
    assert hotel.place.placeId == 'verified-hotel'
    assert hotel.place.placeName == candidate.name
    assert hotel.place.category == 'hotel'
    assert hotel.place.latitude == 37.56 and hotel.place.longitude == 126.98
    assert hotel.place.address == '대한민국 서울 중구'
    assert set(metrics['skus']) == {'text_search_ids', 'place_details_essentials'}
    assert metrics['skus']['place_details_essentials']['requests'] == 1
    stop = StopSchema(sequence=6, stayMinutes=30, memo='숙박 안내', reason='숙소 추천',
                      cost=100_000, place=hotel.place, transportToNext={'type': 'none'})
    wire = stop.model_dump(context={'public_response': True})
    assert wire['place']['category'] == 'LODGING'
    assert PlaceSchema.model_validate(wire['place']).category == 'LODGING'
    assert wire['cost'] == 100_000


@pytest.mark.asyncio
async def test_generated_lodging_without_meal_role_verifies_correlated_names(lodging_payload):
    """A real generated anchor schema needs neither a booking nor visit fields."""
    candidate = NamedLodgingCandidate(name='서울 호텔', english_name='Seoul Hotel')
    destination = Destination('KR', 37.3, 126.7, 37.8, 127.3,
                              verified_country_name='대한민국', verified_city_name='서울')
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lodging_http_handler(lodging_payload, calls))) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        hotel = await provider.search_lodging(candidate, destination)
        repeated = await provider.search_lodging(candidate, destination)
    assert hotel == repeated
    assert hotel.place.placeId == 'verified-hotel'
    assert hotel.place.placeName == candidate.name
    assert hotel.place.category == 'hotel'
    assert len(calls) == 3
    assert {json.loads(call.content)['textQuery'] for call in calls if call.method == 'POST'} == {
        '서울 호텔, 서울, 대한민국', 'Seoul Hotel, 서울, 대한민국',
    }


@pytest.mark.asyncio
@pytest.mark.parametrize('types', [['hotel'], ['lodging', 'establishment']])
async def test_lodging_only_type_is_not_promoted_to_a_tourism_stop(lodging_payload, types):
    payload = {**lodging_payload, 'types': types}
    calls = []
    # A single hit isolates role validation from legacy multi-hit tourism retries.
    def respond(request):
        if request.method == 'POST':
            calls.append(request)
            return httpx.Response(200, json={'places': [{'id': payload['id']}]})
        return lodging_http_handler(payload, calls)(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        with pytest.raises(ValueError, match='No verified venue facts'):
            await provider.search(Candidate(name='공급자 서울 호텔'), Destination('KR', 37.3, 126.7, 37.8, 127.3))
        assert (await provider.search_lodging(Candidate(name='공급자 서울 호텔'), Destination('KR', 37.3, 126.7, 37.8, 127.3))).place.category in types
    assert len(calls) == 2  # Same request-scoped responses are reused across roles.


@pytest.mark.asyncio
@pytest.mark.parametrize(('types', 'meal', 'category'), [
    (['lodging', 'restaurant'], 'dinner', 'restaurant'),
    (['restaurant', 'resort_hotel'], 'lunch', 'restaurant'),
    (['tourist_attraction', 'lodging'], 'none', 'tourist_attraction'),
])
async def test_existing_mixed_venue_role_remains_separate_from_lodging(lodging_payload, types, meal, category):
    """Adding hotels must preserve independently supported existing visit roles."""
    payload = {**lodging_payload, 'types': types}
    calls = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lodging_http_handler(payload, calls))) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        destination = Destination('KR', 37.3, 126.7, 37.8, 127.3)
        visit = await provider.search(Candidate(name='숙박시설 내 방문 장소', meal=meal), destination)
        hotel = await provider.search_lodging(Candidate(name='숙박시설 내 방문 장소'), destination)
    assert visit.place.category == category
    assert hotel.place.category in {'lodging', 'resort_hotel'}
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [
    'wrong_country', 'outside_city', 'missing_address', 'invalid_coordinates',
    'wrong_id', 'tourism_type', 'missing_type', 'meal_role', 'deleted',
])
async def test_invalid_leading_lodging_does_not_bind_a_different_hotel(lodging_payload, failure):
    payload = copy.deepcopy(lodging_payload)
    if failure == 'wrong_country':
        payload['addressComponents'][0]['shortText'] = 'JP'
    elif failure == 'outside_city':
        payload['location']['latitude'] = 35.1
    elif failure == 'missing_address':
        payload['formattedAddress'] = ''
    elif failure == 'invalid_coordinates':
        payload['location']['longitude'] = True
    elif failure == 'wrong_id':
        payload['id'] = 'different-id'
    elif failure == 'tourism_type':
        payload['types'] = ['restaurant', 'tourist_attraction']
    elif failure == 'missing_type':
        payload['types'] = ['establishment', 'point_of_interest']
    calls = []
    handler = lodging_http_handler(payload, calls)

    def respond(request):
        if failure == 'deleted' and request.method == 'GET':
            calls.append(request)
            return httpx.Response(404, json={'error': {}})
        return handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        with pytest.raises(ValueError, match='No verified venue facts'):
            await provider.search_lodging(Candidate(name='공급자 서울 호텔', meal='lunch' if failure == 'meal_role' else 'none'),
                                          Destination('KR', 37.3, 126.7, 37.8, 127.3))
    assert [call.url.path for call in calls] == ['/v1/places:searchText', '/v1/places/verified-hotel']

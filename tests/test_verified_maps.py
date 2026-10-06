"""Reject malformed external facts instead of turning them into plausible places/routes."""

import copy
from datetime import UTC

import httpx
import pytest

from app.schemas.course import PlaceSchema


@pytest.fixture
def place_payload():
    return {
        'id': 'verified-museum',
        'displayName': {'text': '서울 미술관'},
        'formattedAddress': '대한민국 서울 종로구',
        'location': {'latitude': 37.57, 'longitude': 126.98},
        'businessStatus': 'OPERATIONAL',
        'addressComponents': [
            {'longText': '대한민국', 'shortText': 'KR', 'types': ['country']},
            {'longText': '서울', 'types': ['administrative_area_level_1']},
        ],
        'types': ['museum'],
        'regularOpeningHours': {
            'periods': [{'open': {'day': 1, 'hour': 9, 'minute': 0}, 'close': {'day': 1, 'hour': 18, 'minute': 0}}],
            'weekdayDescriptions': ['월요일 09:00-18:00'],
        },
    }


@pytest.mark.asyncio
async def test_provider_uses_place_facts_from_http_response(place_payload):
    from app.agent.course_graph import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    def respond(request):
        if request.method == 'GET':
            return httpx.Response(200, json=place_payload)
        return httpx.Response(200, json={'places': [{'id': place_payload['id']}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        place = await provider.search(
            Candidate(name='서울 미술관', english_name='Seoul Museum'),
            Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3),
        )
    assert place.place.placeId.endswith('verified-museum')
    assert place.place.latitude == 37.57
    assert place.place.longitude == 126.98
    assert place.place.address == place_payload['formattedAddress']
    assert place.periods == place_payload['regularOpeningHours']['periods']


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['missing_id', 'out_of_bounds', 'wrong_country', 'closed', 'nan', 'missing_address'])
async def test_invalid_place_facts_are_rejected(place_payload, mutation):
    from app.agent.course_graph import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    payload = copy.deepcopy(place_payload)
    if mutation == 'missing_id':
        payload.pop('id')
    elif mutation == 'out_of_bounds':
        payload['location'] = {'latitude': 48.8, 'longitude': 2.3}
    elif mutation == 'wrong_country':
        payload['addressComponents'][0]['shortText'] = 'JP'
    elif mutation == 'closed':
        payload['businessStatus'] = 'CLOSED_PERMANENTLY'
    elif mutation == 'nan':
        payload['location']['latitude'] = 'NaN'
    elif mutation == 'missing_address':
        payload.pop('formattedAddress')
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload if request.method == 'GET' else {'places': [{'id': payload.get('id')}]}))) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        with pytest.raises(ValueError):
            await provider.search(Candidate(name='서울 미술관'), Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3))


@pytest.mark.asyncio
@pytest.mark.parametrize('route', [{}, {'duration': '600s'}, {'distanceMeters': 200}, {'duration': '-1s', 'distanceMeters': 200}, {'duration': '600s', 'distanceMeters': -1}, {'duration': 'invalid', 'distanceMeters': 200}])
async def test_route_requires_real_nonnegative_duration_and_distance(route):
    from app.agent.tools.verified_maps import VerifiedMapsProvider, VerifiedPlace

    first = VerifiedPlace(PlaceSchema(placeId='places/a', placeName='A', category='museum', latitude=37.55, longitude=126.98))
    second = VerifiedPlace(PlaceSchema(placeId='places/b', placeName='B', category='restaurant', latitude=37.56, longitude=126.99))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'routes': [route]}))) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        with pytest.raises(ValueError):
            await provider.route(first, second)


@pytest.mark.asyncio
async def test_route_rounds_up_duration_and_does_not_invent_bus_instructions():
    from app.agent.tools.verified_maps import VerifiedMapsProvider, VerifiedPlace

    first = VerifiedPlace(PlaceSchema(placeId='places/a', placeName='A', category='museum', latitude=37.55, longitude=126.98))
    second = VerifiedPlace(PlaceSchema(placeId='places/b', placeName='B', category='restaurant', latitude=37.56, longitude=126.99))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'routes': [{'duration': '601s', 'distanceMeters': 750}]}))) as client:
        result = await VerifiedMapsProvider(client=client, api_key='offline').route(first, second)
    assert result.minutes == 11
    assert result.distance == 750
    assert result.type == 'walking'


@pytest.mark.asyncio
async def test_provider_bounds_concurrent_searches_and_deduplicates(place_payload):
    import asyncio

    from app.agent.course_graph import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    in_flight = peak = calls = 0

    async def respond(request):
        nonlocal in_flight, peak, calls
        in_flight += 1
        peak = max(peak, in_flight)
        calls += 1
        await asyncio.sleep(.01)
        in_flight -= 1
        if request.method == 'GET':
            return httpx.Response(200, json=place_payload)
        return httpx.Response(200, json={'places': [{'id': place_payload['id']}]})

    destination = Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline', concurrency=2)
        await asyncio.gather(*[
            provider.search(Candidate(name='서울 미술관', english_name=f'Museum {index}'), destination)
            for index in range(6)
        ])
        assert peak <= 2
        calls_before = calls
        await asyncio.gather(*[provider.search(Candidate(name='서울 미술관', english_name='Museum 0'), destination) for _ in range(3)])
        assert calls == calls_before


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['valid', 'wrong_country', 'official_name_differs', 'missing_viewport', 'ambiguous_city'])
async def test_destination_requires_unambiguous_city_and_country(mutation):
    import json

    from app.agent.tools.verified_maps import VerifiedMapsProvider

    country_component = {'longText': '대한민국', 'shortText': 'KR', 'types': ['country']}
    city = {
        'id': 'seoul-city', 'displayName': {'text': '서울'}, 'types': ['locality'],
        'addressComponents': [copy.deepcopy(country_component), {'longText': '서울', 'types': ['locality']}],
        'viewport': {'low': {'latitude': 37.3, 'longitude': 126.7}, 'high': {'latitude': 37.8, 'longitude': 127.3}},
    }
    if mutation == 'wrong_country':
        city['addressComponents'][0]['shortText'] = 'JP'
    elif mutation == 'official_name_differs':
        city['displayName']['text'] = '首爾'
        city['addressComponents'][1]['longText'] = '首爾'
    elif mutation == 'missing_viewport':
        city.pop('viewport')

    def respond(request):
        query = json.loads(request.content)['textQuery']
        assert request.url.host == 'places.googleapis.com'
        if query == '대한민국':
            return httpx.Response(200, json={'places': [{'id': 'verified-country', 'types': ['country'], 'addressComponents': [country_component]}]})
        return httpx.Response(200, json={'places': [city, copy.deepcopy(city)] if mutation == 'ambiguous_city' else [city]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        if mutation in {'valid', 'official_name_differs'}:
            destination = await provider.resolve_destination('대한민국', '서울')
            assert destination.country_code == 'KR'
            assert destination.contains(37.55, 126.98)
            assert not destination.contains(48.8, 2.3)
        else:
            with pytest.raises(ValueError):
                await provider.resolve_destination('대한민국', '서울')


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['related_shop', 'ambiguous_ids', 'parenthetical_alias', 'english_fallback', 'nonfood_meal'])
async def test_named_place_identity_and_official_aliases(place_payload, case):
    import json

    from app.agent.course_graph import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    candidate = Candidate(name='Louvre Museum', english_name='Louvre Museum')
    payload = copy.deepcopy(place_payload)
    payload['displayName']['text'] = 'Louvre Museum'
    payload['id'] = 'official-louvre-id'
    places = [payload]
    if case == 'related_shop':
        payload['displayName']['text'] = 'Louvre Museum Shop'
        payload['types'] = ['store']
    elif case == 'ambiguous_ids':
        second = copy.deepcopy(payload)
        second['id'] = 'another-louvre-id'
        places.append(second)
    elif case == 'parenthetical_alias':
        payload['displayName']['text'] = 'Louvre Museum (Musée du Louvre)'
    elif case == 'english_fallback':
        candidate.name = '루브르 박물관'
    elif case == 'nonfood_meal':
        candidate.meal = 'lunch'
    queries = []

    def respond(request):
        if request.method == 'GET':
            return httpx.Response(200, json=payload)
        query = json.loads(request.content)['textQuery']
        queries.append(query)
        if case == 'english_fallback' and query == '루브르 박물관':
            return httpx.Response(200, json={'places': []})
        if request.headers['X-Goog-FieldMask'] == 'places.id':
            return httpx.Response(200, json={'places': [{'id': item['id']} for item in places]})
        return httpx.Response(200, json={'places': places})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        destination = Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3)
        if case in {'related_shop', 'ambiguous_ids', 'nonfood_meal'}:
            with pytest.raises(ValueError):
                await provider.search(candidate, destination)
        else:
            verified = await provider.search(candidate, destination)
            assert verified.place.placeId == 'official-louvre-id'
            if case == 'english_fallback':
                assert queries == ['루브르 박물관', 'Louvre Museum']


@pytest.mark.asyncio
@pytest.mark.parametrize('geographic_type', ['sublocality_level_2', 'neighborhood', 'locality', 'administrative_area_level_1', 'administrative_area_level_2', 'country'])
async def test_geographic_area_is_not_accepted_as_individual_stop(place_payload, geographic_type):
    """Exact name and valid coordinates do not turn an area centroid into a venue."""
    from app.agent.course_graph import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    payload = copy.deepcopy(place_payload)
    payload['primaryType'] = geographic_type
    payload['types'] = [geographic_type, 'political']
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload if request.method == 'GET' else {'places': [{'id': payload.get('id')}]}))) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        with pytest.raises(ValueError):
            await provider.search(Candidate(name='서울 미술관'), Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3))


@pytest.mark.asyncio
async def test_verified_named_park_remains_a_valid_individual_stop(place_payload):
    from app.agent.course_graph import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    payload = copy.deepcopy(place_payload)
    payload['displayName']['text'] = '서울숲'
    payload['primaryType'] = 'park'
    payload['types'] = ['park', 'tourist_attraction', 'point_of_interest', 'establishment']
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload if request.method == 'GET' else {'places': [{'id': payload.get('id')}]}))) as client:
        result = await VerifiedMapsProvider(client=client, api_key='offline').search(Candidate(name='서울숲'), Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3))
    assert result.place.placeName == '서울숲'
    assert result.place.category == 'park'
    assert result.place.placeId == payload['id']


@pytest.mark.asyncio
async def test_geographic_primary_type_cannot_be_hidden_by_poi_secondary_type(place_payload):
    from app.agent.course_graph import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    payload = copy.deepcopy(place_payload)
    payload['primaryType'] = 'neighborhood'
    payload['types'] = ['neighborhood', 'tourist_attraction']
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload if request.method == 'GET' else {'places': [{'id': payload.get('id')}]}))) as client:
        with pytest.raises(ValueError):
            await VerifiedMapsProvider(client=client, api_key='offline').search(Candidate(name='서울 미술관'), Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3))


@pytest.mark.asyncio
@pytest.mark.parametrize('meal', ['breakfast', 'lunch', 'dinner'])
async def test_breakfast_restaurant_category_only_establishes_breakfast_role(place_payload, meal):
    from app.agent.course_graph import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    payload = copy.deepcopy(place_payload)
    payload['primaryType'] = 'breakfast_restaurant'
    payload['types'] = ['breakfast_restaurant', 'restaurant', 'food']
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload if request.method == 'GET' else {'places': [{'id': payload.get('id')}]}))) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        destination = Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3)
        candidate = Candidate(name='서울 미술관', meal=meal)
        if meal == 'breakfast':
            assert (await provider.search(candidate, destination)).place.category == 'breakfast_restaurant'
        else:
            with pytest.raises(ValueError):
                await provider.search(candidate, destination)


@pytest.mark.asyncio
@pytest.mark.parametrize('transit_type', ['bus_stop', 'bus_station', 'train_station', 'subway_station', 'transit_station', 'light_rail_station'])
async def test_transit_access_point_is_not_an_attraction(place_payload, transit_type):
    """A station sharing an attraction name must not replace the actual attraction."""
    from app.agent.course_graph import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    payload = copy.deepcopy(place_payload)
    payload['primaryType'] = transit_type
    payload['types'] = [transit_type, 'point_of_interest', 'establishment']
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload if request.method == 'GET' else {'places': [{'id': payload.get('id')}]}))) as client:
        with pytest.raises(ValueError):
            await VerifiedMapsProvider(client=client, api_key='offline').search(Candidate(name='서울 미술관'), Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3))


@pytest.mark.asyncio
@pytest.mark.parametrize(('category', 'meal'), [('market', 'lunch'), ('market', 'dinner'), ('shopping_mall', 'dinner'), ('supermarket', 'lunch'), ('food', 'lunch'), ('cafe', 'lunch'), ('bakery', 'dinner'), ('dessert_restaurant', 'dinner')])
async def test_shopping_or_generic_food_type_does_not_establish_meal_service(place_payload, category, meal):
    from app.agent.course_graph import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    payload = copy.deepcopy(place_payload)
    payload['primaryType'] = category
    payload['types'] = [category, 'point_of_interest', 'establishment']
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload if request.method == 'GET' else {'places': [{'id': payload.get('id')}]}))) as client:
        with pytest.raises(ValueError):
            await VerifiedMapsProvider(client=client, api_key='offline').search(Candidate(name='서울 미술관', meal=meal), Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3))


@pytest.mark.asyncio
@pytest.mark.parametrize(('category', 'meal'), [('noodle_shop', 'lunch'), ('restaurant', 'dinner'), ('food_court', 'lunch')])
async def test_concrete_meal_venue_types_remain_accepted(place_payload, category, meal):
    from app.agent.course_graph import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    payload = copy.deepcopy(place_payload)
    payload['primaryType'] = category
    payload['types'] = [category, 'point_of_interest', 'establishment']
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload if request.method == 'GET' else {'places': [{'id': payload.get('id')}]}))) as client:
        verified = await VerifiedMapsProvider(client=client, api_key='offline').search(Candidate(name='서울 미술관', meal=meal), Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3))
    assert verified.place.category == category
    assert verified.place.placeId == payload['id']


@pytest.mark.asyncio
async def test_market_complex_is_not_meal_venue_from_secondary_restaurant_tag(place_payload):
    from app.agent.course_graph import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    payload = copy.deepcopy(place_payload)
    payload['primaryType'] = 'market'
    payload['types'] = ['market', 'restaurant', 'food', 'tourist_attraction']
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload if request.method == 'GET' else {'places': [{'id': payload.get('id')}]}))) as client:
        with pytest.raises(ValueError):
            await VerifiedMapsProvider(client=client, api_key='offline').search(Candidate(name='서울 미술관', meal='dinner'), Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3))


@pytest.mark.asyncio
async def test_single_id_search_uses_ids_mask_then_complete_details(place_payload):
    import json

    from app.agent.course_graph import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    requests = []

    def respond(request):
        requests.append(request)
        if request.method == 'POST':
            assert request.headers['X-Goog-FieldMask'] == 'places.id'
            body = json.loads(request.content)
            assert body['pageSize'] == 5 and body['languageCode'] == 'ko'
            assert body['textQuery'] == '서울 미술관'
            assert body['locationBias']['rectangle']['low'] == {'latitude': 37.3, 'longitude': 126.7}
            return httpx.Response(200, json={'places': [{'id': place_payload['id']}]})
        assert request.method == 'GET'
        assert request.url.path == '/v1/places/verified-museum'
        assert request.url.params['languageCode'] == 'ko'
        mask = set(request.headers['X-Goog-FieldMask'].split(','))
        assert {'id', 'displayName', 'formattedAddress', 'addressComponents', 'location', 'businessStatus', 'primaryType', 'types', 'rating', 'regularOpeningHours'} <= mask
        assert all(not field.startswith('places.') for field in mask)
        return httpx.Response(200, json=place_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='private-test-key')
        place = await provider.search(Candidate(name='서울 미술관'), Destination('KR', 37.3, 126.7, 37.8, 127.3))
        metrics = provider.metrics.snapshot()
    assert place.place.placeId == place_payload['id']
    assert [request.method for request in requests] == ['POST', 'GET']
    assert metrics['skus']['text_search_ids']['requests'] == 1
    assert metrics['skus']['place_details_enterprise']['requests'] == 1
    assert metrics['skus'].get('text_search_enterprise', {}).get('requests', 0) == 0
    assert metrics['estimate_complete']
    assert 'private-test-key' not in json.dumps(metrics)


@pytest.mark.asyncio
@pytest.mark.parametrize('ambiguous', [False, True])
async def test_multiple_ids_use_full_search_without_details_and_keep_ambiguity_rule(place_payload, ambiguous):
    from app.agent.course_graph import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    calls = []
    other = copy.deepcopy(place_payload)
    other['id'] = 'other-museum'
    if not ambiguous:
        other['displayName']['text'] = '다른 명소'

    def respond(request):
        assert request.method == 'POST'
        calls.append(request.headers['X-Goog-FieldMask'])
        if request.headers['X-Goog-FieldMask'] == 'places.id':
            return httpx.Response(200, json={'places': [{'id': place_payload['id']}, {'id': other['id']}]})
        assert 'places.location' in request.headers['X-Goog-FieldMask']
        return httpx.Response(200, json={'places': [place_payload, other]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        if ambiguous:
            with pytest.raises(ValueError):
                await provider.search(Candidate(name='서울 미술관'), Destination('KR', 37.3, 126.7, 37.8, 127.3))
        else:
            assert (await provider.search(Candidate(name='서울 미술관'), Destination('KR', 37.3, 126.7, 37.8, 127.3))).place.placeId == place_payload['id']
        metrics = provider.metrics.snapshot()
    assert len(calls) == 2 and calls[0] == 'places.id'
    assert metrics['skus']['text_search_enterprise']['requests'] == 1
    assert metrics['skus'].get('place_details_enterprise', {}).get('requests', 0) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('malformed_id', [None, '', 'bad/id', ' white space', 123])
async def test_mixed_malformed_ids_do_not_turn_ambiguous_search_into_single_hit(place_payload, malformed_id):
    from app.agent.course_graph import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    calls = []

    def respond(request):
        calls.append(request)
        assert request.method == 'POST'
        assert request.headers['X-Goog-FieldMask'] == 'places.id'
        return httpx.Response(200, json={'places': [{'id': place_payload['id']}, {'id': malformed_id}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError):
            await VerifiedMapsProvider(client=client, api_key='offline').search(Candidate(name='서울 미술관'), Destination('KR', 37.3, 126.7, 37.8, 127.3))
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mismatch', [False, True])
async def test_duplicate_search_ids_require_one_detail_and_same_response_id(place_payload, mismatch):
    from app.agent.course_graph import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    methods = []

    def respond(request):
        methods.append(request.method)
        if request.method == 'POST':
            return httpx.Response(200, json={'places': [{'id': place_payload['id']}, {'id': place_payload['id']}]})
        return httpx.Response(200, json={**place_payload, 'id': 'unrequested-other-id' if mismatch else place_payload['id']})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        if mismatch:
            with pytest.raises(ValueError):
                await provider.search(Candidate(name='서울 미술관'), Destination('KR', 37.3, 126.7, 37.8, 127.3))
        else:
            assert (await provider.search(Candidate(name='서울 미술관'), Destination('KR', 37.3, 126.7, 37.8, 127.3))).place.placeId == place_payload['id']
    assert methods == ['POST', 'GET']


@pytest.mark.asyncio
async def test_same_place_details_singleflight_is_shared_across_meal_roles(place_payload):
    import asyncio

    from app.agent.course_graph import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    payload = {**place_payload, 'primaryType': 'restaurant', 'types': ['restaurant']}
    calls = []

    async def respond(request):
        calls.append(request.method)
        await asyncio.sleep(.01)
        if request.method == 'POST':
            return httpx.Response(200, json={'places': [{'id': payload['id']}]})
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        places = await asyncio.gather(*[provider.search(Candidate(name='서울 미술관', meal=meal), Destination('KR', 37.3, 126.7, 37.8, 127.3)) for meal in ['none', 'lunch', 'dinner']])
        metrics = provider.metrics.snapshot()
    assert len(places) == 3
    assert calls == ['POST', 'GET']
    assert metrics['skus']['place_details_enterprise']['requests'] == 1
    assert metrics['skus']['place_details_enterprise']['cache_hits'] == 2


@pytest.mark.asyncio
async def test_details_cache_keeps_languages_separate_for_english_fallback(place_payload):
    import json

    from app.agent.course_graph import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    languages = []

    def respond(request):
        if request.method == 'POST':
            assert json.loads(request.content)['languageCode'] in {'ko', 'en'}
            return httpx.Response(200, json={'places': [{'id': place_payload['id']}]})
        language = request.url.params['languageCode']
        languages.append(language)
        return httpx.Response(200, json={**place_payload, 'displayName': {'text': '다른 이름' if language == 'ko' else 'Seoul Museum'}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        place = await VerifiedMapsProvider(client=client, api_key='offline').search(Candidate(name='서울 미술관', english_name='Seoul Museum'), Destination('KR', 37.3, 126.7, 37.8, 127.3))
    assert place.place.placeName == 'Seoul Museum'
    assert languages == ['ko', 'en']


@pytest.mark.asyncio
async def test_failed_details_are_not_success_cached_and_metrics_count_real_sends(place_payload):
    import json

    from app.agent.course_graph import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    details = 0

    def respond(request):
        nonlocal details
        if request.method == 'POST':
            return httpx.Response(200, json={'places': [{'id': place_payload['id']}]})
        details += 1
        if details == 1:
            return httpx.Response(503, json={'error': 'temporary failure'})
        return httpx.Response(200, json=place_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='private-test-key')
        candidate, destination = Candidate(name='서울 미술관'), Destination('KR', 37.3, 126.7, 37.8, 127.3)
        assert (await provider.search(candidate, destination)).place.placeId == place_payload['id']
        assert (await provider.search(candidate, destination)).place.placeId == place_payload['id']
        metrics = provider.metrics.snapshot()
    assert details == 2
    sku = metrics['skus']['place_details_enterprise']
    assert sku['requests'] == 2 and sku['responses_200'] == 1 and sku['errors'] == 1
    assert metrics['skus']['text_search_ids']['requests'] == 1
    assert metrics['skus']['text_search_ids']['cache_hits'] == 1
    assert sku['estimated_list_cost_usd'] == pytest.approx(2 * sku['unit_price_usd'])
    assert 'private-test-key' not in json.dumps(metrics)


@pytest.mark.asyncio
async def test_cancelled_details_count_only_sent_attempt_and_can_be_retried(place_payload):
    import asyncio

    from app.agent.course_graph import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    started = asyncio.Event()
    detail_calls = 0

    async def respond(request):
        nonlocal detail_calls
        if request.method == 'POST':
            return httpx.Response(200, json={'places': [{'id': place_payload['id']}]})
        detail_calls += 1
        if detail_calls == 1:
            started.set()
            await asyncio.Event().wait()
        return httpx.Response(200, json=place_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        candidate, destination = Candidate(name='서울 미술관'), Destination('KR', 37.3, 126.7, 37.8, 127.3)
        task = asyncio.create_task(provider.search(candidate, destination))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await provider.search(candidate, destination)).place.placeId == place_payload['id']
        sku = provider.metrics.snapshot()['skus']['place_details_enterprise']
    assert detail_calls == 2
    assert (sku['requests'], sku['responses_200'], sku['errors'], sku['cache_hits']) == (2, 1, 1, 0)
    assert sku['estimated_list_cost_usd'] == pytest.approx(2 * sku['unit_price_usd'])
    assert sku['successful_list_cost_usd'] == pytest.approx(sku['unit_price_usd'])


@pytest.mark.asyncio
async def test_actual_masks_drive_sku_cost_and_unknown_fields_keep_estimate_incomplete():
    import json

    from app.agent.tools.verified_maps import VerifiedMapsProvider

    requests = []

    def respond(request):
        requests.append(request)
        payload = {'routes': [{'duration': '600s', 'distanceMeters': 800}]} if request.url.host == 'routes.googleapis.com' else {}
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='private-test-key')
        search_url = 'https://places.googleapis.com/v1/places:searchText'
        body = {'textQuery': 'sensitive-test-query', 'pageSize': 5}
        for mask in ['places.id,places.displayName,places.viewport', 'places.id,places.rating', 'places.id,places.unknownBillableField']:
            await provider._request('POST', search_url, headers={'X-Goog-Api-Key': 'private-test-key', 'X-Goog-FieldMask': mask}, json=body)
        route_options = {'headers': {'X-Goog-FieldMask': 'routes.duration,routes.distanceMeters'}, 'json': {'origin': {}, 'destination': {}, 'travelMode': 'WALK'}}
        await provider._request('POST', 'https://routes.googleapis.com/directions/v2:computeRoutes', **route_options)
        await provider._request('POST', 'https://routes.googleapis.com/directions/v2:computeRoutes', **route_options)
        metrics = provider.metrics.snapshot()
    assert len(requests) == 4
    for sku in ['text_search_pro', 'text_search_enterprise', 'unknown', 'routes_essentials']:
        assert metrics['skus'][sku]['requests'] == 1
    assert metrics['skus']['routes_essentials']['cache_hits'] == 1
    assert not metrics['estimate_complete']
    assert metrics['skus']['unknown']['estimated_list_cost_usd'] is None
    assert metrics['estimated_list_cost_usd'] == pytest.approx(sum(metrics['skus'][sku]['unit_price_usd'] for sku in ['text_search_pro', 'text_search_enterprise', 'routes_essentials']))
    assert 'private-test-key' not in json.dumps(metrics)
    assert 'sensitive-test-query' not in json.dumps(metrics)


@pytest.mark.asyncio
async def test_cancelled_lock_and_capacity_waiters_are_not_http_attempts(place_payload):
    import asyncio

    from app.agent.course_graph import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def respond(request):
        calls.append(request.method)
        if request.method == 'POST':
            return httpx.Response(200, json={'places': [{'id': place_payload['id']}]})
        started.set()
        await release.wait()
        return httpx.Response(200, json=place_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline', concurrency=1)
        candidate, destination = Candidate(name='서울 미술관'), Destination('KR', 37.3, 126.7, 37.8, 127.3)
        first = asyncio.create_task(provider.search(candidate, destination))
        await asyncio.wait_for(started.wait(), timeout=1)
        same = asyncio.create_task(provider.search(candidate, destination))
        other = asyncio.create_task(provider.search(Candidate(name='다른 미술관'), destination))
        await asyncio.sleep(0)
        same.cancel()
        other.cancel()
        for task in [same, other]:
            with pytest.raises(asyncio.CancelledError):
                await task
        release.set()
        assert (await first).place.placeId == place_payload['id']
        metrics = provider.metrics.snapshot()
    assert calls == ['POST', 'GET']
    assert sum(sku['requests'] for sku in metrics['skus'].values()) == 2
    assert sum(sku['errors'] for sku in metrics['skus'].values()) == 0
    assert metrics['skus']['text_search_ids']['wrapper_calls'] == 3
    assert metrics['skus']['place_details_enterprise']['wrapper_calls'] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_body', [b'not json', b'[]', b'{"error": {"message": "unusable"}}'])
async def test_http_200_invalid_payload_records_response_and_error_without_success_cache(place_payload, invalid_body):
    from app.agent.course_graph import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    details = 0

    def respond(request):
        nonlocal details
        if request.method == 'POST':
            return httpx.Response(200, json={'places': [{'id': place_payload['id']}]})
        details += 1
        return httpx.Response(200, content=invalid_body) if details == 1 else httpx.Response(200, json=place_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        candidate, destination = Candidate(name='서울 미술관'), Destination('KR', 37.3, 126.7, 37.8, 127.3)
        with pytest.raises(ValueError):
            await provider.search(candidate, destination)
        assert (await provider.search(candidate, destination)).place.placeId == place_payload['id']
        sku = provider.metrics.snapshot()['skus']['place_details_enterprise']
    assert details == 2
    assert (sku['requests'], sku['responses_200'], sku['errors'], sku['cache_hits']) == (2, 2, 1, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [429, 503, 'timeout'])
async def test_transient_maps_request_retries_once_then_caches_only_success(failure):
    from app.agent.tools.verified_maps import VerifiedMapsProvider, VerifiedPlace

    sends = 0

    def respond(request):
        nonlocal sends
        sends += 1
        if sends == 1:
            if failure == 'timeout':
                raise httpx.ReadTimeout('temporary', request=request)
            return httpx.Response(failure, json={'error': {'message': 'temporary'}})
        return httpx.Response(200, json={'routes': [{'duration': '600s', 'distanceMeters': 800}]})

    first = VerifiedPlace(PlaceSchema(placeId='places/a', placeName='A', category='museum', latitude=37.55, longitude=126.98))
    second = VerifiedPlace(PlaceSchema(placeId='places/b', placeName='B', category='museum', latitude=37.551, longitude=126.98))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        assert (await provider.route(first, second)).minutes == 10
        assert (await provider.route(first, second)).minutes == 10
        metrics = provider.metrics.snapshot()['skus']['routes_essentials']
    assert sends == 2
    assert metrics['requests'] == 2
    assert metrics['errors'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('status', [401, 403, 400])
async def test_permission_and_invalid_requests_are_typed_and_never_retried(status):
    from app.agent.tools.verified_maps import (
        MapsProviderError,
        VerifiedMapsProvider,
        VerifiedPlace,
    )

    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(status, json={'error': {'message': 'secret provider detail'}})

    first = VerifiedPlace(PlaceSchema(placeId='places/a', placeName='A', category='museum', latitude=37.55, longitude=126.98))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(MapsProviderError) as error:
            await VerifiedMapsProvider(client=client, api_key='private-test-key').route(first, first)
    assert error.value.status_code == status
    assert error.value.kind == ('unauthorized' if status in {401, 403} else 'invalid')
    assert not error.value.transient
    assert len(calls) == 1
    assert 'private-test-key' not in str(error.value)
    assert 'secret provider detail' not in str(error.value)


@pytest.mark.asyncio
async def test_empty_routes_do_not_poison_success_cache():
    from app.agent.tools.verified_maps import (
        NoRouteError,
        VerifiedMapsProvider,
        VerifiedPlace,
    )

    sends = 0

    def respond(request):
        nonlocal sends
        sends += 1
        return httpx.Response(200, json={'routes': [] if sends == 1 else [{'duration': '600s', 'distanceMeters': 800}]})

    first = VerifiedPlace(PlaceSchema(placeId='places/a', placeName='A', category='museum', latitude=37.55, longitude=126.98))
    second = VerifiedPlace(PlaceSchema(placeId='places/b', placeName='B', category='museum', latitude=37.551, longitude=126.98))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        with pytest.raises(NoRouteError):
            await provider.route(first, second)
        assert (await provider.route(first, second)).minutes == 10
    assert sends == 2


@pytest.mark.asyncio
async def test_transit_uses_supplied_trip_departure_time():
    import json
    from datetime import datetime

    from app.agent.tools.verified_maps import VerifiedMapsProvider, VerifiedPlace

    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={'routes': [{'duration': '600s', 'distanceMeters': 800}]})

    first = VerifiedPlace(PlaceSchema(placeId='places/a', placeName='A', category='museum', latitude=37.55, longitude=126.98))
    second = VerifiedPlace(PlaceSchema(placeId='places/b', placeName='B', category='museum', latitude=37.551, longitude=126.98))
    departure = datetime(2026, 10, 5, 1, 0, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        await provider.route(first, second, mode='transit', departure_time=departure)
        metrics = provider.metrics.snapshot()
    assert metrics['estimate_complete']
    assert metrics['skus']['routes_essentials']['requests'] == 1
    assert 'unknown' not in metrics['skus']
    assert bodies[0]['travelMode'] == 'TRANSIT'
    actual = datetime.fromisoformat(bodies[0]['departureTime'])
    assert actual == departure


@pytest.mark.asyncio
@pytest.mark.parametrize(('mode', 'label', 'departure_supplied'), [
    ('walking', '도보', False), ('transit', '대중교통', False),
    ('transit', '대중교통', True), ('driving', '차량', True), ('taxi', '택시', False),
])
async def test_missing_route_steps_keep_compact_mode_action_and_actual_departure_request(mode, label, departure_supplied):
    import json
    from datetime import datetime, timedelta, timezone

    from app.agent.tools.verified_maps import VerifiedMapsProvider, VerifiedPlace

    bodies = []
    def respond(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={'routes': [{'duration': '601s', 'distanceMeters': 1250}]})
    origin = VerifiedPlace(PlaceSchema(placeId='origin', placeName='출발 장소', category='museum', latitude=35.67, longitude=139.76))
    destination = VerifiedPlace(PlaceSchema(placeId='destination', placeName='다음 방문 장소', category='park', latitude=35.68, longitude=139.76))
    departure = datetime(2026, 10, 5, 14, 25, tzinfo=timezone(timedelta(hours=9))) if departure_supplied else None
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await VerifiedMapsProvider(client=client, api_key='offline').route(origin, destination, mode=mode, departure_time=departure)
    assert len(bodies) == 1
    assert result.type == mode and result.distance == 1250 and result.minutes == 11
    assert result.cost == (0 if mode == 'walking' else None)
    assert label in result.memo and len(result.memo) <= 120
    assert '11분' not in result.memo and '1250' not in result.memo
    assert not any(blanket in result.memo for blanket in ['재확인이 필요', '운행·혼잡·요금', '확정 운행', '승차장', '환승'])
    if mode == 'transit' and departure_supplied:
        assert bodies[0]['departureTime'] == '2026-10-05T05:25:00Z'
    else:
        assert 'departureTime' not in bodies[0]
    assert not any(repeated in result.memo for repeated in ('다음 방문 장소까지', '2026-10-05', '14:25', 'UTC', '조회 시점', '출발 기준'))


@pytest.mark.asyncio
async def test_transient_outage_is_bounded_and_fresh_call_can_recover():
    from app.agent.tools.verified_maps import (
        MapsProviderError,
        VerifiedMapsProvider,
        VerifiedPlace,
    )

    sends = 0

    def respond(request):
        nonlocal sends
        sends += 1
        return httpx.Response(503, json={'error': {}}) if sends <= 2 else httpx.Response(200, json={'routes': [{'duration': '600s', 'distanceMeters': 800}]})

    first = VerifiedPlace(PlaceSchema(placeId='places/a', placeName='A', category='museum', latitude=37.55, longitude=126.98))
    second = VerifiedPlace(PlaceSchema(placeId='places/b', placeName='B', category='museum', latitude=37.551, longitude=126.98))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        with pytest.raises(MapsProviderError) as error:
            await provider.route(first, second)
        assert sends == 2
        assert error.value.transient
        assert (await provider.route(first, second)).minutes == 10
    assert sends == 3


@pytest.mark.asyncio
async def test_malformed_route_metrics_are_not_reused_as_success_cache():
    from app.agent.tools.verified_maps import (
        MapsProviderError,
        VerifiedMapsProvider,
        VerifiedPlace,
    )

    sends = 0

    def respond(request):
        nonlocal sends
        sends += 1
        return httpx.Response(200, json={'routes': [{'duration': 'bad' if sends == 1 else '600s', 'distanceMeters': 800}]})

    first = VerifiedPlace(PlaceSchema(placeId='places/a', placeName='A', category='museum', latitude=37.55, longitude=126.98))
    second = VerifiedPlace(PlaceSchema(placeId='places/b', placeName='B', category='museum', latitude=37.551, longitude=126.98))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        with pytest.raises(MapsProviderError) as error:
            await provider.route(first, second)
        assert error.value.kind == 'route_data' and not error.value.transient
        assert sends == 1  # Semantic invalidity is not a transient retry condition.
        assert (await provider.route(first, second)).minutes == 10
    assert sends == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(('requested', 'official'), [
    ('수원', '수원시'), ('수원시', '수원'),
    ('서울', '서울특별시'), ('부산', '부산광역시'),
    ('시흥', '시흥시'), ('시흥시', '시흥'),
])
async def test_official_korean_administrative_name_can_differ_from_input(requested, official):
    import json

    from app.agent.tools.verified_maps import VerifiedMapsProvider

    country = {'longText': '대한민국', 'shortText': 'KR', 'types': ['country']}
    city = {
        'id': 'verified-city', 'displayName': {'text': official}, 'types': ['locality'],
        'addressComponents': [country, {'longText': official, 'types': ['locality']}],
        'viewport': {'low': {'latitude': 37.1, 'longitude': 126.5}, 'high': {'latitude': 37.8, 'longitude': 127.5}},
    }

    def respond(request):
        query = json.loads(request.content)['textQuery']
        return httpx.Response(200, json={'places': [{'id': 'verified-country', 'types': ['country'], 'addressComponents': [country]}] if query == '대한민국' else [city]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        destination = await VerifiedMapsProvider(client=client, api_key='offline').resolve_destination('대한민국', requested)
    assert destination.country_code == 'KR'
    assert destination.contains(37.3, 127.0)


@pytest.mark.asyncio
@pytest.mark.parametrize('requested_country', ['한국', 'South Korea', 'Republic of Korea', 'KR'])
async def test_country_lookup_uses_actual_country_entity_despite_input_name_difference(requested_country):
    import json

    from app.agent.tools.verified_maps import VerifiedMapsProvider

    country = {'longText': '대한민국', 'shortText': 'KR', 'types': ['country']}
    city = {
        'id': 'verified-suwon', 'displayName': {'text': '수원시'}, 'types': ['locality'],
        'addressComponents': [country, {'longText': '수원시', 'types': ['locality']}],
        'viewport': {'low': {'latitude': 37.1, 'longitude': 126.5}, 'high': {'latitude': 37.8, 'longitude': 127.5}},
    }

    def respond(request):
        query = json.loads(request.content)['textQuery']
        return httpx.Response(200, json={'places': [{'id': 'verified-country', 'types': ['country'], 'addressComponents': [country]}] if query == requested_country else [city]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        destination = await VerifiedMapsProvider(client=client, api_key='offline').resolve_destination(requested_country, '수원')
    assert destination.country_code == 'KR'


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['district_prefix', 'removed_internal_character', 'wrong_city_country', 'ambiguous_variants', 'ambiguous_country_name', 'inconsistent_country_codes', 'non_korean_suffix', 'district_with_matching_parent'])
async def test_common_country_entity_validation_preserves_ambiguity_and_mismatch_guards(case):
    import json

    from app.agent.tools.verified_maps import VerifiedMapsProvider

    requested_country, requested_city, official_city = '대한민국', '수원', '수원시'
    country = {'longText': '대한민국', 'shortText': 'KR', 'types': ['country']}
    city_country = copy.deepcopy(country)
    if case == 'district_prefix':
        official_city = '수원시영통구'
    elif case == 'removed_internal_character':
        requested_city, official_city = '흥', '시흥시'
    elif case == 'wrong_city_country':
        city_country['shortText'] = 'JP'
    elif case == 'ambiguous_country_name':
        requested_country = 'Korea'
    elif case == 'inconsistent_country_codes':
        requested_country = '한국'
        country = {'longText': '조선민주주의인민공화국', 'shortText': 'KP', 'types': ['country']}
        city_country = {'longText': '대한민국', 'shortText': 'KR', 'types': ['country']}
    elif case == 'non_korean_suffix':
        requested_country = 'Japan'
        country = {'longText': 'Japan', 'shortText': 'JP', 'types': ['country']}
        city_country = copy.deepcopy(country)
    city = {
        'id': 'verified-city', 'displayName': {'text': official_city}, 'types': ['locality'],
        'addressComponents': [city_country, {'longText': official_city, 'types': ['locality']}],
        'viewport': {'low': {'latitude': 37.1, 'longitude': 126.5}, 'high': {'latitude': 37.8, 'longitude': 127.5}},
    }
    if case == 'district_with_matching_parent':
        city['displayName']['text'] = '수원시 영통구'
        city['types'] = ['administrative_area_level_2']
        city['addressComponents'] = [city_country, {'longText': '영통구', 'types': ['administrative_area_level_2']}, {'longText': '수원시', 'types': ['locality']}]
    cities = [city]
    if case == 'ambiguous_variants':
        alternative = copy.deepcopy(city)
        alternative['id'] = 'other-verified-city'
        alternative['displayName']['text'] = '수원'
        alternative['addressComponents'][1]['longText'] = '수원'
        cities.append(alternative)

    def respond(request):
        query = json.loads(request.content)['textQuery']
        countries = [{'id': 'verified-country', 'types': ['country'], 'addressComponents': [country]}]
        if case == 'ambiguous_country_name':
            countries.append({'id': 'other-country', 'types': ['country'], 'addressComponents': [{'shortText': 'KP', 'types': ['country']}]})
        return httpx.Response(200, json={'places': countries if query == requested_country else cities})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        # The common contract validates the provider's single geographic entity.
        # It does not claim to prove free-text intent from display-name equality.
        if case in {'district_prefix', 'removed_internal_character', 'non_korean_suffix', 'district_with_matching_parent'}:
            destination = await provider.resolve_destination(requested_country, requested_city)
            assert destination.country_code == country['shortText']
            assert destination.place_id == city['id']
        else:
            with pytest.raises(ValueError):
                await provider.resolve_destination(requested_country, requested_city)


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['wrong_country', 'out_of_viewport', 'far', 'closed', 'nonmeal', 'invalid_coords', 'missing_id', 'missing_address', 'missing_name'])
async def test_meal_discovery_filters_invalid_facts_without_inventing_venue_names(place_payload, invalid):
    import json

    from app.agent.tools.verified_maps import (
        Destination,
        VerifiedMapsProvider,
        VerifiedPlace,
    )

    valid = copy.deepcopy(place_payload)
    valid['id'] = 'official-restaurant'
    valid['displayName']['text'] = '공식 식당명'
    valid['primaryType'] = 'restaurant'
    valid['types'] = ['restaurant', 'food', 'establishment']
    invalid_place = copy.deepcopy(valid)
    invalid_place['id'] = 'invalid-restaurant'
    if invalid == 'wrong_country':
        invalid_place['addressComponents'][0]['shortText'] = 'JP'
    elif invalid == 'out_of_viewport':
        invalid_place['location']['latitude'] = 38.0
    elif invalid == 'far':
        invalid_place['location']['latitude'] = 37.59
    elif invalid == 'closed':
        invalid_place['businessStatus'] = 'CLOSED_PERMANENTLY'
    elif invalid == 'nonmeal':
        invalid_place['primaryType'] = 'dessert_restaurant'
        invalid_place['types'] = ['dessert_restaurant', 'food']
    elif invalid == 'invalid_coords':
        invalid_place['location']['latitude'] = 'NaN'
    elif invalid == 'missing_id':
        invalid_place.pop('id')
    elif invalid == 'missing_address':
        invalid_place.pop('formattedAddress')
    else:
        invalid_place['displayName']['text'] = ''
    sends = []

    def respond(request):
        sends.append(request)
        body = json.loads(request.content)
        assert request.method == 'POST'
        assert request.url.host == 'places.googleapis.com'
        assert request.url.path == '/v1/places:searchText'
        assert 1 <= body['pageSize'] <= 10
        mask = set(request.headers['X-Goog-FieldMask'].split(','))
        assert {'places.id', 'places.displayName', 'places.addressComponents', 'places.location', 'places.types', 'places.businessStatus', 'places.regularOpeningHours'} <= mask
        return httpx.Response(200, json={'places': [valid, invalid_place, copy.deepcopy(valid)]})

    anchor = VerifiedPlace(PlaceSchema(placeId='anchor', placeName='실제 명소', category='museum', latitude=37.57, longitude=126.98))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        result = await provider.discover_meals(Destination('KR', 37.3, 126.7, 37.8, 127.3), anchor)
    assert len(sends) == 1
    assert len(result) == 1
    assert result[0].place.placeId == 'official-restaurant'
    assert result[0].place.placeName == '공식 식당명'
    assert result[0].periods == valid['regularOpeningHours']['periods']


@pytest.mark.asyncio
async def test_meal_discovery_bounds_results_even_if_provider_returns_more(place_payload):
    from app.agent.tools.verified_maps import (
        Destination,
        VerifiedMapsProvider,
        VerifiedPlace,
    )

    venues = []
    for index in range(8):
        item = copy.deepcopy(place_payload)
        item.update({'id': f'restaurant-{index}', 'primaryType': 'restaurant', 'types': ['restaurant']})
        item['displayName']['text'] = f'공식 식당 {index}'
        venues.append(item)
    anchor = VerifiedPlace(PlaceSchema(placeId='anchor', placeName='명소', category='museum', latitude=37.57, longitude=126.98))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'places': venues}))) as client:
        result = await VerifiedMapsProvider(client=client, api_key='offline').discover_meals(Destination('KR', 37.3, 126.7, 37.8, 127.3), anchor)
    assert len(result) == 5
    assert len({venue.place.placeId for venue in result}) == 5


@pytest.mark.asyncio
async def test_meal_discovery_filters_before_limiting_valid_results(place_payload):
    from app.agent.tools.verified_maps import (
        Destination,
        VerifiedMapsProvider,
        VerifiedPlace,
    )

    venues = []
    for index in range(8):
        item = copy.deepcopy(place_payload)
        item.update({'id': f'restaurant-{index}', 'primaryType': 'restaurant', 'types': ['restaurant']})
        item['displayName']['text'] = f'공식 식당 {index}'
        if index < 5:
            item['businessStatus'] = 'CLOSED_PERMANENTLY'
        venues.append(item)
    anchor = VerifiedPlace(PlaceSchema(placeId='anchor', placeName='명소', category='museum', latitude=37.57, longitude=126.98))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'places': venues}))) as client:
        result = await VerifiedMapsProvider(client=client, api_key='offline').discover_meals(Destination('KR', 37.3, 126.7, 37.8, 127.3), anchor)
    assert {venue.place.placeId for venue in result} == {'restaurant-5', 'restaurant-6', 'restaurant-7'}


@pytest.mark.asyncio
async def test_attraction_discovery_uses_one_small_nearby_query_and_only_verified_official_facts(place_payload):
    """Local refill obtains provider identity and location without model guessing."""
    import json

    from app.agent.tools.verified_maps import (
        Destination,
        VerifiedMapsProvider,
        VerifiedPlace,
    )

    anchor = VerifiedPlace(PlaceSchema(placeId='places/anchor', placeName='기존 명소', category='museum', address='대한민국 서울', latitude=37.57, longitude=126.98))
    destination = Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3)
    requests = []

    def respond(request):
        requests.append(request)
        assert request.method == 'POST' and request.url.path.endswith('places:searchText')
        body = json.loads(request.content)
        assert body['pageSize'] <= 5
        assert body['locationBias']['circle']['radius'] <= 2000
        assert body['locationBias']['circle']['center'] == {'latitude': 37.57, 'longitude': 126.98}
        return httpx.Response(200, json={'places': [place_payload, copy.deepcopy(place_payload)]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        found = await provider.discover_attractions(destination, anchor)
    assert len(requests) == 1 and len(found) == 1
    assert found[0].place.placeId.endswith(place_payload['id'])
    assert found[0].place.placeName == place_payload['displayName']['text']
    assert found[0].place.address == place_payload['formattedAddress']
    assert found[0].periods == place_payload['regularOpeningHours']['periods']


@pytest.mark.parametrize('mutation', ['missing_id', 'food', 'cafe', 'closed', 'wrong_country', 'outside_city', 'far', 'nan', 'missing_address', 'geographic_area', 'hospital'])
@pytest.mark.asyncio
async def test_attraction_discovery_discards_unusable_places_without_satisfying_core_count(place_payload, mutation):
    """API supplied entries remain untrusted until their core-place facts pass."""
    from app.agent.tools.verified_maps import (
        Destination,
        VerifiedMapsProvider,
        VerifiedPlace,
    )

    payload = copy.deepcopy(place_payload)
    if mutation == 'missing_id':
        payload.pop('id')
    elif mutation in {'food', 'cafe', 'geographic_area', 'hospital'}:
        payload['types'] = [{'food': 'restaurant', 'cafe': 'coffee_shop', 'geographic_area': 'locality', 'hospital': 'hospital'}[mutation]]
    elif mutation == 'closed':
        payload['businessStatus'] = 'CLOSED_PERMANENTLY'
    elif mutation == 'wrong_country':
        payload['addressComponents'][0]['shortText'] = 'JP'
    elif mutation == 'outside_city':
        payload['location']['latitude'] = 38.0
    elif mutation == 'far':
        payload['location']['latitude'] = 37.7
    elif mutation == 'nan':
        payload['location']['latitude'] = 'NaN'
    elif mutation == 'missing_address':
        payload.pop('formattedAddress')
    anchor = VerifiedPlace(PlaceSchema(placeId='places/anchor', placeName='기존 명소', category='museum', address='대한민국 서울', latitude=37.57, longitude=126.98))
    destination = Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'places': [payload]}))) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        assert await provider.discover_attractions(destination, anchor) == []

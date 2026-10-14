"""Reject malformed external facts instead of turning them into plausible places/routes."""

import copy
import json
from datetime import UTC

import httpx
import pytest

from app.agent.tools.verified_maps import InternalPlaceSchema as PlaceSchema

ESSENTIAL_FIELDS = {'id', 'formattedAddress', 'addressComponents', 'location', 'types', 'viewport'}


def place_response(request, payloads):
    """Return only IDs at search and only Essentials facts at the matching detail URL."""
    records = payloads if isinstance(payloads, list) else [payloads]
    if request.method == 'POST':
        assert request.url.path == '/v1/places:searchText'
        assert request.headers['X-Goog-FieldMask'] == 'places.id'
        return httpx.Response(200, json={'places': [{'id': item.get('id')} for item in records]})
    assert request.method == 'GET' and request.url.path.startswith('/v1/places/')
    assert set(request.headers['X-Goog-FieldMask'].split(',')) <= ESSENTIAL_FIELDS
    identifier = request.url.path.rsplit('/', 1)[-1]
    item = next((item for item in records if item.get('id') == identifier), records[0])
    mask = set(request.headers['X-Goog-FieldMask'].split(','))
    return httpx.Response(200, json={key: value for key, value in item.items() if key in mask})


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
    }


@pytest.mark.asyncio
async def test_provider_uses_place_facts_from_http_response(place_payload):
    from app.agent.course_state import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    def respond(request):
        return place_response(request, place_payload)

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
    assert place.periods is None


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['missing_id', 'out_of_bounds', 'wrong_country', 'nan', 'missing_address', 'bool_coordinate', 'missing_category', 'malformed_types', 'conflicting_country'])
async def test_invalid_place_facts_are_rejected(place_payload, mutation):
    from app.agent.course_state import Candidate
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
    elif mutation == 'bool_coordinate':
        payload['location']['latitude'] = True
    elif mutation == 'missing_category':
        payload.pop('types')
    elif mutation == 'malformed_types':
        payload['types'] = 'museum'
    elif mutation == 'conflicting_country':
        payload['addressComponents'].append({'shortText': 'JP', 'types': ['country']})
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: place_response(request, payload))) as client:
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

    from app.agent.course_state import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    in_flight = peak = calls = 0

    async def respond(request):
        nonlocal in_flight, peak, calls
        in_flight += 1
        peak = max(peak, in_flight)
        calls += 1
        await asyncio.sleep(.01)
        in_flight -= 1
        return place_response(request, place_payload)

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
@pytest.mark.parametrize('case', ['candidate_label', 'english_fallback', 'nonfood_meal'])
async def test_named_candidate_uses_verified_essential_identity_and_preserves_search_label(place_payload, case):
    from app.agent.course_state import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    candidate = Candidate(name='루브르 박물관', english_name='Louvre Museum', meal='lunch' if case == 'nonfood_meal' else 'none')
    payload = {**place_payload, 'id': 'official-louvre-id'}
    queries = []
    methods = []

    def respond(request):
        methods.append(request.method)
        if request.method == 'POST':
            query = json.loads(request.content)['textQuery']
            queries.append(query)
            if case == 'english_fallback' and query == candidate.name:
                return httpx.Response(200, json={'places': []})
        return place_response(request, payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        destination = Destination('KR', 37.3, 126.7, 37.8, 127.3)
        if case == 'nonfood_meal':
            with pytest.raises(ValueError):
                await provider.search(candidate, destination)
        else:
            verified = await provider.search(candidate, destination)
            assert verified.place.placeId == payload['id']
            assert verified.place.placeName == candidate.name
            assert verified.place.placeEngName == candidate.english_name
            assert verified.periods is None
            assert queries == ([candidate.name, candidate.english_name] if case == 'english_fallback' else [candidate.name])
            assert methods[-1] == 'GET'


@pytest.mark.asyncio
@pytest.mark.parametrize('geographic_type', ['sublocality_level_2', 'neighborhood', 'locality', 'administrative_area_level_1', 'administrative_area_level_2', 'country'])
async def test_geographic_area_is_not_accepted_as_individual_stop(place_payload, geographic_type):
    """Exact name and valid coordinates do not turn an area centroid into a venue."""
    from app.agent.course_state import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    payload = copy.deepcopy(place_payload)
    payload['primaryType'] = geographic_type
    payload['types'] = [geographic_type, 'political']
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: place_response(request, payload))) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        with pytest.raises(ValueError):
            await provider.search(Candidate(name='서울 미술관'), Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3))


@pytest.mark.asyncio
async def test_verified_named_park_remains_a_valid_individual_stop(place_payload):
    from app.agent.course_state import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    payload = copy.deepcopy(place_payload)
    payload['displayName']['text'] = '서울숲'
    payload['primaryType'] = 'park'
    payload['types'] = ['park', 'tourist_attraction', 'point_of_interest', 'establishment']
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: place_response(request, payload))) as client:
        result = await VerifiedMapsProvider(client=client, api_key='offline').search(Candidate(name='서울숲'), Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3))
    assert result.place.placeName == '서울숲'
    assert result.place.category == 'park'
    assert result.place.placeId == payload['id']


@pytest.mark.asyncio
async def test_geographic_primary_type_cannot_be_hidden_by_poi_secondary_type(place_payload):
    from app.agent.course_state import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    payload = copy.deepcopy(place_payload)
    payload['primaryType'] = 'neighborhood'
    payload['types'] = ['neighborhood', 'tourist_attraction']
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: place_response(request, payload))) as client:
        with pytest.raises(ValueError):
            await VerifiedMapsProvider(client=client, api_key='offline').search(Candidate(name='서울 미술관'), Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3))


@pytest.mark.asyncio
@pytest.mark.parametrize('meal', ['breakfast', 'lunch', 'dinner'])
async def test_breakfast_restaurant_category_only_establishes_breakfast_role(place_payload, meal):
    from app.agent.course_state import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    payload = copy.deepcopy(place_payload)
    payload['primaryType'] = 'breakfast_restaurant'
    payload['types'] = ['breakfast_restaurant', 'restaurant', 'food']
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: place_response(request, payload))) as client:
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
    from app.agent.course_state import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    payload = copy.deepcopy(place_payload)
    payload['primaryType'] = transit_type
    payload['types'] = [transit_type, 'point_of_interest', 'establishment']
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: place_response(request, payload))) as client:
        with pytest.raises(ValueError):
            await VerifiedMapsProvider(client=client, api_key='offline').search(Candidate(name='서울 미술관'), Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3))


@pytest.mark.asyncio
@pytest.mark.parametrize(('category', 'meal'), [('market', 'lunch'), ('market', 'dinner'), ('shopping_mall', 'dinner'), ('supermarket', 'lunch'), ('food', 'lunch'), ('cafe', 'lunch'), ('bakery', 'dinner'), ('dessert_restaurant', 'dinner')])
async def test_shopping_or_generic_food_type_does_not_establish_meal_service(place_payload, category, meal):
    from app.agent.course_state import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    payload = copy.deepcopy(place_payload)
    payload['primaryType'] = category
    payload['types'] = [category, 'point_of_interest', 'establishment']
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: place_response(request, payload))) as client:
        with pytest.raises(ValueError):
            await VerifiedMapsProvider(client=client, api_key='offline').search(Candidate(name='서울 미술관', meal=meal), Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3))


@pytest.mark.asyncio
@pytest.mark.parametrize(('category', 'meal'), [('noodle_shop', 'lunch'), ('restaurant', 'dinner'), ('food_court', 'lunch')])
async def test_concrete_meal_venue_types_remain_accepted(place_payload, category, meal):
    from app.agent.course_state import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    payload = copy.deepcopy(place_payload)
    payload['primaryType'] = category
    payload['types'] = [category, 'point_of_interest', 'establishment']
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: place_response(request, payload))) as client:
        verified = await VerifiedMapsProvider(client=client, api_key='offline').search(Candidate(name='서울 미술관', meal=meal), Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3))
    assert verified.place.category == category
    assert verified.place.placeId == payload['id']


@pytest.mark.asyncio
async def test_market_complex_is_not_meal_venue_from_secondary_restaurant_tag(place_payload):
    from app.agent.course_state import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    payload = copy.deepcopy(place_payload)
    payload['primaryType'] = 'market'
    payload['types'] = ['market', 'restaurant', 'food', 'tourist_attraction']
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: place_response(request, payload))) as client:
        with pytest.raises(ValueError):
            await VerifiedMapsProvider(client=client, api_key='offline').search(Candidate(name='서울 미술관', meal='dinner'), Destination(country_code='KR', south=37.3, west=126.7, north=37.8, east=127.3))


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ko', 'en', 'ja', 'zh-TW'])
async def test_id_search_and_essential_details_supply_place_without_pro_fields_or_photos(place_payload, language):
    from app.agent.course_state import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    requests = []

    def respond(request):
        requests.append(request)
        if request.method == 'POST':
            body = json.loads(request.content)
            assert body['pageSize'] == 5 and body['languageCode'] == language
            assert body['textQuery'] == '서울 미술관'
            assert body['locationBias']['rectangle']['low'] == {'latitude': 37.3, 'longitude': 126.7}
        else:
            assert set(request.headers['X-Goog-FieldMask'].split(',')) == {'id', 'formattedAddress', 'addressComponents', 'location', 'types'}
            assert request.url.path == '/v1/places/verified-museum'
        return place_response(request, place_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='private-test-key', language=language)
        place = await provider.search(Candidate(name='서울 미술관'), Destination('KR', 37.3, 126.7, 37.8, 127.3))
        metrics = provider.metrics.snapshot()
    assert place.place.placeId == place_payload['id'] and place.periods is None
    assert [request.method for request in requests] == ['POST', 'GET']
    assert set(metrics['skus']) == {'text_search_ids', 'place_details_essentials'}
    assert all(bucket['requests'] == 1 for bucket in metrics['skus'].values())
    assert metrics['estimated_list_cost_usd'] == pytest.approx(.005)
    assert metrics['estimate_complete']
    assert 'private-test-key' not in json.dumps(metrics)


@pytest.mark.asyncio
@pytest.mark.parametrize('first_status', ['valid', 'wrong_country', 'hospital', 'doctor', 'insurance_agency'])
async def test_multiple_ids_choose_first_valid_travel_essential_record(place_payload, first_status):
    from app.agent.course_state import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    first = copy.deepcopy(place_payload)
    second = {**copy.deepcopy(place_payload), 'id': 'other-museum'}
    first_valid = first_status == 'valid'
    if first_status == 'wrong_country':
        first['addressComponents'][0]['shortText'] = 'JP'
    elif not first_valid:
        first['types'] = [first_status, 'point_of_interest', 'establishment']
    calls = []

    def respond(request):
        calls.append((request.method, request.url.path))
        return place_response(request, [first, second])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        place = await VerifiedMapsProvider(client=client, api_key='offline').search(Candidate(name='서울 미술관'), Destination('KR', 37.3, 126.7, 37.8, 127.3))
    assert place.place.placeId == (first['id'] if first_valid else second['id'])
    assert calls == [('POST', '/v1/places:searchText'), ('GET', '/v1/places/verified-museum')] + ([] if first_valid else [('GET', '/v1/places/other-museum')])


@pytest.mark.asyncio
@pytest.mark.parametrize('malformed_id', [None, '', 'bad/id', ' white space', 123])
async def test_malformed_individual_ids_are_never_requested_as_detail_urls(place_payload, malformed_id):
    from app.agent.course_state import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    calls = []

    def respond(request):
        calls.append(request)
        return place_response(request, [{'id': malformed_id}, place_payload])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        place = await VerifiedMapsProvider(client=client, api_key='offline').search(Candidate(name='서울 미술관'), Destination('KR', 37.3, 126.7, 37.8, 127.3))
    assert place.place.placeId == place_payload['id']
    assert [call.url.path for call in calls if call.method == 'GET'] == ['/v1/places/verified-museum']


@pytest.mark.asyncio
async def test_duplicate_identical_provider_records_remain_one_verified_match(place_payload):
    from app.agent.course_state import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    methods = []

    def respond(request):
        methods.append(request.method)
        return place_response(request, [place_payload, copy.deepcopy(place_payload)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        assert (await provider.search(Candidate(name='서울 미술관'), Destination('KR', 37.3, 126.7, 37.8, 127.3))).place.placeId == place_payload['id']
    assert methods == ['POST', 'GET']


@pytest.mark.asyncio
async def test_same_place_search_singleflight_is_shared_across_meal_roles(place_payload):
    import asyncio

    from app.agent.course_state import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    payload = {**place_payload, 'primaryType': 'restaurant', 'types': ['restaurant']}
    calls = []

    async def respond(request):
        calls.append(request.method)
        await asyncio.sleep(.01)
        return place_response(request, payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        places = await asyncio.gather(*[provider.search(Candidate(name='서울 미술관', meal=meal), Destination('KR', 37.3, 126.7, 37.8, 127.3)) for meal in ['none', 'lunch', 'dinner']])
        metrics = provider.metrics.snapshot()
    assert len(places) == 3 and calls == ['POST', 'GET']
    assert metrics['skus']['text_search_ids']['requests'] == 1
    assert metrics['skus']['text_search_ids']['cache_hits'] == 2


@pytest.mark.asyncio
async def test_empty_local_search_falls_back_to_english_without_inventing_official_display_name(place_payload):
    from app.agent.course_state import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    languages = []

    def respond(request):
        if request.method == 'POST':
            body = json.loads(request.content)
            languages.append(body['languageCode'])
            if body['textQuery'] == '서울 미술관':
                return httpx.Response(200, json={'places': []})
        return place_response(request, place_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        place = await VerifiedMapsProvider(client=client, api_key='offline').search(Candidate(name='서울 미술관', english_name='Seoul Museum'), Destination('KR', 37.3, 126.7, 37.8, 127.3))
    assert place.place.placeName == '서울 미술관' and place.place.placeEngName == 'Seoul Museum'
    assert languages == ['ko', 'en']


@pytest.mark.asyncio
async def test_failed_search_is_not_success_cached_and_metrics_count_real_sends(place_payload):
    from app.agent.course_state import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    searches = 0

    def respond(request):
        nonlocal searches
        searches += int(request.method == 'POST')
        if request.method == 'POST' and searches == 1:
            return httpx.Response(503, json={'error': 'temporary failure'})
        return place_response(request, place_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='private-test-key')
        candidate, destination = Candidate(name='서울 미술관'), Destination('KR', 37.3, 126.7, 37.8, 127.3)
        assert (await provider.search(candidate, destination)).place.placeId == place_payload['id']
        assert (await provider.search(candidate, destination)).place.placeId == place_payload['id']
        metrics = provider.metrics.snapshot()
    assert searches == 2
    sku = metrics['skus']['text_search_ids']
    assert (sku['requests'], sku['responses_200'], sku['errors'], sku['cache_hits']) == (2, 1, 1, 1)
    assert sku['estimated_list_cost_usd'] == pytest.approx(2 * sku['unit_price_usd'])
    assert 'private-test-key' not in json.dumps(metrics)


@pytest.mark.asyncio
async def test_cancelled_search_counts_only_sent_attempt_and_can_be_retried(place_payload):
    import asyncio

    from app.agent.course_state import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    started = asyncio.Event()
    search_calls = 0

    async def respond(request):
        nonlocal search_calls
        search_calls += int(request.method == 'POST')
        if request.method == 'POST' and search_calls == 1:
            started.set()
            await asyncio.Event().wait()
        return place_response(request, place_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        candidate, destination = Candidate(name='서울 미술관'), Destination('KR', 37.3, 126.7, 37.8, 127.3)
        task = asyncio.create_task(provider.search(candidate, destination))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await provider.search(candidate, destination)).place.placeId == place_payload['id']
        sku = provider.metrics.snapshot()['skus']['text_search_ids']
    assert search_calls == 2
    assert (sku['requests'], sku['responses_200'], sku['errors'], sku['cache_hits']) == (2, 1, 1, 0)
    assert sku['estimated_list_cost_usd'] == pytest.approx(2 * sku['unit_price_usd'])
    assert sku['successful_list_cost_usd'] == pytest.approx(sku['unit_price_usd'])


@pytest.mark.asyncio
async def test_actual_masks_drive_sku_cost_and_unknown_fields_keep_estimate_incomplete():
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

    from app.agent.course_state import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def respond(request):
        calls.append(request.method)
        started.set()
        await release.wait()
        return place_response(request, place_payload)

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


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_body', [b'not json', b'[]', b'{"error": {"message": "unusable"}}'])
async def test_http_200_invalid_payload_records_response_and_error_without_success_cache(place_payload, invalid_body):
    from app.agent.course_state import Candidate
    from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider

    details = 0

    def respond(request):
        nonlocal details
        details += int(request.method == 'POST')
        return httpx.Response(200, content=invalid_body) if request.method == 'POST' and details == 1 else place_response(request, place_payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        candidate, destination = Candidate(name='서울 미술관'), Destination('KR', 37.3, 126.7, 37.8, 127.3)
        with pytest.raises(ValueError):
            await provider.search(candidate, destination)
        assert (await provider.search(candidate, destination)).place.placeId == place_payload['id']
        sku = provider.metrics.snapshot()['skus']['text_search_ids']
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
    from app.agent.tools.verified_maps import VerifiedMapsProvider

    country = {'longText': '대한민국', 'shortText': 'KR', 'types': ['country']}
    city = {
        'id': 'verified-city', 'displayName': {'text': official}, 'types': ['locality'],
        'addressComponents': [country, {'longText': official, 'types': ['locality']}],
        'viewport': {'low': {'latitude': 37.1, 'longitude': 126.5}, 'high': {'latitude': 37.8, 'longitude': 127.5}},
    }

    def respond(request):
        countries = [{'id': 'verified-country', 'types': ['country'], 'addressComponents': [country]}]
        if request.method == 'POST':
            query = json.loads(request.content)['textQuery']
            return place_response(request, countries if query == '대한민국' else [city])
        return place_response(request, [*countries, city])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        destination = await VerifiedMapsProvider(client=client, api_key='offline').resolve_destination('대한민국', requested)
    assert destination.country_code == 'KR'
    assert destination.contains(37.3, 127.0)


@pytest.mark.asyncio
@pytest.mark.parametrize('requested_country', ['한국', 'South Korea', 'Republic of Korea', 'KR'])
async def test_country_lookup_uses_actual_country_entity_despite_input_name_difference(requested_country):
    from app.agent.tools.verified_maps import VerifiedMapsProvider

    country = {'longText': '대한민국', 'shortText': 'KR', 'types': ['country']}
    city = {
        'id': 'verified-suwon', 'displayName': {'text': '수원시'}, 'types': ['locality'],
        'addressComponents': [country, {'longText': '수원시', 'types': ['locality']}],
        'viewport': {'low': {'latitude': 37.1, 'longitude': 126.5}, 'high': {'latitude': 37.8, 'longitude': 127.5}},
    }

    def respond(request):
        countries = [{'id': 'verified-country', 'types': ['country'], 'addressComponents': [country]}]
        if request.method == 'POST':
            query = json.loads(request.content)['textQuery']
            return place_response(request, countries if query == requested_country else [city])
        return place_response(request, [*countries, city])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        destination = await VerifiedMapsProvider(client=client, api_key='offline').resolve_destination(requested_country, '수원')
    assert destination.country_code == 'KR'


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['district_prefix', 'removed_internal_character', 'wrong_city_country', 'ambiguous_variants', 'ambiguous_country_name', 'inconsistent_country_codes', 'non_korean_suffix', 'district_with_matching_parent'])
async def test_common_country_entity_validation_preserves_ambiguity_and_mismatch_guards(case):
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
        countries = [{'id': 'verified-country', 'types': ['country'], 'addressComponents': [country]}]
        if case == 'ambiguous_country_name':
            countries.append({'id': 'other-country', 'types': ['country'], 'addressComponents': [{'shortText': 'KP', 'types': ['country']}]})
        if request.method == 'POST':
            query = json.loads(request.content)['textQuery']
            return place_response(request, countries if query == requested_country else cities)
        return place_response(request, [*countries, *cities])

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

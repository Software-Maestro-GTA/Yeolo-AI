"""Validate common Places destination facts without language or country aliases."""

import asyncio
import copy
import json

import httpx
import pytest

from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider


def country_record(code='JP', identifier='verified-country', name='공급자 국가명'):
    return {'id': identifier, 'displayName': {'text': name}, 'types': ['country', 'political'],
            'addressComponents': [{'longText': name, 'shortText': code, 'types': ['country']}]}


def city_record(code='JP', identifier='verified-city', name='공급자 도시명', kind='locality'):
    return {'id': identifier, 'displayName': {'text': name}, 'types': [kind, 'political'],
            'addressComponents': [{'longText': name, 'types': [kind]}, {'shortText': code, 'types': ['country']}],
            'viewport': {'low': {'latitude': 34.5, 'longitude': 138.2}, 'high': {'latitude': 36.5, 'longitude': 141.3}}}


def essential_response(request, records):
    """Expose search IDs and masked geographic details as separate responses."""
    assert request.url.host == 'places.googleapis.com'
    if request.method == 'POST':
        assert request.url.path == '/v1/places:searchText'
        assert request.headers['X-Goog-FieldMask'] == 'places.id'
        return httpx.Response(200, json={'places': [{'id': item.get('id')} for item in records]})
    assert request.method == 'GET' and request.url.path.startswith('/v1/places/')
    fields = set(request.headers['X-Goog-FieldMask'].split(','))
    assert fields == {'id', 'addressComponents', 'types', 'viewport'}
    identifier = request.url.path.rsplit('/', 1)[-1]
    record = next(item for item in records if item.get('id') == identifier)
    return httpx.Response(200, json={key: value for key, value in record.items() if key in fields})


def responder(countries, cities, queries=None):
    def respond(request):
        if request.method == 'POST':
            body = json.loads(request.content)
            assert body['pageSize'] == 5
            query = body['textQuery']
            if queries is not None:
                queries.append(query)
            return essential_response(request, cities if ', ' in query else countries)
        return essential_response(request, [*countries, *cities])
    return httpx.MockTransport(respond)


@pytest.mark.asyncio
@pytest.mark.parametrize(('country', 'city', 'code', 'official', 'kind'), [
    ('일본', '도쿄', 'JP', '도쿄도', 'administrative_area_level_1'),
    ('대한민국', '수원', 'KR', '수원시', 'locality'),
    ('한국', '서울', 'KR', '서울특별시', 'administrative_area_level_1'),
    ('Japan', 'Tokyo', 'JP', '도쿄도', 'administrative_area_level_1'),
    ('日本', '東京', 'JP', '도쿄도', 'administrative_area_level_1'),
    ('프랑스', '파리', 'FR', 'Paris', 'locality'),
    ('미국', '뉴욕', 'US', 'New York', 'locality'),
    ('영국', '런던', 'GB', 'London', 'postal_town'),
    ('중국', '베이징', 'CN', '北京市', 'administrative_area_level_1'),
    ('대만', '타이베이', 'TW', '臺北市', 'locality'),
    ('아랍에미리트', '두바이', 'AE', 'Dubai', 'locality'),
])
async def test_different_official_names_use_same_verified_geographic_contract(country, city, code, official, kind):
    queries = []
    async with httpx.AsyncClient(transport=responder([country_record(code)], [city_record(code, name=official, kind=kind)], queries)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        result = await provider.resolve_destination(country, city)
        metrics = provider.metrics.snapshot()
    assert result.country_code == code
    assert result.place_id == 'verified-city'
    assert queries == [country, f'{city}, {country}']
    assert metrics['skus']['text_search_ids']['requests'] == 2
    assert metrics['skus']['place_details_essentials']['requests'] == 2
    assert metrics['estimate_complete']


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['parent_only', 'missing_id', 'malformed_id', 'lower_code', 'long_code', 'missing_code', 'multiple_codes', 'no_country_component'])
async def test_country_requires_own_type_id_and_one_uppercase_iso_code(invalid):
    country = country_record()
    if invalid == 'parent_only':
        country['types'] = ['store', 'establishment']
    elif invalid == 'missing_id':
        country.pop('id')
    elif invalid == 'malformed_id':
        country['id'] = 'bad/id'
    elif invalid in {'lower_code', 'long_code', 'missing_code'}:
        country['addressComponents'][0]['shortText'] = {'lower_code': 'jp', 'long_code': 'JPN', 'missing_code': ''}[invalid]
    elif invalid == 'multiple_codes':
        country['addressComponents'].append({'shortText': 'KR', 'types': ['country']})
    else:
        country['addressComponents'] = []
    async with httpx.AsyncClient(transport=responder([country], [city_record()])) as client:
        with pytest.raises(ValueError):
            await VerifiedMapsProvider(client=client, api_key='offline').resolve_destination('공급자 국가명', '공급자 도시명')


@pytest.mark.asyncio
@pytest.mark.parametrize('same_id', [True, False])
async def test_country_entity_is_ambiguous_only_for_distinct_valid_ids(same_id):
    first = country_record(name='Japan')
    second = country_record(identifier=first['id'] if same_id else 'another-country', name='다른 국가명')
    async with httpx.AsyncClient(transport=responder([first, second], [city_record(name='Tokyo')])) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        if same_id:
            assert (await provider.resolve_destination('Japan', 'Tokyo')).country_code == 'JP'
        else:
            with pytest.raises(ValueError):
                await provider.resolve_destination('Japan', 'Tokyo')


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['parent_only', 'missing_id', 'malformed_id', 'wrong_country', 'multiple_country_codes', 'missing_country'])
async def test_city_requires_own_geographic_type_identity_and_consistent_country(invalid):
    city = city_record(name='Tokyo')
    if invalid == 'parent_only':
        city['types'] = ['store', 'establishment']
    elif invalid == 'missing_id':
        city.pop('id')
    elif invalid == 'malformed_id':
        city['id'] = 'bad/id'
    elif invalid == 'wrong_country':
        city['addressComponents'][-1]['shortText'] = 'KR'
    elif invalid == 'multiple_country_codes':
        city['addressComponents'].append({'shortText': 'KR', 'types': ['country']})
    else:
        city['addressComponents'] = city['addressComponents'][:1]
    async with httpx.AsyncClient(transport=responder([country_record(name='Japan')], [city])) as client:
        with pytest.raises(ValueError):
            await VerifiedMapsProvider(client=client, api_key='offline').resolve_destination('Japan', 'Tokyo')


@pytest.mark.asyncio
@pytest.mark.parametrize(('same_id', 'same_bounds'), [(True, True), (False, True), (True, False), (False, False)])
async def test_distinct_geographic_ids_remain_ambiguous_without_name_filter(same_id, same_bounds):
    first = city_record(name='Tokyo')
    second = city_record(identifier=first['id'] if same_id else 'another-region', name='完全に異なる地名')
    if not same_bounds:
        second['viewport']['low']['latitude'] = 35.0
    async with httpx.AsyncClient(transport=responder([country_record(name='Japan')], [first, second])) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        if same_id:
            assert (await provider.resolve_destination('Japan', 'Tokyo')).place_id == first['id']
        else:
            with pytest.raises(ValueError):
                await provider.resolve_destination('Japan', 'Tokyo')


@pytest.mark.asyncio
@pytest.mark.parametrize('value', [True, False, '35.0', None, float('nan'), float('inf'), -91.0, 91.0])
async def test_destination_latitude_must_be_finite_real_numeric_coordinate(value):
    city = city_record(name='Tokyo')
    city['viewport']['low']['latitude'] = value
    # JSON itself disallows NaN/Infinity; use a response JSON override to model an
    # invalid provider decoder value without inventing a successful coordinate.
    def respond(request):
        records = [country_record(name='Japan'), city]
        if request.method == 'POST':
            body = json.loads(request.content)
            return essential_response(request, [city] if ', ' in body['textQuery'] else [records[0]])
        identifier = request.url.path.rsplit('/', 1)[-1]
        record = next(item for item in records if item['id'] == identifier)
        response = httpx.Response(200, json={})
        response.json = lambda: {key: value for key, value in record.items() if key in {'id', 'addressComponents', 'types', 'viewport'}}
        return response
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError):
            await VerifiedMapsProvider(client=client, api_key='offline').resolve_destination('Japan', 'Tokyo')


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['missing_viewport', 'latitude_order', 'longitude_range', 'missing_coordinate'])
async def test_destination_requires_complete_and_ordered_viewport(invalid):
    """Apply the same bounds contract to every city, including Tokyo."""
    city = city_record()
    if invalid == 'missing_viewport':
        city.pop('viewport')
    elif invalid == 'latitude_order':
        city['viewport']['low']['latitude'] = 40.0
    elif invalid == 'longitude_range':
        city['viewport']['high']['longitude'] = 190.0
    else:
        city['viewport']['low'].pop('latitude')
    async with httpx.AsyncClient(transport=responder([country_record()], [city])) as client:
        with pytest.raises(ValueError):
            await VerifiedMapsProvider(client=client, api_key='offline').resolve_destination('Japan', 'Tokyo')


@pytest.mark.asyncio
async def test_valid_candidate_is_selected_only_after_full_geographic_validation():
    good, invalid = city_record(name='Untranslated official city'), city_record(identifier='invalid-region', name='Tokyo')
    invalid.pop('viewport')
    async with httpx.AsyncClient(transport=responder([country_record(name='Japan')], [invalid, good])) as client:
        result = await VerifiedMapsProvider(client=client, api_key='offline').resolve_destination('Japan', 'Tokyo')
    assert result.place_id == good['id']


@pytest.mark.asyncio
@pytest.mark.parametrize(('country', 'city'), [('Singapore', 'Ｓｉｎｇａｐｏｒｅ'), (' 싱가포르 ', '싱가포르'), ('SINGAPORE', 'singapore')])
async def test_same_country_and_city_entity_is_allowed_by_input_and_identity(country, city):
    record = country_record('SG', identifier='same-geographic-entity')
    record['viewport'] = city_record()['viewport']
    async with httpx.AsyncClient(transport=responder([record], [record])) as client:
        result = await VerifiedMapsProvider(client=client, api_key='offline').resolve_destination(country, city)
    assert result.country_code == 'SG'
    assert result.place_id == record['id']


@pytest.mark.asyncio
@pytest.mark.parametrize(('country', 'city', 'same_id'), [('United States', 'New York', True), ('Singapore', 'Singapore', False), ('New Zealand', 'NewZealand', True), ('New  Zealand', 'New Zealand', True)])
async def test_country_only_city_response_cannot_bypass_distinct_input_or_identity(country, city, same_id):
    first = country_record('SG', identifier='country-entity', name=country)
    second = copy.deepcopy(first)
    second['viewport'] = city_record()['viewport']
    if not same_id:
        second['id'] = 'other-entity'
    async with httpx.AsyncClient(transport=responder([first], [second])) as client:
        with pytest.raises(ValueError):
            await VerifiedMapsProvider(client=client, api_key='offline').resolve_destination(country, city)


@pytest.mark.asyncio
async def test_common_unicode_cleanup_applies_to_queries_without_changing_inputs():
    country, city = '　Ｕｎｉｔｅｄ\u00a0　Ｓｔａｔｅｓ　', '　Ｎｅｗ\t　Ｙｏｒｋ　'
    original = (country, city)
    queries = []
    async with httpx.AsyncClient(transport=responder([country_record('US')], [city_record('US')], queries)) as client:
        result = await VerifiedMapsProvider(client=client, api_key='offline').resolve_destination(country, city)
    assert queries == ['United States', 'New York, United States']
    assert (country, city) == original
    assert result.country_code == 'US'


@pytest.mark.asyncio
@pytest.mark.parametrize(('country', 'city'), [('', 'Tokyo'), ('Japan', ''), ('\u3000\t', 'Tokyo'), ('Japan', '\u00a0\n')])
async def test_blank_destination_inputs_are_rejected_before_http(country, city):
    calls = []
    async with httpx.AsyncClient(transport=responder([], [], calls)) as client:
        with pytest.raises(ValueError):
            await VerifiedMapsProvider(client=client, api_key='offline').resolve_destination(country, city)
    assert calls == []


@pytest.mark.asyncio
async def test_destination_queries_remain_parallel_and_cross_dateline_bounds_are_preserved():
    calls = []
    both_started = asyncio.Event()
    city = city_record(name='Official Pacific city')
    city['viewport'] = {'low': {'latitude': -20.0, 'longitude': 170.0}, 'high': {'latitude': 20.0, 'longitude': -170.0}}
    async def respond(request):
        records = [country_record(), city]
        if request.method == 'POST':
            query = json.loads(request.content)['textQuery']
            calls.append(query)
            if len(calls) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 1)
            return essential_response(request, [city] if ', ' in query else [records[0]])
        return essential_response(request, records)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await VerifiedMapsProvider(client=client, api_key='offline').resolve_destination('Japan', 'Pacific destination')
    assert len(calls) == 2
    assert result.contains(0, 175) and result.contains(0, -175)
    assert not result.contains(0, 0)


def test_destination_internal_identity_is_optional_and_positional_construction_remains_compatible():
    result = Destination('JP', 34.0, 138.0, 36.0, 141.0)
    assert result.place_id == ''
    assert result.verified_country_name == result.verified_city_name == ''


@pytest.mark.asyncio
async def test_cancellation_stops_both_destination_queries_without_retry():
    started = asyncio.Event()
    sends = 0

    async def respond(request):
        nonlocal sends
        sends += 1
        if sends == 2:
            started.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        task = asyncio.create_task(provider.resolve_destination('Japan', 'Tokyo'))
        try:
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert sends == 2
    assert not provider.cache


@pytest.mark.asyncio
async def test_destination_permission_failure_is_sanitized_and_not_retried():
    from app.agent.tools.verified_maps import MapsProviderError

    sends = []

    def respond(request):
        sends.append(request)
        return httpx.Response(403, json={'error': {'message': 'private provider detail'}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(MapsProviderError) as error:
            await VerifiedMapsProvider(client=client, api_key='private-test-key').resolve_destination('Japan', 'Tokyo')
    assert len(sends) == 2  # One country request and one city request; no retry.
    assert error.value.kind == 'unauthorized' and error.value.status_code == 403
    assert 'private-test-key' not in str(error.value)
    assert 'private provider detail' not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['ko', 'en', 'ja', 'zh-TW'])
async def test_country_city_queries_use_korean_and_reuse_cross_provider_component_names(language):
    searches, details = [], []

    def respond(request):
        records = [country_record(name='일본'), city_record(name='Tokyo', kind='administrative_area_level_1')]
        if request.method == 'POST':
            body = json.loads(request.content)
            searches.append(body)
            assert body['languageCode'] == 'ko'
            return essential_response(request, [records[1]] if ', ' in body['textQuery'] else [records[0]])
        details.append(request)
        assert request.url.params['languageCode'] == ('ko' if request.url.path.endswith('/verified-country') else 'en')
        return essential_response(request, records)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline', language=language)
        result = await provider.resolve_destination('Japan', 'Tokyo')
        metrics = provider.metrics.snapshot()
    assert result.country_code == 'JP' and result.place_id == 'verified-city'
    assert result.verified_country_name == '일본' and result.verified_city_name == 'Tokyo'
    assert len(searches) == len(details) == 2
    assert set(metrics['skus']) == {'text_search_ids', 'place_details_essentials'}
    assert metrics['skus']['place_details_essentials']['requests'] == 2


@pytest.mark.asyncio
async def test_same_country_city_entity_reuses_one_korean_detail_lookup():
    record = country_record('SG', identifier='same-geographic-entity', name='싱가포르')
    record['viewport'] = city_record()['viewport']

    def respond(request):
        localized = copy.deepcopy(record)
        if request.method == 'GET' and request.url.params['languageCode'] == 'en':
            localized['addressComponents'][0]['longText'] = 'Singapore'
        return essential_response(request, [localized])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        result = await provider.resolve_destination('Singapore', 'Singapore')
        metrics = provider.metrics.snapshot()
    assert result.verified_country_name == result.verified_city_name == '싱가포르'
    assert metrics['skus']['place_details_essentials']['requests'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(('entity', 'status'), [('country', 404), ('city', 410)])
async def test_deleted_geographic_search_hit_does_not_discard_valid_destination(entity, status):
    country, city = country_record(), city_record()
    calls = []

    def respond(request):
        calls.append(request)
        if request.method == 'POST':
            record = city if ', ' in json.loads(request.content)['textQuery'] else country
            deleted = [{'id': 'deleted-entity'}] if (record is city) == (entity == 'city') else []
            return httpx.Response(200, json={'places': [*deleted, {'id': record['id']}]})
        if request.url.path.endswith('/deleted-entity'):
            return httpx.Response(status, json={'error': {}})
        return essential_response(request, [country, city])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        result = await provider.resolve_destination('Japan', 'Tokyo')
        metrics = provider.metrics.snapshot()
    assert result.country_code == 'JP' and result.place_id == city['id']
    assert len(calls) == 5
    assert set(metrics['skus']) == {'text_search_ids', 'place_details_essentials'}
    assert metrics['skus']['place_details_essentials']['errors'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(('failure', 'kind'), [(503, 'transient'), (403, 'unauthorized'), (400, 'invalid'), ('wrong_id', 'place_data'), ('malformed_json', 'place_data')])
async def test_incomplete_geographic_details_cannot_hide_destination_ambiguity(failure, kind):
    from app.agent.tools.verified_maps import MapsProviderError

    country, city = country_record(), city_record()

    def respond(request):
        if request.method == 'POST':
            city_query = ', ' in json.loads(request.content)['textQuery']
            return httpx.Response(200, json={'places': [{'id': city['id']}, {'id': 'uncertain-city'}] if city_query else [{'id': country['id']}]})
        if request.url.path.endswith('/uncertain-city'):
            if isinstance(failure, int):
                return httpx.Response(failure, json={'error': {}})
            if failure == 'malformed_json':
                return httpx.Response(200, content=b'not json')
            return httpx.Response(200, json={'id': 'a-different-city'})
        return essential_response(request, [country, city])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(MapsProviderError) as error:
            await VerifiedMapsProvider(client=client, api_key='offline').resolve_destination('Japan', 'Tokyo')
    assert error.value.kind == kind


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['missing_city', 'ambiguous_city', 'parent_name', 'missing_country', 'same_country_city'])
async def test_only_unambiguous_existing_entity_components_become_internal_names(case):
    country = country_record(name='일본')
    city = city_record(name='도쿄도', kind='administrative_area_level_1')
    expected_country, expected_city = '일본', '도쿄도'
    if case == 'missing_city':
        city['addressComponents'][0].pop('longText')
        expected_city = ''
    elif case == 'ambiguous_city':
        city['addressComponents'].append({'longText': '다른 지역', 'types': ['administrative_area_level_1']})
        expected_city = ''
    elif case == 'parent_name':
        city['types'] = ['locality', 'political']
        expected_city = ''
    elif case == 'missing_country':
        country['addressComponents'][0].pop('longText')
        expected_country = ''
    else:
        country['viewport'] = city['viewport']
        city = country
        expected_city = '일본'
    async with httpx.AsyncClient(transport=responder([country], [city])) as client:
        result = await VerifiedMapsProvider(client=client, api_key='offline').resolve_destination('Japan', 'Japan' if case == 'same_country_city' else 'Tokyo')
    assert result.verified_country_name == expected_country
    assert result.verified_city_name == expected_city

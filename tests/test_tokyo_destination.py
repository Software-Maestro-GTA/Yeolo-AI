"""Reproduce Tokyo's actual administrative name while preserving strict identity."""

import copy
import json
from itertools import product

import httpx
import pytest

TOKYO_NAME_FORMS = ['도쿄', '도쿄도', '東京', '東京都', 'Tokyo', 'Tokyo Metropolis']


@pytest.fixture
def tokyo_payload():
    """Project only provider facts needed for destination verification."""
    country = {'longText': '일본', 'shortText': 'JP', 'types': ['country']}
    return {
        'id': 'ChIJ51cu8IcbXWARiRtXIothAS4',
        'displayName': {'text': '도쿄도'},
        'types': ['administrative_area_level_1', 'political'],
        'addressComponents': [
            {'longText': '도쿄도', 'shortText': '도쿄도', 'types': ['administrative_area_level_1', 'political']},
            country,
        ],
        'viewport': {
            'low': {'latitude': 34.5776326, 'longitude': 138.2991098},
            'high': {'latitude': 36.4408483, 'longitude': 141.2405144},
        },
    }


def destination_transport(cities, country='일본', country_code='JP'):
    """Return geocoding facts via HTTP without live Maps calls."""
    def respond(request):
        assert request.url.host == 'places.googleapis.com'
        query = json.loads(request.content)['textQuery']
        if query == country:
            payload = [{'id': 'verified-country', 'types': ['country'], 'addressComponents': [{'longText': country, 'shortText': country_code, 'types': ['country']}]}]
        else:
            payload = cities
        return httpx.Response(200, json={'places': payload})

    return httpx.MockTransport(respond)


@pytest.mark.asyncio
async def test_actual_tokyo_metropolis_response_verifies_requested_tokyo(tokyo_payload):
    from app.agent.tools.verified_maps import VerifiedMapsProvider

    async with httpx.AsyncClient(transport=destination_transport([tokyo_payload])) as client:
        result = await VerifiedMapsProvider(client=client, api_key='offline').resolve_destination('일본', '도쿄')
    assert result.country_code == 'JP'
    assert (result.south, result.west, result.north, result.east) == (34.5776326, 138.2991098, 36.4408483, 141.2405144)
    assert result.contains(35.6812, 139.7671)


@pytest.mark.asyncio
@pytest.mark.parametrize(('requested', 'official'), list(product(TOKYO_NAME_FORMS, repeat=2)))
async def test_tokyo_provider_name_forms_preserve_existing_success_cases(tokyo_payload, requested, official):
    from app.agent.tools.verified_maps import VerifiedMapsProvider

    tokyo_payload['displayName']['text'] = official
    tokyo_payload['addressComponents'][0]['longText'] = official
    async with httpx.AsyncClient(transport=destination_transport([tokyo_payload])) as client:
        result = await VerifiedMapsProvider(client=client, api_key='offline').resolve_destination('일본', requested)
    assert result.country_code == 'JP'


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['ward_parent', 'wrong_country', 'shop_parent', 'district_type', 'unlisted_alias', 'generic_suffix', 'non_japan_alias'])
async def test_tokyo_common_entities_still_reject_shops_and_wrong_country(tokyo_payload, case):
    from app.agent.tools.verified_maps import VerifiedMapsProvider

    country, country_code, requested = '일본', 'JP', '도쿄'
    if case == 'ward_parent':
        requested = '東京都'
        tokyo_payload['addressComponents'][0]['longText'] = '東京都'
        tokyo_payload['displayName']['text'] = '台東区'
        tokyo_payload['types'] = ['administrative_area_level_2', 'political']
        tokyo_payload['addressComponents'].insert(0, {'longText': '台東区', 'types': ['administrative_area_level_2']})
    elif case == 'wrong_country':
        tokyo_payload['addressComponents'][-1]['shortText'] = 'KR'
    elif case == 'shop_parent':
        tokyo_payload['displayName']['text'] = 'Tokyo'
        tokyo_payload['types'] = ['store', 'point_of_interest', 'establishment']
    elif case == 'district_type':
        requested = '도쿄도'
        tokyo_payload['types'] = ['administrative_area_level_2', 'political']
    elif case == 'unlisted_alias':
        tokyo_payload['displayName']['text'] = 'Tokyo Bay'
        tokyo_payload['addressComponents'][0]['longText'] = 'Tokyo Bay'
    elif case == 'generic_suffix':
        requested = '大阪'
        tokyo_payload['displayName']['text'] = '大阪府'
        tokyo_payload['addressComponents'][0]['longText'] = '大阪府'
    else:
        country, country_code = '대한민국', 'KR'
        tokyo_payload['addressComponents'][-1] = {'longText': country, 'shortText': country_code, 'types': ['country']}
    async with httpx.AsyncClient(transport=destination_transport([tokyo_payload], country, country_code)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        # A sole own admin2 region is now interpreted by the same geographic
        # contract everywhere; a parent name never grants a shop a geographic type.
        if case in {'wrong_country', 'shop_parent'}:
            with pytest.raises(ValueError):
                await provider.resolve_destination(country, requested)
        else:
            result = await provider.resolve_destination(country, requested)
            assert result.country_code == country_code
            assert result.place_id == tokyo_payload['id']


@pytest.mark.asyncio
@pytest.mark.parametrize(('same_id', 'same_bounds'), [(True, True), (False, True), (True, False), (False, False)])
async def test_tokyo_multiple_matching_records_remain_ambiguous(tokyo_payload, same_id, same_bounds):
    from app.agent.tools.verified_maps import VerifiedMapsProvider

    other = copy.deepcopy(tokyo_payload)
    other['displayName']['text'] = 'Tokyo Metropolis'
    other['addressComponents'][0]['longText'] = 'Tokyo Metropolis'
    if not same_id:
        other['id'] = 'distinct-tokyo-region'
    if not same_bounds:
        other['viewport']['low']['latitude'] = 35.0
    async with httpx.AsyncClient(transport=destination_transport([tokyo_payload, other])) as client:
        with pytest.raises(ValueError):
            await VerifiedMapsProvider(client=client, api_key='offline').resolve_destination('일본', '도쿄')


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['missing', 'latitude_order', 'latitude_range', 'longitude_range', 'missing_coordinate'])
async def test_tokyo_entity_does_not_bypass_viewport_validation(tokyo_payload, invalid):
    from app.agent.tools.verified_maps import VerifiedMapsProvider

    if invalid == 'missing':
        tokyo_payload.pop('viewport')
    elif invalid == 'latitude_order':
        tokyo_payload['viewport']['low']['latitude'] = 40.0
    elif invalid == 'latitude_range':
        tokyo_payload['viewport']['high']['latitude'] = 100.0
    elif invalid == 'longitude_range':
        tokyo_payload['viewport']['high']['longitude'] = 190.0
    else:
        tokyo_payload['viewport']['low'].pop('latitude')
    async with httpx.AsyncClient(transport=destination_transport([tokyo_payload])) as client:
        with pytest.raises(ValueError):
            await VerifiedMapsProvider(client=client, api_key='offline').resolve_destination('일본', '도쿄')


@pytest.mark.asyncio
@pytest.mark.parametrize(('city', 'kind'), [('大阪市', 'administrative_area_level_1'), ('新宿区', 'locality')])
async def test_other_explicit_japanese_destinations_remain_valid_by_own_name(tokyo_payload, city, kind):
    from app.agent.tools.verified_maps import VerifiedMapsProvider

    tokyo_payload['displayName']['text'] = city
    tokyo_payload['types'] = [kind, 'political']
    tokyo_payload['addressComponents'][0]['longText'] = city
    tokyo_payload['addressComponents'][0]['types'] = [kind]
    async with httpx.AsyncClient(transport=destination_transport([tokyo_payload])) as client:
        result = await VerifiedMapsProvider(client=client, api_key='offline').resolve_destination('일본', city)
    assert result.country_code == 'JP'

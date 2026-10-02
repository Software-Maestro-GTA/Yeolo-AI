"""Verify real place photo media, attribution, optional failures and no-cache policy."""

import asyncio
import copy
import json

import httpx
import pytest

from app.agent.tools.verified_maps import VerifiedMapsProvider

IMAGE_URL = 'https://lh3.googleusercontent.com/p/actual-place-image=s1200'
AVATAR_URL = 'https://lh3.googleusercontent.com/a/author-avatar=s100'
SOURCE_URL = 'https://www.google.com/maps/place/?cid=123'
PROFILE_URL = 'https://www.google.com/maps/contrib/123'


@pytest.fixture
def photo_metadata():
    return {'id': 'verified-place', 'photos': [{
        'name': 'places/verified-place/photos/current-resource',
        'googleMapsUri': SOURCE_URL,
        'authorAttributions': [{'displayName': '실제 작성자', 'uri': PROFILE_URL, 'photoUri': AVATAR_URL}],
    }]}


@pytest.mark.asyncio
async def test_actual_media_uri_and_all_available_author_fields_are_returned_without_key_or_cache(photo_metadata):
    sends = []

    def respond(request):
        sends.append(request)
        assert request.headers['X-Goog-Api-Key'] == 'private-test-key'
        assert 'key' not in request.url.params
        if request.url.path == '/v1/places/verified-place':
            assert request.headers['X-Goog-FieldMask'] == 'id,photos'
            return httpx.Response(200, json=photo_metadata)
        assert request.url.path == '/v1/places/verified-place/photos/current-resource/media'
        assert request.url.params['skipHttpRedirect'].lower() == 'true'
        assert request.url.params['maxWidthPx'] == '1200'
        return httpx.Response(200, json={'name': 'places/verified-place/photos/current-resource/media', 'photoUri': IMAGE_URL})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='private-test-key')
        first = await provider.photo('places/verified-place')
        second = await provider.photo('verified-place')
        metrics = provider.metrics.snapshot()
    assert first.url == second.url == IMAGE_URL
    assert first.url != AVATAR_URL
    for value in ('사진 출처: Google Maps', '실제 작성자', SOURCE_URL, PROFILE_URL, AVATAR_URL):
        assert value in first.credit
    assert 'private-test-key' not in first.url + first.credit + json.dumps(metrics)
    assert len(sends) == 4  # Fetch fresh metadata/media each time; photo names expire.
    assert not provider.cache
    assert not provider.locks
    assert metrics['estimate_complete']
    assert metrics['estimated_list_cost_usd'] == pytest.approx(.014)
    assert sum(bucket['requests'] for bucket in metrics['skus'].values()) == 4
    assert sorted(bucket['unit_price_usd'] for bucket in metrics['skus'].values()) == [0, .007]


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['no_photos', 'wrong_id', 'wrong_photo_place', 'nested_resource', 'missing_source', 'unsafe_source', 'unsafe_author', 'key_author', 'key_author_name', 'missing_author_name', 'avatar_only', 'wrong_media_name', 'unsafe_image', 'userinfo_image', 'key_image', 'control_image', 'endpoint_image', 'broken_json', 'permission', 'timeout'])
async def test_photo_failure_returns_none_without_exposing_or_replacing_image(photo_metadata, failure, caplog):
    metadata = copy.deepcopy(photo_metadata)
    media = {'name': 'places/verified-place/photos/current-resource/media', 'photoUri': IMAGE_URL}
    if failure == 'no_photos':
        metadata['photos'] = []
    elif failure == 'wrong_id':
        metadata['id'] = 'another-place'
    elif failure == 'wrong_photo_place':
        metadata['photos'][0]['name'] = 'places/another-place/photos/current-resource'
    elif failure == 'nested_resource':
        metadata['photos'][0]['name'] += '/extra'
    elif failure == 'missing_source':
        metadata['photos'][0].pop('googleMapsUri')
    elif failure == 'unsafe_source':
        metadata['photos'][0]['googleMapsUri'] = 'https://evil.test/source'
    elif failure == 'unsafe_author':
        metadata['photos'][0]['authorAttributions'][0]['uri'] = 'javascript:alert(1)'
    elif failure == 'key_author':
        metadata['photos'][0]['authorAttributions'][0]['photoUri'] += '?key=private-test-key'
    elif failure == 'key_author_name':
        metadata['photos'][0]['authorAttributions'][0]['displayName'] += ' private-test-key'
    elif failure == 'missing_author_name':
        metadata['photos'][0]['authorAttributions'][0].pop('displayName')
    elif failure == 'avatar_only':
        media.pop('photoUri')
    elif failure == 'wrong_media_name':
        media['name'] = 'places/another-place/photos/current-resource/media'
    elif failure == 'unsafe_image':
        media['photoUri'] = 'https://evil.test/image.jpg'
    elif failure == 'userinfo_image':
        media['photoUri'] = 'https://user:password@lh3.googleusercontent.com/image.jpg'
    elif failure == 'key_image':
        media['photoUri'] = IMAGE_URL + '?key=private-test-key'
    elif failure == 'control_image':
        media['photoUri'] = IMAGE_URL + '\n'
    elif failure == 'endpoint_image':
        media['photoUri'] = 'https://places.googleapis.com/v1/places/verified-place/photos/current-resource/media'

    def respond(request):
        if failure == 'permission':
            return httpx.Response(403, json={'error': {'message': 'private raw detail'}})
        if failure == 'timeout':
            raise httpx.ReadTimeout('private raw timeout', request=request)
        if failure == 'broken_json':
            return httpx.Response(200, content=b'not JSON')
        return httpx.Response(200, json=media if request.url.path.endswith('/media') else metadata)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='private-test-key')
        assert await provider.photo('verified-place') is None
    assert not provider.cache
    assert not provider.locks
    assert 'private-test-key' not in caplog.text
    assert 'private raw' not in caplog.text


@pytest.mark.asyncio
async def test_photo_source_protocol_relative_links_are_normalized_and_credit_is_plaintext(photo_metadata):
    author = photo_metadata['photos'][0]['authorAttributions'][0]
    author['uri'] = '//www.google.com/maps/contrib/123'
    author['displayName'] = '<script>作成者</script>'
    def respond(request):
        return httpx.Response(200, json={'photoUri': IMAGE_URL} if request.url.path.endswith('/media') else photo_metadata)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        photo = await VerifiedMapsProvider(client=client, api_key='offline').photo('verified-place')
    assert photo.url == IMAGE_URL
    assert PROFILE_URL in photo.credit
    assert '<script>' not in photo.credit
    assert '作成者' in photo.credit


@pytest.mark.asyncio
async def test_external_photo_cancellation_is_not_hidden_as_missing_image():
    entered = asyncio.Event()
    sends = 0
    async def respond(request):
        nonlocal sends
        sends += 1
        entered.set()
        await asyncio.Event().wait()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = VerifiedMapsProvider(client=client, api_key='offline')
        task = asyncio.create_task(provider.photo('verified-place'))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert sends == 1
    assert not provider.cache and not provider.locks


def test_photo_sku_classification_uses_supported_endpoint_masks_and_parameters_only():
    from app.services.maps_cost import UNIT_PRICES_USD, classify_maps_sku

    details = {'headers': {'X-Goog-FieldMask': 'id,photos'}}
    media = {'params': {'skipHttpRedirect': 'true', 'maxWidthPx': 1200}}
    details_sku = classify_maps_sku('GET', 'https://places.googleapis.com/v1/places/verified-place', details)
    media_sku = classify_maps_sku('GET', 'https://places.googleapis.com/v1/places/verified-place/photos/resource/media', media)
    assert UNIT_PRICES_USD[details_sku] == 0
    assert UNIT_PRICES_USD[media_sku] == .007
    for method, url, options in [
        ('POST', 'https://places.googleapis.com/v1/places/verified-place/photos/resource/media', media),
        ('GET', 'https://evil.test/v1/places/verified-place/photos/resource/media', media),
        ('GET', 'https://places.googleapis.com/v1/places/verified-place', {'headers': {'X-Goog-FieldMask': 'id,photos,rating'}}),
        ('GET', 'https://places.googleapis.com/v1/places/verified-place/photos/resource/extra/media', media),
    ]:
        assert classify_maps_sku(method, url, options) == 'unknown'

"""Guard the IDs-only/Essentials billing boundary and actual-send cost counters."""

import pytest

from app.services.maps_cost import MapsCostMetrics, classify_maps_sku

DETAIL_URL = 'https://places.googleapis.com/v1/places/ChIJ_verified-Place_123'
SEARCH_URL = 'https://places.googleapis.com/v1/places:searchText'


@pytest.mark.parametrize('mask,expected,price', [
    ('id', 'place_details_ids', 0.0),
    ('id,name,attributions,movedPlaceId', 'place_details_ids', 0.0),
    ('photos', 'place_details_ids', 0.0),  # Metadata field, not a photo/media download.
    ('id,formattedAddress,addressComponents,location,types', 'place_details_essentials', .005),
    ('id,addressComponents,types,viewport', 'place_details_essentials', .005),
    ('id,displayName,location', 'place_details_pro', .017),
    ('id,businessStatus,location', 'place_details_pro', .017),
    ('id,primaryType,types', 'place_details_pro', .017),
    ('id,businessStatus,rating', 'place_details_enterprise', .020),
    ('id,regularOpeningHours', 'place_details_enterprise', .020),
    ('id,status', 'unknown', None),
    ('id,unknownField', 'unknown', None),
    ('*', 'unknown', None),
    ('id, location', 'unknown', None),
])
def test_details_field_mask_uses_highest_known_sku_and_unknown_masks_never_assume_five_dollars(mask, expected, price):
    sku = classify_maps_sku('GET', DETAIL_URL, {'headers': {'x-gOoG-FIELDmASK': mask}})
    assert sku == expected
    metrics = MapsCostMetrics()
    metrics.record_request(sku)
    snapshot = metrics.snapshot()
    assert snapshot['skus'][sku]['unit_price_usd'] == price
    assert snapshot['estimate_complete'] is (price is not None)


@pytest.mark.parametrize('method,url', [
    ('POST', DETAIL_URL),
    ('GET', 'https://other.googleapis.com/v1/places/ChIJ_verified'),
    ('GET', 'https://places.googleapis.com.evil.example/v1/places/ChIJ_verified'),
    ('GET', 'https://places.googleapis.com/v1/places/'),
    ('GET', 'https://places.googleapis.com/v1/places/ChIJ_verified/photos/photoid/media'),
    ('GET', 'https://places.googleapis.com/v1/places/ChIJ_verified/photos/photoid'),
    ('GET', 'https://places.googleapis.com/v1/places/places/ChIJ_verified'),
    ('GET', 'https://places.googleapis.com/v1/places/bad%2Fplace'),
])
def test_non_detail_endpoints_never_get_an_essentials_price(method, url):
    assert classify_maps_sku(method, url, {'headers': {'X-Goog-FieldMask': 'id,location'}}) == 'unknown'


@pytest.mark.parametrize('options,url', [
    ({'headers': {}}, DETAIL_URL),
    ({'headers': {'X-Goog-FieldMask': None}}, DETAIL_URL),
    ({'headers': {'X-Goog-FieldMask': 'id,location', 'x-goog-fieldmask': 'id,displayName'}}, DETAIL_URL),
    ({'headers': {'X-Goog-FieldMask': 'id,location'}, 'params': {'fields': 'id,displayName'}}, DETAIL_URL),
    ({'headers': {'X-Goog-FieldMask': 'id,location'}, 'params': [('fields', 'id,displayName')]}, DETAIL_URL),
    ({'headers': {'X-Goog-FieldMask': 'id,location'}}, DETAIL_URL + '?fields=id,displayName'),
])
def test_missing_or_conflicting_mask_contract_stays_unknown(options, url):
    assert classify_maps_sku('GET', url, options) == 'unknown'


def test_text_search_id_lookup_is_free_without_repricing_pro_search():
    assert classify_maps_sku('POST', SEARCH_URL, {'headers': {'x-goog-fieldmask': 'places.id'}}) == 'text_search_ids'
    assert classify_maps_sku('POST', SEARCH_URL, {'headers': {'X-Goog-FieldMask': 'places.id,places.displayName,places.location'}}) == 'text_search_pro'


def test_metrics_count_actual_sends_cache_hits_and_unpriced_attempts_separately():
    metrics = MapsCostMetrics()
    ids = classify_maps_sku('POST', SEARCH_URL, {'headers': {'X-Goog-FieldMask': 'places.id'}})
    detail = classify_maps_sku('GET', DETAIL_URL, {'headers': {'X-Goog-FieldMask': 'id,location'}})
    for sku in (ids, detail):
        metrics.record_call(sku)
        metrics.record_request(sku)
        metrics.record_response(sku, 200)
    metrics.record_call(detail)
    metrics.record_cache_hit(detail)
    metrics.record_call(detail)
    metrics.record_request(detail)
    metrics.record_response(detail, 503)
    metrics.record_error(detail)
    before = metrics.snapshot()
    assert before['estimated_list_cost_usd'] == .010
    assert before['successful_list_cost_usd'] == .005
    assert before['skus'][detail] == {
        'wrapper_calls': 3, 'requests': 2, 'responses_200': 1, 'errors': 1, 'cache_hits': 1,
        'unit_price_usd': .005, 'estimated_list_cost_usd': .010, 'successful_list_cost_usd': .005,
    }
    assert before['skus'][ids]['unit_price_usd'] == 0 and before['estimate_complete']
    # An unpriced actual send makes the subtotal incomplete; it is not a free API.
    metrics.record_call('unknown')
    metrics.record_request('unknown')
    metrics.record_error('unknown')
    snapshot = metrics.snapshot()
    assert snapshot['estimated_list_cost_usd'] == .010 and not snapshot['estimate_complete']
    assert snapshot['skus']['unknown']['estimated_list_cost_usd'] is None
    assert snapshot['skus']['unknown']['unit_price_usd'] is None
    assert 'unknown' not in before['skus']  # snapshots detach previous counters


def test_unknown_wrapper_without_http_send_does_not_claim_an_unbilled_cost():
    metrics = MapsCostMetrics()
    metrics.record_call('unknown')
    snapshot = metrics.snapshot()
    assert snapshot['estimated_list_cost_usd'] == 0 and snapshot['estimate_complete']
    assert snapshot['skus']['unknown']['requests'] == 0

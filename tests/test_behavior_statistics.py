"""Visit statistics must measure repeated visits rather than photo volume."""

import importlib
import json
from copy import deepcopy

import pytest

from app.schemas.behavior import BehaviorItemSchema


def photo(image_id, captured_at='2026-07-18T10:00:00+09:00', place='Cafe A', types=None):
    return BehaviorItemSchema.model_validate({
        'sourceImageId': image_id,
        'location': {'country': 'Korea', 'city': 'Seoul', 'region': 'Mapo',
                     'district': 'Yeonnam', 'placeName': place,
                     'placeTypes': ['cafe'] if types is None else types},
        'timeContext': {'capturedAt': captured_at, 'dayOfWeek': 'sat',
                        'isWeekend': True, 'timeBucket': 'morning', 'season': 'summer'},
    })


def statistics(items):
    module = importlib.import_module('app.services.behavior_statistics')
    result = module.build_behavior_statistics(items)
    json.dumps(result)  # Public pipeline must receive a serializable object.
    return result


def test_photo_burst_does_not_inflate_visit_or_category_frequency():
    base = statistics([photo('a'), photo('b', place='Park', types=['park'])])
    burst = statistics([photo(f'cafe-{i}', types=['cafe', 'cafe']) for i in range(80)]
                       + [photo('b', place='Park', types=['park'])])
    for key in ('visitCount', 'distinctDayCount', 'distinctPlaceCount',
                'distributions', 'placeTypeVisitCounts', 'placeTypeDayCounts', 'dataSufficiency'):
        assert burst[key] == base[key]
    assert burst['inputPhotoCount'] == 81
    assert burst['placeTypeVisitCounts'] == {'cafe': 1, 'park': 1}
    assert burst['dataSufficiency'] == 'provisional'


def test_duplicates_are_removed_before_visits_and_conflicts_are_order_invariant():
    a = photo('duplicate')
    conflict = photo('duplicate', place='Other cafe')
    b = photo('other', '2026-07-19T10:00:00+09:00')
    forward = statistics([a, conflict, a, b])
    assert forward == statistics([b, a, conflict, a])
    assert forward['inputPhotoCount'] == 4
    assert forward['uniquePhotoCount'] == 2
    assert forward['duplicatePhotoCount'] == 2
    assert forward['validPhotoCount'] == 2


def test_visit_window_is_anchored_and_next_day_is_a_revisit():
    result = statistics([
        photo('1', '2026-07-18T10:00:00+09:00'),
        photo('2', '2026-07-18T11:30:00+09:00'),
        photo('3', '2026-07-18T13:00:00+09:00'),
        photo('4', '2026-07-19T10:00:00+09:00'),
    ])
    assert result['visitCount'] == 3
    assert result['distinctPlaceCount'] == 1
    assert result['distinctDayCount'] == 2
    assert result['placeTypeVisitCounts']['cafe'] == 3
    assert result['placeTypeDayCounts']['cafe'] == 2


def test_equivalent_timezone_instants_group_but_local_midnight_splits_visits():
    result = statistics([
        photo('a', '2026-07-18T10:00:00+09:00'),
        photo('b', '2026-07-18T01:00:00Z'),
        photo('c', '2026-07-18T23:50:00+09:00'),
        photo('d', '2026-07-19T00:10:00+09:00'),
    ])
    assert result['visitCount'] == 3
    assert result['distinctDayCount'] == 2


@pytest.mark.parametrize('bad_time', [
    'bad-date', '2026-07-18T10:00:00', '2026-02-30T10:00:00+09:00',
    '0001-01-01T00:00:00+14:00', '9999-12-31T23:59:59-14:00',
])
def test_invalid_or_naive_times_are_excluded_and_counted(bad_time):
    result = statistics([photo('ok'), photo('bad', bad_time)])
    assert result['invalidPhotoCount'] == 1
    assert result['validPhotoCount'] == 1
    assert result['visitCount'] == 1


def test_unknown_places_are_excluded_and_identity_is_normalized():
    a = photo('a', place='  CAFE A  ')
    b = photo('b', place='cafe a')
    empty = photo('empty', place=' ')
    unknown = photo('unknown', place='unknown')
    result = statistics([a, b, empty, unknown])
    assert result['visitCount'] == 1
    assert result['invalidPhotoCount'] == 2
    different_city = deepcopy(b)
    different_city.sourceImageId = 'different-city'
    different_city.location.city = 'Busan'
    assert statistics([b, different_city])['distinctPlaceCount'] == 2


def test_place_types_are_union_per_visit_but_days_are_distinct():
    result = statistics([
        photo('a', types=['cafe', 'food']),
        photo('b', '2026-07-18T10:30:00+09:00', types=['food', 'bakery']),
        photo('c', '2026-07-18T14:00:00+09:00', types=['cafe']),
    ])
    assert result['placeTypeVisitCounts'] == {'cafe': 2, 'food': 1, 'bakery': 1}
    assert result['placeTypeDayCounts'] == {'cafe': 1, 'food': 1, 'bakery': 1}


def test_diverse_sample_is_sufficient_by_explicit_heuristic():
    result = statistics([photo(str(i), f'2026-07-{i + 1:02d}T10:00:00+09:00',
                               place=f'Cafe {i}') for i in range(15)])
    assert result['dataSufficiency'] == 'sufficient'
    assert result['distinctDayCount'] == result['distinctPlaceCount'] == 15


def test_overlapping_gourmet_types_count_once_per_visit():
    result = statistics([photo('a', types=['cafe', 'food']),
                         photo('b', '2026-07-18T10:30:00+09:00', types=['bakery'])])
    assert result['visitCount'] == 1
    assert result['fieldVisitCounts']['travelPurpose.gourmet'] == 1
    assert result['fieldVisitCounts']['activityPreference.gourmetExploration'] == 1
    assert result['fieldDayCounts']['travelPurpose.gourmet'] == 1


def test_blank_source_ids_are_invalid_without_deduplicating_unrelated_photos():
    result = statistics([photo('', place='Cafe A'), photo(' ', place='Park'), photo('valid')])
    assert result['inputPhotoCount'] == result['uniquePhotoCount'] == 3
    assert result['invalidPhotoCount'] == 2
    assert result['duplicatePhotoCount'] == 0
    assert result['validPhotoCount'] == result['visitCount'] == 1

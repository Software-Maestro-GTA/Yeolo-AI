"""Check readable day summaries against finalized places without model calls."""

import re

import pytest

from app.schemas.course import PlaceSchema, StopSchema, TransportToNextSchema


def stops_for(categories: list[str], name: str = '확인된 장소') -> list[StopSchema]:
    """Build final response stops; category facts are independent of names."""
    return [StopSchema(
        sequence=index + 1, arrivalTime='10:00', stayMinutes=60,
        memo='방문 팁', reason='선택된 장소 추천 이유', cost=0,
        place=PlaceSchema(placeId=f'places/{index}', placeName=name, category=category, latitude=37.55, longitude=126.98),
        transportToNext=TransportToNextSchema(type='none', distance=0, minutes=0, cost=0),
    ) for index, category in enumerate(categories)]


def assert_clean_summary(summary: str) -> None:
    assert isinstance(summary, str) and summary.strip()
    assert len(summary) <= 200
    assert not any(term in summary for term in (
        '09:00', '21:00', '10분', '조식', '숙소', '조회 시점',
        '최소 5', '명소 3', '축소', '실패', '재확인', '확인이 필요',
    ))


@pytest.mark.parametrize(('categories', 'experience_words'), [
    (['museum', 'art_gallery'], ('전시', '작품', '문화', '이야기')),
    (['park', 'botanical_garden'], ('자연', '산책', '정원', '풍경')),
    (['restaurant', 'cafe'], ('맛', '음식', '한 끼', '식사')),
])
def test_summary_experience_follows_verified_categories(categories, experience_words):
    from app.services.day_summary import compose_day_summary

    summary = compose_day_summary(stops_for(categories), 1)
    assert_clean_summary(summary)
    assert any(word in summary for word in experience_words)
    assert not any(unsupported in summary for unsupported in ('노을', '야경', '날씨', '고요', '한적', '오래된 골목', '여유로운', '느긋'))


def test_unknown_and_empty_categories_do_not_infer_features_from_place_name():
    from app.services.day_summary import compose_day_summary

    for stops in ([], stops_for(['', 'unknown'], name='노을공원 옛골목 전망대')):
        summary = compose_day_summary(stops, 1)
        assert_clean_summary(summary)
        assert not any(invented in summary for invented in ('노을', '공원', '골목', '전망', '야경', '전시', '산책'))


def test_same_theme_has_stable_day_variants_without_changing_actual_stops():
    from app.services.day_summary import compose_day_summary

    stops = stops_for(['museum', 'art_gallery', 'restaurant'])
    original = [stop.model_dump() for stop in stops]
    first = compose_day_summary(stops, 1)
    second = compose_day_summary(stops, 2)
    assert first != second
    assert first == compose_day_summary(stops, 1)
    assert second == compose_day_summary(stops, 2)
    assert_clean_summary(first)
    assert_clean_summary(second)
    assert [stop.model_dump() for stop in stops] == original


def test_optional_place_names_cannot_leak_markup_control_or_partial_identifiers():
    from app.services.day_summary import compose_day_summary

    long_name = '아주긴공식장소명' * 100
    summary = compose_day_summary(stops_for(['museum'], name=long_name + '<script>bad</script>\x00\x1b[31mhttps://example.com/private'), 1)
    assert_clean_summary(summary)
    assert not any(raw in summary for raw in ('<', '>', '\x00', '\x1b', 'https://', 'bad', long_name[:80]))


@pytest.mark.parametrize('count', [3, 4])
def test_compact_summary_reports_actual_count_without_internal_targets(count):
    from app.services.day_summary import compose_day_summary

    summary = compose_day_summary(stops_for(['museum'] + ['restaurant'] * (count - 1)), 1, compact=True)
    assert_clean_summary(summary)
    assert re.search(rf'{count}\s*곳', summary)
    assert not re.search(r'[35]\s*곳', summary) if count == 4 else not re.search(r'5\s*곳', summary)

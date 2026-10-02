"""Enrich only final course places within an optional bounded image phase."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.schemas.course import CourseSchema


@pytest.fixture
def course_for_images():
    return CourseSchema.model_validate({
        'title': '확정 코스', 'destinationCountry': '대한민국', 'destinationCity': '서울',
        'startDate': '2026-10-05', 'totalDays': 1, 'recommendationReason': '기존 취향 추천 이유',
        'itinerary': {'days': [{'day': 1, 'date': '2026-10-05', 'memo': '확정된 일정', 'stops': [
            {'sequence': index + 1, 'arrivalTime': '09:00', 'stayMinutes': 60, 'memo': '기존 방문 안내',
             'reason': '기존 장소 추천 이유', 'cost': 15000,
             'place': {'placeId': identifier, 'placeName': identifier, 'category': category,
                       'latitude': 37.55, 'longitude': 126.98},
             'transportToNext': {'type': 'none', 'distance': 0, 'minutes': 0, 'cost': 0}}
            for index, (identifier, category) in enumerate([('meal', 'restaurant'), ('museum', 'museum'), ('park', 'park')])
        ]}]},
    })


@pytest.mark.asyncio
async def test_images_are_copied_and_cover_prefers_attraction_with_matching_credit(course_for_images, mocker):
    from app.agent.tools.verified_maps import VerifiedPhoto
    from app.services.course_images import enrich_course_images

    provider = mocker.Mock()
    def credit(identifier):
        return (f'사진 출처: Google Maps; 작성자 {identifier}; '
                f'프로필 https://www.google.com/maps/contrib/{identifier}; '
                f'아바타 https://lh3.googleusercontent.com/a/{identifier}; '
                f'원본 https://www.google.com/maps/place/?cid={identifier}')
    provider.photo = AsyncMock(side_effect=lambda identifier: VerifiedPhoto(f'https://lh3.googleusercontent.com/p/{identifier}', credit(identifier)))
    original = course_for_images.model_dump()
    result = await enrich_course_images(course_for_images, provider)
    assert result is not course_for_images
    assert course_for_images.model_dump() == original
    assert result.coverImageUrl == 'https://lh3.googleusercontent.com/p/museum'
    assert result.recommendationReason == original['recommendationReason']
    cover_stop = next(stop for stop in result.itinerary.days[0].stops if stop.place.photoUrl == result.coverImageUrl)
    assert '작성자 museum' in cover_stop.memo
    assert 'https://www.google.com/maps/place/?cid=museum' in cover_stop.memo
    assert {call.args[0] for call in provider.photo.await_args_list} == {'meal', 'museum', 'park'}
    for stop in result.itinerary.days[0].stops:
        assert stop.place.photoUrl == f'https://lh3.googleusercontent.com/p/{stop.place.placeId}'
        assert stop.memo.startswith('기존 방문 안내\n\n사진 출처: Google Maps')
        assert f'작성자 {stop.place.placeId}' in stop.memo
        assert credit(stop.place.placeId) in stop.memo
        assert stop.reason == '기존 장소 추천 이유'
        assert stop.stayMinutes == 60 and stop.cost == 15000
    assert result.model_dump().keys() == original.keys()


@pytest.mark.asyncio
async def test_no_attraction_photo_uses_first_available_place_image_without_substitution(course_for_images, mocker):
    from app.agent.tools.verified_maps import VerifiedPhoto
    from app.services.course_images import enrich_course_images

    provider = mocker.Mock()
    provider.photo = AsyncMock(side_effect=lambda identifier: VerifiedPhoto('https://lh3.googleusercontent.com/p/meal', '사진 출처: Google Maps meal') if identifier == 'meal' else None)
    result = await enrich_course_images(course_for_images, provider)
    assert result.coverImageUrl == result.itinerary.days[0].stops[0].place.photoUrl
    assert all(not stop.place.photoUrl for stop in result.itinerary.days[0].stops[1:])


@pytest.mark.asyncio
async def test_duplicate_place_ids_fetch_once_and_concurrency_is_bounded(course_for_images, mocker):
    from app.agent.tools.verified_maps import VerifiedPhoto
    from app.services.course_images import enrich_course_images

    course_for_images.itinerary.days[0].stops.append(course_for_images.itinerary.days[0].stops[1].model_copy(deep=True))
    provider = mocker.Mock()
    in_flight = peak = 0
    async def photo(identifier):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(.01)
            return VerifiedPhoto(f'https://lh3.googleusercontent.com/p/{identifier}', f'사진 출처: Google Maps {identifier}')
        finally:
            in_flight -= 1
    provider.photo = AsyncMock(side_effect=photo)
    mocker.patch('app.core.config.settings.COURSE_MAPS_CONCURRENCY', 2)
    result = await enrich_course_images(course_for_images, provider)
    assert provider.photo.await_count == 3
    assert 1 < peak <= 2
    duplicates = [stop for stop in result.itinerary.days[0].stops if stop.place.placeId == 'museum']
    assert len(duplicates) == 2
    assert all(stop.place.photoUrl and 'Google Maps museum' in stop.memo for stop in duplicates)


@pytest.mark.asyncio
async def test_phase_timeout_keeps_completed_images_and_joins_cancelled_tasks(course_for_images, mocker):
    from app.agent.tools.verified_maps import VerifiedPhoto
    from app.services.course_images import enrich_course_images

    provider = mocker.Mock()
    cancelled = []
    async def photo(identifier):
        if identifier == 'museum':
            return VerifiedPhoto('https://lh3.googleusercontent.com/p/museum', '사진 출처: Google Maps museum')
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(identifier)
    provider.photo = AsyncMock(side_effect=photo)
    result = await enrich_course_images(course_for_images, provider, timeout_seconds=.03)
    assert result.coverImageUrl == 'https://lh3.googleusercontent.com/p/museum'
    assert set(cancelled) == {'meal', 'park'}
    assert result.itinerary.days[0].stops[1].place.photoUrl
    assert not result.itinerary.days[0].stops[0].place.photoUrl


@pytest.mark.asyncio
async def test_no_remaining_budget_skips_photo_calls_and_returns_course(course_for_images, mocker):
    from app.services.course_images import enrich_course_images

    provider = mocker.Mock()
    provider.photo = AsyncMock(side_effect=AssertionError('No image request without a phase budget'))
    result = await enrich_course_images(course_for_images, provider, timeout_seconds=0)
    provider.photo.assert_not_awaited()
    assert result.title == course_for_images.title
    assert not result.coverImageUrl
    assert result.recommendationReason == course_for_images.recommendationReason
    assert result.itinerary.days[0].memo.startswith(course_for_images.itinerary.days[0].memo)
    assert '사진' in result.itinerary.days[0].memo
    assert course_for_images.itinerary.days[0].memo == '확정된 일정'


@pytest.mark.asyncio
async def test_optional_photo_errors_do_not_fail_course_or_reuse_stale_urls(course_for_images, mocker):
    from app.services.course_images import enrich_course_images

    course_for_images.coverImageUrl = 'https://old.test/stale-cover'
    course_for_images.itinerary.days[0].stops[0].place.photoUrl = 'https://old.test/stale-place'
    provider = mocker.Mock()
    provider.photo = AsyncMock(side_effect=ValueError('private provider payload'))
    result = await enrich_course_images(course_for_images, provider)
    assert result.coverImageUrl == ''
    assert all(stop.place.photoUrl == '' for stop in result.itinerary.days[0].stops)
    assert 'private provider payload' not in str(result.model_dump())
    assert result.title == course_for_images.title
    assert course_for_images.coverImageUrl == 'https://old.test/stale-cover'


@pytest.mark.asyncio
async def test_external_cancellation_of_image_phase_propagates_after_cleanup(course_for_images, mocker):
    from app.services.course_images import enrich_course_images

    entered = asyncio.Event()
    cleaned = []
    async def photo(identifier):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(identifier)
    provider = mocker.Mock()
    provider.photo = AsyncMock(side_effect=photo)
    task = asyncio.create_task(enrich_course_images(course_for_images, provider))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert cleaned

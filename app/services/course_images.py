"""Add fresh attributed photos to a verified course within an optional time budget."""

import asyncio
import logging
import math

import httpx

from app.agent.tools.verified_maps import (
    VerifiedMapsProvider,
    VerifiedPhoto,
    meal_category_supported,
)
from app.core.config import settings
from app.schemas.course import CourseSchema

logger = logging.getLogger(__name__)


def _cover_attraction(category: str) -> bool:
    """Prefer ordinary visits over meal businesses when choosing a cover."""
    return not (category.endswith('_restaurant') or category in {'restaurant', 'cafe', 'coffee_shop', 'bakery', 'bar', 'food_court', 'meal_takeaway'} or meal_category_supported(category, 'lunch'))


async def enrich_course_images(course: CourseSchema, provider: VerifiedMapsProvider, timeout_seconds: float = 5) -> CourseSchema:
    """Copy a course and add final-place images without risking its valid schedule.

    Args:
        course: A course whose places, timing and duplication have been finalized.
        provider: Request-scoped Maps provider with optional fresh photo lookup.
        timeout_seconds: Remaining optional phase budget, capped at five seconds.
    Returns:
        An independent course with completed fresh images and matching attribution.
        Missing/failed photos stay empty; phase timeout preserves partial results.
    Raises:
        asyncio.CancelledError: External cancellation propagates after task cleanup.
    """
    enriched = course.model_copy(deep=True)
    enriched.coverImageUrl = ''
    enriched.coverImageAttribution = None
    stops = [stop for day in enriched.itinerary.days for stop in day.stops]
    for stop in stops:
        stop.place.photoUrl = ''
        stop.place.photoAttribution = None
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        if enriched.itinerary.days:
            enriched.itinerary.days[0].memo += ' 사진을 준비할 추가 시간이 부족해 이번 코스는 이미지 없이 제공해요.'
        return enriched
    semaphore = asyncio.Semaphore(max(1, min(settings.COURSE_MAPS_CONCURRENCY, 8)))
    completed: dict[str, VerifiedPhoto] = {}

    async def fetch(identifier: str, provider_id: str) -> None:
        try:
            async with semaphore:
                photo = await provider.photo(provider_id)
            if isinstance(photo, VerifiedPhoto):
                completed[identifier] = photo
        except (ValueError, httpx.HTTPError, KeyError, TypeError):
            logger.warning('Optional course photo unavailable')

    identifiers: dict[str, str] = {}
    for stop in stops:
        identifiers.setdefault(stop.place.placeId.removeprefix('places/'), stop.place.placeId)
    tasks = [asyncio.create_task(fetch(identifier, provider_id)) for identifier, provider_id in identifiers.items()]
    try:
        if tasks:
            await asyncio.wait(tasks, timeout=min(5, timeout_seconds))
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    available = []
    for stop in stops:
        photo = completed.get(stop.place.placeId.removeprefix('places/'))
        if photo is not None:
            stop.place.photoUrl = photo.url
            stop.place.photoAttribution = photo.attribution.model_copy(deep=True)
            available.append((stop, photo))
    if available:
        _, cover = next((item for item in available if _cover_attraction(item[0].place.category)), available[0])
        enriched.coverImageUrl = cover.url
        enriched.coverImageAttribution = cover.attribution.model_copy(deep=True)
    return enriched

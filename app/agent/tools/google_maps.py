"""
Google Maps Place (New) API 및 Routes API 연동 도구 모듈.

Google Maps Platform의 Place (New) Text Search API와 Routes API를 호출하여
장소의 위경도, 주소, 평점, 사진, 영업시간 및 장소 간 이동거리/소요시간을 비동기로 추출합니다.
"""

import logging
import os
from typing import Any

import httpx
from langchain_core.tools import tool

from app.core.config import settings
from app.schemas.course import PlaceSchema, TransportToNextSchema

logger = logging.getLogger(__name__)

GOOGLE_PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
GOOGLE_ROUTES_COMPUTE_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"


@tool
async def search_place_detail_tool(query: str, destination_city: str = "") -> dict[str, Any]:
    """Google Maps Place (New) API를 사용해 장소의 정밀 정보(위경도, 주소, 평점, 영업시간 등)를 조회합니다.

    Args:
        query (str): 장소명 또는 검색어 (예: "N서울타워", "족 프린스").
        destination_city (str): 도시명 또는 목적지 보조 정보 (예: "서울", "방콕").

    Returns:
        dict: 장소 상세 정보 (placeId, placeName, placeEngName, category, address, latitude, longitude, rating, photoUrl, openingHours).
    """
    res = await search_place_detail(query, destination_city)
    return res.model_dump()


@tool
async def compute_route_between_places_tool(
    origin_latitude: float,
    origin_longitude: float,
    destination_latitude: float,
    destination_longitude: float,
    travel_mode: str = "transit",
) -> dict[str, Any]:
    """Google Maps Routes API를 사용해 두 위경도 좌표 간 최적 경로, 이동거리(미터), 소요시간(분)을 계산합니다.

    Args:
        origin_latitude (float): 출발지 위도.
        origin_longitude (float): 출발지 경도.
        destination_latitude (float): 목적지 위도.
        destination_longitude (float): 목적지 경도.
        travel_mode (str): 이동 수단 ("walking", "transit", "driving", "taxi", "none").

    Returns:
        dict: 이동 정보 (type, distance, minutes, cost, memo).
    """
    origin = {"latitude": origin_latitude, "longitude": origin_longitude}
    dest = {"latitude": destination_latitude, "longitude": destination_longitude}
    res = await compute_route_between_places(origin, dest, travel_mode)
    return res.model_dump()


async def search_place_detail(query: str, destination_city: str = "") -> PlaceSchema:
    """Google Maps Place (New) API를 사용하여 장소의 상세 정보를 검색하고 추출합니다.

    Args:
        query (str): 장소명 또는 검색어 (예: "N서울타워").
        destination_city (str): 도시명 또는 목적지 보조 정보 (예: "서울").

    Returns:
        PlaceSchema: 장소 상세 정보 (위경도, 주소, 평점, 사진 URL, 영업시간 등).
    """
    api_key = settings.GOOGLE_MAPS_API_KEY or os.getenv("GOOGLE_MAPS_API_KEY", "")
    full_text = f"{destination_city} {query}".strip()

    if not api_key:
        return _get_fallback_place(query, destination_city)

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.id,places.displayName,places.formattedAddress,"
            "places.location,places.rating,places.photos,"
            "places.regularOpeningHours,places.primaryTypeDisplayName"
        ),
    }
    payload = {"textQuery": full_text}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(GOOGLE_PLACES_SEARCH_URL, json=payload, headers=headers)
            if response.status_code == 200:
                data = response.json()
                places = data.get("places", [])
                if places:
                    p = places[0]
                    place_id = p.get("id", f"place_{hash(query)}")
                    display_name = p.get("displayName", {}).get("text", query)
                    address = p.get("formattedAddress", f"{destination_city} {query}")
                    location = p.get("location", {})
                    lat = location.get("latitude", 37.5665)
                    lng = location.get("longitude", 126.9780)
                    rating = p.get("rating")
                    
                    photos = p.get("photos", [])
                    photo_url = ""
                    if photos:
                        photo_name = photos[0].get("name", "")
                        photo_url = f"https://places.googleapis.com/v1/{photo_name}/media?key={api_key}&maxHeightPx=400"

                    opening_hours = []
                    hours_data = p.get("regularOpeningHours", {})
                    if hours_data and "weekdayDescriptions" in hours_data:
                        opening_hours = hours_data.get("weekdayDescriptions", [])

                    category = p.get("primaryTypeDisplayName", {}).get("text", "명소")

                    return PlaceSchema(
                        placeId=place_id,
                        placeName=display_name,
                        placeEngName=query,
                        category=category,
                        address=address,
                        latitude=lat,
                        longitude=lng,
                        rating=rating,
                        photoUrl=photo_url,
                        openingHours=opening_hours,
                    )
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        logger.warning(f"Google Places API request failed for '{query}': {exc}")

    return _get_fallback_place(query, destination_city)


async def compute_route_between_places(
    origin: dict[str, float],
    destination: dict[str, float],
    travel_mode: str = "transit",
) -> TransportToNextSchema:
    """Google Maps Routes API를 활용하여 두 장소 사이의 최적 경로, 이동거리, 소요시간을 계산합니다.

    Args:
        origin (dict): 출발지 위경도 정보 ({"latitude": float, "longitude": float}).
        destination (dict): 목적지 위경도 정보 ({"latitude": float, "longitude": float}).
        travel_mode (str): 이동 수단 ("walking", "transit", "driving", "taxi", "none").

    Returns:
        TransportToNextSchema: 이동 정보 (이동수단, 거리(미터), 소요시간(분), 비용, 안내메모).
    """
    if travel_mode == "none":
        return TransportToNextSchema(
            type="none",
            distance=0.0,
            minutes=0,
            cost=0,
            memo="마지막 일정입니다.",
        )

    api_key = settings.GOOGLE_MAPS_API_KEY or os.getenv("GOOGLE_MAPS_API_KEY", "")

    if not api_key:
        return _get_fallback_transport(travel_mode)

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
    }

    mode_mapping = {
        "walking": "WALK",
        "transit": "TRANSIT",
        "driving": "DRIVE",
        "taxi": "DRIVE",
    }
    google_mode = mode_mapping.get(travel_mode, "TRANSIT")

    payload: dict[str, Any] = {
        "origin": {"location": {"latLng": {"latitude": origin["latitude"], "longitude": origin["longitude"]}}},
        "destination": {"location": {"latLng": {"latitude": destination["latitude"], "longitude": destination["longitude"]}}},
        "travelMode": google_mode,
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(GOOGLE_ROUTES_COMPUTE_URL, json=payload, headers=headers)
            if response.status_code == 200:
                data = response.json()
                routes = data.get("routes", [])
                if routes:
                    r = routes[0]
                    distance_meters = r.get("distanceMeters", 1000)
                    duration_str = r.get("duration", "900s")
                    minutes = int(duration_str.rstrip("s")) // 60 if isinstance(duration_str, str) and duration_str.endswith("s") else 15
                    
                    memo_str = f"{travel_mode}로 약 {minutes}분 소요 ({distance_meters}m)"

                    return TransportToNextSchema(
                        type=travel_mode,  # type: ignore
                        distance=float(distance_meters),
                        minutes=minutes,
                        cost=0,
                        memo=memo_str,
                    )
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        logger.warning(f"Google Routes API request failed: {exc}")

    return _get_fallback_transport(travel_mode)


def _get_fallback_place(query: str, destination_city: str) -> PlaceSchema:
    """API 호출 불가 시 사용하는 기본 Fallback PlaceSchema 반환."""
    return PlaceSchema(
        placeId=f"place_{abs(hash(query))}",
        placeName=query,
        placeEngName=query,
        category="관광명소",
        address=f"{destination_city} {query}",
        latitude=37.5665,
        longitude=126.9780,
        rating=4.5,
        photoUrl="",
        openingHours=["매일 09:00 - 22:00"],
    )


def _get_fallback_transport(travel_mode: str) -> TransportToNextSchema:
    """API 호출 불가 시 사용하는 기본 Fallback TransportToNextSchema 반환."""
    valid_mode = travel_mode if travel_mode in ["walking", "transit", "driving", "taxi", "none"] else "transit"
    return TransportToNextSchema(
        type=valid_mode,  # type: ignore
        distance=1200.0,
        minutes=15,
        cost=0,
        memo=f"{valid_mode} 이동 (약 15분)",
    )

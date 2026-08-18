"""
Google Maps Place (New) API 및 Routes API 연동 도구 모듈.

Google Maps Platform의 Place (New) Text Search API와 Routes API를 호출하여
장소의 위경도, 주소, 평점, 사진, 영업시간 및 장소 간 이동거리/소요시간을 비동기로 추출합니다.
"""

import logging
import os
import re
from typing import Any

import httpx
from langchain_core.tools import tool

from app.core.config import settings
from app.schemas.course import PlaceSchema, TransportToNextSchema

logger = logging.getLogger(__name__)

GOOGLE_PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
GOOGLE_ROUTES_COMPUTE_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"


def _clean_english_name(name: str) -> str:
    """한글이 포함되어 있거나 비어있는 경우 빈 문자열을 반환하고, 유효한 영문/로마자 명칭만 반환합니다."""
    if not name:
        return ""
    if re.search(r"[\uac00-\ud7a3\u1100-\u11ff\u3130-\u318f]", name):
        return ""
    return name.strip()


@tool
async def search_place_detail_tool(
    query: str,
    destination_city: str = "",
    english_query: str = "",
) -> dict[str, Any]:
    """Google Maps Place (New) API를 사용해 장소의 정밀 정보(위경도, 주소, 평점, 영업시간 등)를 조회합니다.

    Args:
        query (str): 장소 한글명 또는 주 검색어 (예: "N서울타워", "레 상피옹").
        destination_city (str): 도시명 또는 목적지 보조 정보 (예: "서울", "파리").
        english_query (str): 해외 장소인 경우 원문 또는 영문 명칭 (예: "Le Comptoir du Relais").

    Returns:
        dict: 장소 상세 정보 (placeId, placeName, placeEngName, category, address, latitude, longitude, rating, photoUrl, openingHours).
    """
    res = await search_place_detail(query, destination_city, english_query)
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


async def _execute_places_text_search(text_query: str, api_key: str) -> list[dict[str, Any]]:
    """Google Places API (New) Text Search 엔드포인트를 호출하여 장소 목록을 반환합니다."""
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.id,places.displayName,places.formattedAddress,"
            "places.location,places.rating,places.photos,"
            "places.regularOpeningHours,places.currentOpeningHours,places.primaryTypeDisplayName"
        ),
    }
    payload = {"textQuery": text_query, "languageCode": "ko"}

    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.post(GOOGLE_PLACES_SEARCH_URL, json=payload, headers=headers)
        if response.status_code == 200:
            data = response.json()
            return data.get("places", [])
    return []


async def search_place_detail(
    query: str,
    destination_city: str = "",
    english_query: str = "",
    fallback_place: PlaceSchema | None = None,
) -> PlaceSchema:
    """Google Maps Place (New) API를 사용하여 장소의 상세 정보를 검색하고 추출합니다.
    1차로 한글 명칭(query)으로 검색하고, 검색 결과가 없을 경우 영문 명칭(english_query)으로 2차 다단계 검색을 수행합니다.
    검색이 모두 실패하면 fallback_place가 있을 경우 기존 유효 정보를 보존하여 반환합니다.

    Args:
        query (str): 장소 한글명 또는 주 검색어 (예: "N서울타워", "레 상피옹").
        destination_city (str): 도시명 또는 목적지 보조 정보 (예: "서울", "파리").
        english_query (str): 해외 장소인 경우 원문 또는 영문 명칭 (예: "Le Comptoir du Relais").
        fallback_place (PlaceSchema | None): API 실패 시 참고할 기존 장소 데이터 (LLM 생성 데이터 보존용).

    Returns:
        PlaceSchema: 장소 상세 정보 (위경도, 주소, 평점, 사진 URL, 영업시간 등).
    """
    api_key = settings.GOOGLE_MAPS_API_KEY or os.getenv("GOOGLE_MAPS_API_KEY", "")
    cleaned_english_query = _clean_english_name(english_query)
    cleaned_fallback_eng = _clean_english_name(fallback_place.placeEngName) if fallback_place else ""
    eng_name = cleaned_english_query or cleaned_fallback_eng

    if not api_key:
        return _get_fallback_place(query, destination_city, eng_name, fallback_place)

    full_text = f"{destination_city} {query}".strip()

    try:
        places = await _execute_places_text_search(full_text, api_key)

        # 1차 한글 검색 결과가 없고 영문명이 존재하는 경우 2차 영문 다단계 검색 시도
        if not places and eng_name:
            eng_full_text = f"{destination_city} {eng_name}".strip()
            places = await _execute_places_text_search(eng_full_text, api_key)

        if places:
            p = places[0]
            place_id = p.get("id", f"place_{abs(hash(query))}")
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
            elif fallback_place and fallback_place.photoUrl:
                photo_url = fallback_place.photoUrl

            opening_hours = []
            hours_data = p.get("regularOpeningHours") or p.get("currentOpeningHours") or {}
            if hours_data and "weekdayDescriptions" in hours_data:
                opening_hours = hours_data.get("weekdayDescriptions", [])
            elif fallback_place and fallback_place.openingHours:
                opening_hours = fallback_place.openingHours

            category = p.get("primaryTypeDisplayName", {}).get("text", "")
            if not category:
                category = fallback_place.category if fallback_place and fallback_place.category else "명소"

            return PlaceSchema(
                placeId=place_id,
                placeName=display_name,
                placeEngName=eng_name,
                category=category,
                address=address,
                latitude=lat,
                longitude=lng,
                rating=rating if rating is not None else (fallback_place.rating if fallback_place else None),
                photoUrl=photo_url,
                openingHours=opening_hours,
            )
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        logger.warning(f"Google Places API request failed for '{query}' (eng: '{english_query}'): {exc}")

    return _get_fallback_place(query, destination_city, eng_name, fallback_place)


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
                    
                    mode_kr = {"walking": "도보", "transit": "대중교통", "driving": "차량", "taxi": "택시"}.get(travel_mode, "이동")
                    dist_text = f"{distance_meters / 1000:.1f}km" if distance_meters >= 1000 else f"{distance_meters}m"
                    memo_str = f"{mode_kr}로 약 {minutes}분 이동 ({dist_text})"

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


def _get_fallback_place(
    query: str,
    destination_city: str,
    english_query: str = "",
    fallback_place: PlaceSchema | None = None,
) -> PlaceSchema:
    """API 호출 불가 또는 검색 실패 시 기본 Fallback PlaceSchema 반환 (기존 fallback_place가 존재할 경우 유효 데이터 보존)."""
    cleaned_english_query = _clean_english_name(english_query)
    cleaned_fallback_eng = _clean_english_name(fallback_place.placeEngName) if fallback_place else ""
    eng_name = cleaned_english_query or cleaned_fallback_eng

    if fallback_place:
        lat = fallback_place.latitude if (fallback_place.latitude != 0.0 or fallback_place.longitude != 0.0) else 0.0
        lng = fallback_place.longitude if (fallback_place.latitude != 0.0 or fallback_place.longitude != 0.0) else 0.0
        return PlaceSchema(
            placeId=fallback_place.placeId if fallback_place.placeId else f"place_{abs(hash(query))}",
            placeName=fallback_place.placeName or query,
            placeEngName=eng_name,
            category=fallback_place.category or "관광명소",
            address=fallback_place.address or f"{destination_city} {query}",
            latitude=lat,
            longitude=lng,
            rating=fallback_place.rating,
            photoUrl=fallback_place.photoUrl or "",
            openingHours=fallback_place.openingHours or [],
        )

    return PlaceSchema(
        placeId=f"place_{abs(hash(query))}",
        placeName=query,
        placeEngName=eng_name,
        category="관광명소",
        address=f"{destination_city} {query}",
        latitude=0.0,
        longitude=0.0,
        rating=None,
        photoUrl="",
        openingHours=[],
    )


def _get_fallback_transport(travel_mode: str) -> TransportToNextSchema:
    """API 호출 불가 시 가짜 수치를 주입하지 않고 안전한 기본 TransportToNextSchema 반환."""
    valid_mode = travel_mode if travel_mode in ["walking", "transit", "driving", "taxi", "none"] else "transit"
    return TransportToNextSchema(
        type=valid_mode,  # type: ignore
        distance=None,
        minutes=None,
        cost=0,
        memo=None,
    )

"""
Google Maps Place (New) API & Routes API 연동 도구 단위 테스트 모듈.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.agent.tools.google_maps import (
    compute_route_between_places,
    search_place_detail,
)
from app.schemas.course import PlaceSchema, TransportToNextSchema


@pytest.mark.asyncio
async def test_search_place_detail_success(mocker):
    """Google Maps Place (New) API 검색 및 상세 정보 추출 성공 시나리오 테스트."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "places": [
            {
                "id": "places/ChIJN1t_tDeuEmsRUsoyG83frY4",
                "displayName": {"text": "N서울타워", "languageCode": "ko"},
                "formattedAddress": "대한민국 서울특별시 용산구 남산공원길 105",
                "location": {"latitude": 37.5511694, "longitude": 126.988227},
                "rating": 4.5,
                "photos": [{"name": "places/ChIJN1t_tDeuEmsRUsoyG83frY4/photos/photo1"}],
                "regularOpeningHours": {
                    "weekdayDescriptions": [
                        "월요일: 오전 10:00 ~ 오후 11:00",
                        "화요일: 오전 10:00 ~ 오후 11:00",
                    ]
                },
                "primaryTypeDisplayName": {"text": "전망대"},
            }
        ]
    }

    mock_post = mocker.patch("httpx.AsyncClient.post", new_callable=AsyncMock)
    mock_post.return_value = mock_response

    place_detail = await search_place_detail(query="N서울타워", destination_city="서울")

    assert isinstance(place_detail, PlaceSchema)
    assert place_detail.placeId == "places/ChIJN1t_tDeuEmsRUsoyG83frY4"
    assert place_detail.placeName == "N서울타워"
    assert place_detail.latitude == 37.5511694
    assert place_detail.longitude == 126.988227
    assert place_detail.rating == 4.5
    assert len(place_detail.openingHours) == 2
    mock_post.assert_called_once()


@pytest.mark.asyncio
async def test_search_place_detail_fallback_on_error(mocker):
    """Google Maps API 호출 실패 시 기본 fallback PlaceSchema 반환 테스트."""
    mocker.patch(
        "httpx.AsyncClient.post",
        side_effect=httpx.HTTPError("Google Maps API Connection Failed"),
    )

    place_detail = await search_place_detail(query="경복궁", destination_city="서울")

    assert isinstance(place_detail, PlaceSchema)
    assert place_detail.placeName == "경복궁"
    assert place_detail.placeId != ""


@pytest.mark.asyncio
async def test_compute_route_between_places_success(mocker):
    """Google Maps Routes API 연동 성공 및 TransportToNextSchema 파싱 테스트."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "routes": [
            {
                "distanceMeters": 1250,
                "duration": "900s",
                "localizedValues": {
                    "distance": {"text": "1.3 km"},
                    "duration": {"text": "15분"},
                },
            }
        ]
    }

    mock_post = mocker.patch("httpx.AsyncClient.post", new_callable=AsyncMock)
    mock_post.return_value = mock_response

    origin = {"latitude": 37.5511694, "longitude": 126.988227}
    destination = {"latitude": 37.579617, "longitude": 126.977041}

    transport_info = await compute_route_between_places(
        origin=origin,
        destination=destination,
        travel_mode="transit",
    )

    assert isinstance(transport_info, TransportToNextSchema)
    assert transport_info.type == "transit"
    assert transport_info.distance == 1250
    assert transport_info.minutes == 15
    mock_post.assert_called_once()


@pytest.mark.asyncio
async def test_compute_route_between_places_fallback(mocker):
    """Google Maps Routes API 실패 시 기본 fallback TransportToNextSchema 반환 테스트."""
    mocker.patch(
        "httpx.AsyncClient.post",
        side_effect=httpx.HTTPError("Routes API Error"),
    )

    transport_info = await compute_route_between_places(
        origin={"latitude": 37.5, "longitude": 127.0},
        destination={"latitude": 37.51, "longitude": 127.01},
        travel_mode="walking",
    )

    assert isinstance(transport_info, TransportToNextSchema)
    assert transport_info.type == "walking"
    assert transport_info.minutes is None
    assert transport_info.distance is None


@pytest.mark.asyncio
async def test_langchain_google_maps_tools(mocker):
    """LangChain @tool 데코레이터 적용 도구 함수 실행 테스트."""
    mock_place_response = PlaceSchema(
        placeId="place_123",
        placeName="경복궁",
        placeEngName="Gyeongbokgung",
        category="사적지",
        address="서울 종로구 사직로 161",
        latitude=37.5796,
        longitude=126.9770,
        rating=4.6,
        photoUrl="",
        openingHours=["09:00~18:00"],
    )
    mocker.patch(
        "app.agent.tools.google_maps.search_place_detail",
        return_value=mock_place_response,
    )

    from app.agent.tools.google_maps import (
        compute_route_between_places_tool,
        search_place_detail_tool,
    )

    place_dict = await search_place_detail_tool.ainvoke(
        {"query": "경복궁", "destination_city": "서울"}
    )
    assert place_dict["placeName"] == "경복궁"
    assert place_dict["latitude"] == 37.5796

    mock_route_response = TransportToNextSchema(
        type="transit",
        distance=1500.0,
        minutes=12,
        cost=1400,
        memo="대중교통 약 12분",
    )
    mocker.patch(
        "app.agent.tools.google_maps.compute_route_between_places",
        return_value=mock_route_response,
    )

    route_dict = await compute_route_between_places_tool.ainvoke(
        {
            "origin_latitude": 37.5665,
            "origin_longitude": 126.9780,
            "destination_latitude": 37.5796,
            "destination_longitude": 126.9770,
            "travel_mode": "transit",
        }
    )
    assert route_dict["type"] == "transit"
    assert route_dict["minutes"] == 12

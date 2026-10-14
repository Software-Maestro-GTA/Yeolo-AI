"""Verify API-AI-2 through HTTP, the real graph and final SSE serialization.

LLM proposals, Maps HTTP and optional quotes are isolated; endpoint, service,
planning, history and public response serialization remain real.
"""

import json
import math
from copy import deepcopy
from unittest.mock import AsyncMock

import httpx
import pytest

from app.agent.tools.verified_maps import Destination, VerifiedMapsProvider
from app.core.config import settings
from app.main import app
from app.services.myrealtrip_quotes import (
    MyRealTripQuoteProvider,
    QuoteItem,
    TripQuotes,
)
from tests.test_course_graph import graph_dependencies as _graph_dependencies

graph_dependencies = _graph_dependencies
API_KEY = "offline-internal-key"
CATEGORIES = {"FOOD", "CAFE", "SPOT", "CULTURE", "NATURE", "ACTIVITY", "NIGHTLIFE"}


@pytest.fixture(autouse=True)
def api_configuration(mocker, tmp_path):
    """Keep authentication and history independent of developer configuration."""
    mocker.patch.object(settings, "INTERNAL_API_KEY", API_KEY)
    mocker.patch.object(settings, "GOOGLE_MAPS_API_KEY", "offline-google-key")
    mocker.patch.object(settings, "COURSE_HISTORY_PATH", str(tmp_path / "api-history.sqlite3"))


@pytest.fixture
def api_request():
    return {
        "userId": "550e8400-e29b-41d4-a716-446655440000", "language": "ko", "mbti": "INTJ",
        "tripCondition": {
            "destinationCountry": "대한민국", "destinationCity": "서울",
            "startDate": "2027-01-10", "totalDays": 1, "budget": 900000,
        },
    }


def parse_sse(response):
    """Parse emitted event/data frames without treating keep-alives as events."""
    frames = []
    for frame in response.text.split("\n\n"):
        if not frame or frame.startswith(":"):
            continue
        lines = frame.splitlines()
        assert len(lines) == 2
        assert lines[0].startswith("event: ") and lines[1].startswith("data: ")
        frames.append((lines[0][7:], json.loads(lines[1][6:])))
    return frames


def assert_number(value, *, nullable=False):
    if nullable and value is None:
        return
    assert type(value) in (int, float) and math.isfinite(value)


def assert_complete_contract(payload):
    """Check the supplied API fields and JSON types before model coercion."""
    assert set(payload) == {"course"}
    course = payload["course"]
    assert set(course) == {
        "title", "destinationCountry", "destinationCity", "startDate", "totalDays",
        "totalCost", "tags", "itinerary",
    }
    for key in ("title", "destinationCountry", "destinationCity", "startDate"):
        assert isinstance(course[key], str) and course[key]
    assert_number(course["totalDays"])
    assert_number(course["totalCost"])
    assert course["totalCost"] >= 0
    assert isinstance(course["tags"], list) and all(isinstance(tag, str) for tag in course["tags"])
    assert set(course["itinerary"]) == {"days"}
    assert isinstance(course["itinerary"]["days"], list)
    for day in course["itinerary"]["days"]:
        assert set(day) == {"day", "date", "stops"}
        assert_number(day["day"])
        assert isinstance(day["date"], str)
        assert isinstance(day["stops"], list) and day["stops"]
        for stop in day["stops"]:
            assert set(stop) == {"sequence", "stayMinutes", "memo", "reason", "cost", "place", "transportToNext"}
            for key in ("sequence", "stayMinutes", "cost"):
                assert_number(stop[key])
            assert stop["cost"] >= 0 and stop["stayMinutes"] > 0
            for key in ("memo", "reason"):
                assert isinstance(stop[key], str) and stop[key]
            place = stop["place"]
            assert set(place) == {"placeId", "placeName", "category", "latitude", "longitude"}
            assert all(isinstance(place[key], str) and place[key] for key in ("placeId", "placeName"))
            assert place["category"] in CATEGORIES
            assert_number(place["latitude"])
            assert_number(place["longitude"])
            transfer = stop["transportToNext"]
            assert set(transfer) == {"type", "distance", "minutes", "memo"}
            assert transfer["type"] in {"walking", "transit", "driving", "taxi", "none"}
            assert_number(transfer["distance"], nullable=True)
            assert_number(transfer["minutes"], nullable=True)
            assert transfer["memo"] is None or isinstance(transfer["memo"], str)


@pytest.mark.asyncio
async def test_production_endpoint_graph_and_serializer_emit_exact_public_contract(
    api_request, graph_dependencies, mocker,
):
    """Exercise final serialization with real verified IDs, types and routes."""
    _, _, llm, draft, venues = graph_dependencies
    search_ids = {"대한민국": "country-KR", "서울, 대한민국": "city-Seoul"}
    records = {
        "country-KR": {"id": "country-KR", "types": ["country", "political"],
                       "addressComponents": [{"shortText": "KR", "types": ["country"]}]},
        "city-Seoul": {"id": "city-Seoul", "types": ["locality", "political"],
                       "addressComponents": [{"shortText": "KR", "types": ["country"]}],
                       "viewport": {"low": {"latitude": 37.3, "longitude": 126.7},
                                    "high": {"latitude": 37.8, "longitude": 127.3}}},
    }
    for name, venue in venues.items():
        place = venue.place
        identifier = place.placeId.removeprefix("places/")
        search_ids[name] = identifier
        records[identifier] = {
            "id": identifier, "types": [place.category, "point_of_interest", "establishment"],
            "formattedAddress": place.address,
            "addressComponents": [{"shortText": "KR", "types": ["country"]}],
            "location": {"latitude": place.latitude, "longitude": place.longitude},
        }
    calls = []

    def respond(outgoing):
        calls.append(outgoing)
        mask = outgoing.headers["X-Goog-FieldMask"]
        if outgoing.url.path.endswith(":searchText"):
            assert mask == "places.id"
            query = json.loads(outgoing.content)["textQuery"]
            return httpx.Response(200, json={"places": [{"id": search_ids[query]}]})
        if outgoing.method == "GET":
            fields = set(mask.split(","))
            assert fields <= {"id", "addressComponents", "formattedAddress", "location", "types", "viewport"}
            identifier = outgoing.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={key: value for key, value in records[identifier].items() if key in fields})
        assert outgoing.url.path.endswith(":computeRoutes")
        route = json.loads(outgoing.content)
        assert route["origin"]["placeId"] in records and route["destination"]["placeId"] in records
        return httpx.Response(200, json={"routes": [{"duration": "720s", "distanceMeters": 700}]})

    quote = mocker.patch.object(
        MyRealTripQuoteProvider, "quote", new_callable=AsyncMock,
        return_value=TripQuotes(QuoteItem("available", 100000), QuoteItem("not_applicable")),
    )
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as maps_client,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        factory = mocker.patch(
            "app.agent.course_graph.VerifiedMapsProvider",
            side_effect=lambda **kwargs: VerifiedMapsProvider(client=maps_client, api_key="offline", **kwargs),
        )
        response = await client.post(
            "/internal/ai/courses", json=api_request,
            headers={"X-Internal-Api-Key": API_KEY, "Accept-Language": "ko"},
        )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    frames = parse_sse(response)
    assert len(frames) > 1 and frames[-1][0] == "complete"
    assert [event for event, _ in frames].count("complete") == 1
    for event, data in frames[:-1]:
        assert event == "progress" and set(data) == {"step", "message"}
        assert data["step"] == "GENERATING_ROUTE"
        assert isinstance(data["message"], str) and data["message"]
    assert_complete_contract(frames[-1][1])
    course = frames[-1][1]["course"]
    assert course["totalCost"] == 135000
    assert course["startDate"] == course["itinerary"]["days"][0]["date"] == api_request["tripCondition"]["startDate"]
    assert course["totalDays"] == len(course["itinerary"]["days"]) == 1
    stops = course["itinerary"]["days"][0]["stops"]
    assert len(stops) == 5 and len({stop["place"]["placeId"] for stop in stops}) == 5
    assert [stop["sequence"] for stop in stops] == [1, 2, 3, 4, 5]
    assert {stop["place"]["placeName"] for stop in stops} == {candidate.name for candidate in draft.days[0].candidates}
    assert {stop["place"]["category"] for stop in stops} == {"SPOT", "FOOD"}
    assert all(stop["transportToNext"]["minutes"] == 12 for stop in stops[:-1])
    assert stops[-1]["transportToNext"] == {"type": "none", "distance": 0.0, "minutes": 0, "memo": None}
    llm.assert_awaited_once()
    quote.assert_awaited_once()
    factory.assert_called_once()
    assert len(calls) == 18  # 7 ID searches + 7 Essentials lookups + 4 routes.


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["language", "budget", "preference", "calendar"])
async def test_pre_stream_bad_request_matches_json_error_envelope(api_request, invalid):
    payload = deepcopy(api_request)
    if invalid == "language":
        payload.pop("language")
    elif invalid == "budget":
        payload["tripCondition"].pop("budget")
    elif invalid == "preference":
        payload["mbti"] = None
    else:
        payload["tripCondition"]["startDate"] = "2027-02-30"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/internal/ai/courses", json=payload, headers={"X-Internal-Api-Key": API_KEY})
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": 400, "message": "코스 생성 조건이 올바르지 않습니다.", "data": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [None, "wrong-key"])
async def test_pre_stream_authentication_error_matches_json_error_envelope(api_request, key):
    headers = {} if key is None else {"X-Internal-Api-Key": key}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/internal/ai/courses", json=api_request, headers=headers)
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": 401, "message": "내부 인증 실패", "data": None}


@pytest.mark.asyncio
async def test_pre_stream_server_configuration_error_matches_json_error_envelope(api_request, mocker):
    mocker.patch.object(settings, "GOOGLE_MAPS_API_KEY", "")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/internal/ai/courses", json=api_request, headers={"X-Internal-Api-Key": API_KEY})
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": 500, "message": "AI 코스 생성 설정을 확인할 수 없습니다.", "data": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["destination", "model"])
async def test_post_stream_failure_closes_without_a_success_event(api_request, mocker, failure):
    """Document current streaming errors; HTTP status cannot change after start."""
    resolution = mocker.patch.object(VerifiedMapsProvider, "resolve_destination", new_callable=AsyncMock)
    if failure == "destination":
        resolution.side_effect = ValueError("No verifiable destination")
    else:
        resolution.return_value = Destination(country_code="KR", south=37.3, west=126.7, north=37.8, east=127.3)
        mocker.patch("app.agent.course_nodes.draft_candidates", new_callable=AsyncMock,
                     side_effect=RuntimeError("Model response failed; private diagnostic"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/internal/ai/courses", json=api_request, headers={"X-Internal-Api-Key": API_KEY})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = parse_sse(response)
    assert frames and all(event == "progress" for event, _ in frames)
    assert frames[-1][1] == {"step": "GENERATING_ROUTE", "message": "여행 코스를 완성하지 못했습니다. 잠시 후 다시 시도해 주세요."}
    assert all(set(data) == {"step", "message"} and data["step"] == "GENERATING_ROUTE" for _, data in frames)
    assert "private diagnostic" not in response.text and "No verifiable destination" not in response.text

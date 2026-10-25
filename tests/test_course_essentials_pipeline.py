"""Exercise the real course graph with Essentials-only Google HTTP requests."""

import json
from collections import Counter
from unittest.mock import AsyncMock

import httpx
import pytest

from app.agent.course_graph import build_course_graph
from app.agent.course_state import Candidate, CourseDraft, DraftDay
from app.agent.tools.verified_maps import (
    InternalPlaceSchema,
    VerifiedMapsProvider,
    VerifiedPlace,
)
from app.schemas.course import CourseRequestSchema, CourseSchema
from app.services.course_history import CourseHistory
from app.services.course_planning import _core_visit
from app.services.maps_cost import classify_maps_sku


@pytest.mark.parametrize('category', ['hospital', 'doctor', 'insurance_agency', 'point_of_interest', 'food', 'cafe', 'restaurant'])
def test_non_tourism_provider_type_cannot_fill_sightseeing_requirement(category):
    """A real ID and a model's museum label cannot establish a sightseeing venue."""
    proposal = Candidate(name='박물관 후보', category='museum', meal='none', stay_minutes=60)
    actual = VerifiedPlace(InternalPlaceSchema(placeId='actual-id', placeName=proposal.name,
                                              category=category, latitude=37.5, longitude=127.))
    assert not _core_visit((proposal, actual))


@pytest.mark.asyncio
async def test_two_day_course_uses_only_ids_details_essentials_and_actual_routes(mocker, tmp_path):
    """Keep real IDs, types, coordinates and routes while using proposed labels."""
    request = CourseRequestSchema.model_validate({
        'userId': '550e8400-e29b-41d4-a716-446655440000', 'language': 'ko', 'mbti': 'INTJ',
        'tripCondition': {'destinationCountry': '대한민국', 'destinationCity': '서울',
                          'startDate': '2027-01-10', 'totalDays': 2, 'budget': 300000},
    })
    proposals = []
    search_ids = {'대한민국': 'country-KR', '서울, 대한민국': 'city-Seoul'}
    records = {
        'country-KR': {'id': 'country-KR', 'types': ['country', 'political'],
                       'addressComponents': [{'shortText': 'KR', 'types': ['country']}]},
        'city-Seoul': {'id': 'city-Seoul', 'types': ['locality', 'political'],
                       'addressComponents': [{'shortText': 'KR', 'types': ['country']}],
                       'viewport': {'low': {'latitude': 37.3, 'longitude': 126.7},
                                    'high': {'latitude': 37.8, 'longitude': 127.3}}},
    }
    for day_index in range(2):
        candidates = []
        for index, (category, meal) in enumerate([
            ('museum', 'none'), ('restaurant', 'lunch'), ('park', 'none'),
            ('restaurant', 'dinner'), ('art_gallery', 'none'),
        ]):
            name = f'제안한 개별 장소 {day_index}-{index}'
            identifier = f'venue-{day_index}-{index}'
            search_ids[f'{name}, KR'] = identifier
            records[identifier] = {
                'id': identifier, 'types': [category, 'point_of_interest', 'establishment'],
                'formattedAddress': '대한민국 서울',
                'addressComponents': [{'shortText': 'KR', 'types': ['country']}],
                'location': {'latitude': 37.55 + index * .0001, 'longitude': 126.98},
            }
            candidates.append(Candidate(name=name, category='unverified_llm_category', meal=meal,
                                        stay_minutes=30 if meal == 'none' else 45,
                                        cost=0 if meal == 'none' else 15000))
        proposals.append(DraftDay(candidates=candidates))
    llm = mocker.patch('app.agent.course_nodes.draft_candidates', new_callable=AsyncMock,
                       return_value=CourseDraft(title='proposal', reason='', days=proposals))
    calls = []

    async def transport(outgoing):
        options = {'headers': dict(outgoing.headers)}
        if outgoing.method == 'POST':
            options['json'] = json.loads(outgoing.content)
        sku = classify_maps_sku(outgoing.method, str(outgoing.url), options)
        assert sku in {'text_search_ids', 'place_details_essentials', 'routes_essentials'}
        calls.append((sku, outgoing))
        if outgoing.url.path.endswith(':searchText'):
            assert outgoing.headers['X-Goog-FieldMask'] == 'places.id'
            return httpx.Response(200, json={'places': [{'id': search_ids[options['json']['textQuery']]}]})
        if outgoing.method == 'GET':
            mask = set(outgoing.headers['X-Goog-FieldMask'].split(','))
            assert mask <= {'id', 'addressComponents', 'formattedAddress', 'location', 'types', 'viewport'}
            identifier = outgoing.url.path.rsplit('/', 1)[-1]
            return httpx.Response(200, json={key: value for key, value in records[identifier].items() if key in mask})
        assert outgoing.url.path.endswith(':computeRoutes')
        assert options['json']['origin']['placeId'] in records
        assert options['json']['destination']['placeId'] in records
        return httpx.Response(200, json={'routes': [{'duration': '300s', 'distanceMeters': 700}]})

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client,
        VerifiedMapsProvider(client=client, api_key='offline', language='ko') as provider,
    ):
        state = await build_course_graph(provider, CourseHistory(path=tmp_path / 'history.sqlite3')).ainvoke(
            {'request': request, 'attempt': 0},
        )
        metrics = provider.metrics.snapshot()
    course = state['course']
    stops = [stop for day in course.itinerary.days for stop in day.stops]
    assert len(stops) == 10 and all(len(day.stops) == 5 for day in course.itinerary.days)
    assert len({stop.place.placeId for stop in stops}) == 10
    assert {stop.place.placeName for stop in stops} == {candidate.name for day in proposals for candidate in day.candidates}
    assert {stop.place.category for stop in stops} == {'museum', 'restaurant', 'park', 'art_gallery'}
    assert all(stop.place.category != 'unverified_llm_category' for stop in stops)
    assert all(stop.transportToNext.minutes == 5 for day in course.itinerary.days for stop in day.stops[:-1])
    # Two days retain their venue costs and include one benchmark hotel night.
    assert state['trip_quotes'].lodging.status == 'estimated'
    assert state['trip_quotes'].flight.status == 'not_applicable'
    assert course.totalCost == 60000 + 60000
    wire = course.model_dump(by_alias=True, context={'public_response': True})
    public = CourseSchema.model_validate(wire)
    assert {stop.place.placeId for day in public.itinerary.days for stop in day.stops} == {
        f'venue-{day_index}-{index}' for day_index in range(2) for index in range(5)
    }
    assert all(stop.place.placeId.startswith('places/') for stop in stops)
    assert {stop.place.category for day in public.itinerary.days for stop in day.stops} == {'CULTURE', 'FOOD', 'NATURE'}
    assert {stop.place.category for stop in stops} == {'museum', 'restaurant', 'park', 'art_gallery'}
    assert Counter(sku for sku, _ in calls) == {'text_search_ids': 12, 'place_details_essentials': 12, 'routes_essentials': 8}
    assert metrics['estimate_complete'] and metrics['estimated_list_cost_usd'] == .1
    llm.assert_awaited_once()

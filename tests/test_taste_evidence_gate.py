"""Accept sparse observed interests while rejecting unusable or unmapped samples."""

import json
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.behavior_service import analyze_behavior_stream
from tests.test_behavior_evidence import full_profile, request, sse_events
from tests.test_behavior_statistics import photo
from tests.test_taste_summary import mock_summary_model

MESSAGE = '분석 가능한 기록이 부족해요. 다른 날짜와 장소의 사진을 추가해 주세요.'


def repeated_cafe_visits():
    """Provide repeated records across the three-day analysis boundary."""
    return [photo('a'), photo('b', '2026-07-19T10:00:00+09:00'),
            photo('c', '2026-07-20T10:00:00+09:00')]


def minimum_observed_photos():
    """Five independent photos across three days, with no repeated strong interest."""
    return [
        photo(f'{kind}-{day}', f'2026-07-{day:02d}T10:00:00+09:00', place=kind, types=[kind])
        for kind, day in [('cafe', 18), ('museum', 18), ('cafe', 19),
                          ('museum', 19), ('shopping_mall', 20)]
    ]


def repeated_interests(*kinds):
    """Keep three visits across three days for every independent place category."""
    return [item.model_copy(update={
        'sourceImageId': f'{kind}-{item.sourceImageId}',
        'location': item.location.model_copy(update={'placeName': kind, 'placeTypes': [kind]}),
    }) for kind in kinds for item in repeated_cafe_visits()]


@pytest.mark.asyncio
@pytest.mark.parametrize('endpoint', ['analysis', 'behavior'])
@pytest.mark.parametrize('items', [
    [photo('a'), photo('b', '2026-07-19T10:00:00+09:00')],
    [photo(str(hour), f'2026-07-18T{hour}:00:00+09:00') for hour in ('10', '14', '18')],
    [photo(f'burst-{index}') for index in range(30)],
    [photo(f'{kind}-{day}', f'2026-07-{day:02d}T10:00:00+09:00', place=kind, types=[kind])
     for kind in ('cafe', 'museum', 'beach') for day in (1, 2)],
    [photo(str(day), f'2026-07-{day:02d}T10:00:00+09:00', types=['festival']) for day in (1, 2, 3, 4, 5)],
    repeated_interests('cafe') + [photo('extra-1'), photo('extra-2')],
    repeated_interests('cafe', 'museum'),
    repeated_interests('cafe', 'beach'),
], ids=['two-visits', 'single-day', 'photo-burst', 'scattered-interests', 'no-display-mapping',
        'one-group', 'two-groups', 'related-fields-do-not-add-groups'])
async def test_insufficient_sample_returns_json_400_without_any_llm(mocker, endpoint, items):
    mocker.patch('app.core.config.settings.INTERNAL_API_KEY', 'offline-api-key')
    analysis = mocker.patch('app.services.behavior_service.generate_taste_profile', new_callable=AsyncMock)
    summary = mocker.patch('app.services.behavior_service.generate_taste_summary', new_callable=AsyncMock)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(f'/internal/ai/taste-profile/{endpoint}',
                                     json=request(items).model_dump(mode='json'),
                                     headers={'X-Internal-Api-Key': 'offline-api-key'})
    assert response.status_code == 400
    assert response.headers['content-type'] == 'application/json'
    assert response.json() == {'status': 400, 'message': MESSAGE, 'data': None}
    analysis.assert_not_awaited()
    summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_service_cannot_bypass_visit_evidence_gate(mocker):
    analysis = mocker.patch('app.services.behavior_service.generate_taste_profile', new_callable=AsyncMock)
    summary = mocker.patch('app.services.behavior_service.generate_taste_summary', new_callable=AsyncMock)
    stream = analyze_behavior_stream(request([photo('single')]))
    with pytest.raises(ValueError, match=MESSAGE):
        await anext(stream)
    analysis.assert_not_awaited()
    summary.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('strong_groups,unsupported_high_score', [(0, False), (0, True), (1, False), (2, False)])
async def test_neutral_scores_no_longer_block_observed_types(mocker, strong_groups, unsupported_high_score):
    mocker.patch('app.core.config.settings.INTERNAL_API_KEY', 'offline-api-key')
    profile = full_profile()
    for name in ('travelPurpose', 'preferredLocationType', 'activityPreference', 'foodPreference'):
        section = getattr(profile, name)
        for field in type(section).model_fields:
            setattr(section, field, 3)
    if unsupported_high_score:
        profile.travelPurpose.wellness = 5
    for section, field in [('foodPreference', 'cafeDessert'), ('activityPreference', 'viewing')][:strong_groups]:
        setattr(getattr(profile, section), field, 5)
    analysis = mocker.patch('app.services.behavior_service.generate_taste_profile', new_callable=AsyncMock, return_value=profile)
    summary = mock_summary_model(mocker)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/internal/ai/taste-profile/analysis',
                                     json=request(repeated_interests('cafe', 'museum', 'shopping_mall')).model_dump(mode='json'),
                                     headers={'X-Internal-Api-Key': 'offline-api-key'})
    assert response.status_code == 200
    events = sse_events(response)
    assert [kind for kind, _ in events] == ['progress', 'progress', 'complete']
    types = events[-1][1]['summary']['types']
    assert {item['typeCode'] for item in types} == {'CAFE_DESSERT', 'CULTURE', 'SHOPPING'}
    assert sum(item['score'] >= 75 for item in types) == strong_groups
    context = json.loads(summary.call_args.args[0])
    assert sum(item['isProvisional'] for item in context['types']) == 3 - strong_groups
    assert all(item['confidence'] == 'medium' for item in context['types'])
    analysis.assert_awaited_once()
    summary.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('score,display_score', [(1, 50), (2, 50), (3, 50), (4, 75), (5, 100)])
async def test_repeated_interests_preserve_guarded_display_scores(mocker, score, display_score):
    mocker.patch('app.core.config.settings.INTERNAL_API_KEY', 'offline-api-key')
    profile = full_profile()
    profile.foodPreference.cafeDessert = score
    profile.travelPurpose.gourmet = 3
    analysis = mocker.patch('app.services.behavior_service.generate_taste_profile', new_callable=AsyncMock, return_value=profile)
    summary = mock_summary_model(mocker)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/internal/ai/taste-profile/analysis',
                                     json=request(repeated_interests('cafe', 'museum', 'shopping_mall')).model_dump(mode='json'),
                                     headers={'X-Internal-Api-Key': 'offline-api-key'})
    events = sse_events(response)
    assert [kind for kind, _ in events] == ['progress', 'progress', 'complete']
    assert events[1][1]['step'] == 'GENERATING_SUMMARY'
    assert set(events[-1][1]) == {'tasteProfile', 'summary'}
    types = events[-1][1]['summary']['types']
    assert len(types) == 3
    assert {item['typeCode'] for item in types} == {'CAFE_DESSERT', 'CULTURE', 'SHOPPING'}
    assert next(item['score'] for item in types if item['typeCode'] == 'CAFE_DESSERT') == display_score
    analysis.assert_awaited_once()
    summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_summary_returning_only_two_types_never_emits_complete(mocker):
    from tests.test_taste_summary import draft_for

    mocker.patch('app.core.config.settings.INTERNAL_API_KEY', 'offline-api-key')
    analysis = mocker.patch('app.services.behavior_service.generate_taste_profile', new_callable=AsyncMock, return_value=full_profile())

    async def incomplete_summary(serialized_context, **kwargs):
        data = draft_for(serialized_context).model_dump()
        data['types'] = data['types'][:2]
        return data

    summary = mocker.patch('app.services.behavior_service.generate_taste_summary', side_effect=incomplete_summary)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/internal/ai/taste-profile/analysis',
                                     json=request(repeated_interests('cafe', 'museum', 'shopping_mall')).model_dump(mode='json'),
                                     headers={'X-Internal-Api-Key': 'offline-api-key'})
    events = sse_events(response)
    assert [kind for kind, _ in events] == ['progress', 'progress', 'error']
    assert events[-1][1]['status'] == 500
    analysis.assert_awaited_once()
    summary.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('endpoint', ['analysis', 'behavior'])
async def test_five_photos_on_three_days_complete_without_repeated_high_scores(mocker, endpoint):
    mocker.patch('app.core.config.settings.INTERNAL_API_KEY', 'offline-api-key')
    analysis = mocker.patch('app.services.behavior_service.generate_taste_profile',
                            new_callable=AsyncMock, return_value=full_profile())
    summary = mock_summary_model(mocker)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post(f'/internal/ai/taste-profile/{endpoint}',
                                     json=request(minimum_observed_photos()).model_dump(mode='json'),
                                     headers={'X-Internal-Api-Key': 'offline-api-key'})
    assert response.status_code == 200
    events = sse_events(response)
    assert [kind for kind, _ in events] == ['progress', 'progress', 'complete']
    types = events[-1][1]['summary']['types']
    assert {item['typeCode'] for item in types} == {'CAFE_DESSERT', 'CULTURE', 'SHOPPING'}
    assert len(types) == 3 and all(item['score'] == 50 for item in types)
    context = json.loads(summary.call_args.args[0])
    assert all(item['confidence'] == 'low' and item['isProvisional'] for item in context['types'])
    shopping = next(item for item in context['types'] if item['typeCode'] == 'SHOPPING')
    assert shopping['visitCount'] == shopping['distinctDayCount'] == 1
    assert all(item['isProvisional'] for item in context['recommendationInterests'])
    analysis.assert_awaited_once()
    summary.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('sample', ['four-valid', 'two-days', 'duplicate-id', 'invalid-time', 'unknown-place'])
async def test_minimum_sample_uses_valid_deduplicated_records(mocker, sample):
    mocker.patch('app.core.config.settings.INTERNAL_API_KEY', 'offline-api-key')
    items = minimum_observed_photos()
    if sample == 'four-valid':
        items.pop(0)
    elif sample == 'two-days':
        items[-1].timeContext.capturedAt = '2026-07-19T11:00:00+09:00'
    elif sample == 'duplicate-id':
        items[2] = items[0].model_copy(deep=True)
    elif sample == 'invalid-time':
        items[0].timeContext.capturedAt = 'invalid'
    else:
        items[0].location.placeName = 'unknown'
    analysis = mocker.patch('app.services.behavior_service.generate_taste_profile', new_callable=AsyncMock)
    summary = mocker.patch('app.services.behavior_service.generate_taste_summary', new_callable=AsyncMock)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/internal/ai/taste-profile/analysis',
                                     json=request(items).model_dump(mode='json'),
                                     headers={'X-Internal-Api-Key': 'offline-api-key'})
    assert response.status_code == 400
    assert response.json() == {'status': 400, 'message': MESSAGE, 'data': None}
    analysis.assert_not_awaited()
    summary.assert_not_awaited()

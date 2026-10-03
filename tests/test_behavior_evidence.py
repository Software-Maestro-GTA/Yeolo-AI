"""Mocked inference verifies safe evidence adjustment and SSE compatibility."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.schemas.behavior import BehaviorAnalysisRequest
from app.schemas.taste_profile import (
    ActivityFoodSpendingOutput,
    LocationEnvironmentOutput,
    PurposePaceCompanionOutput,
    TasteProfileSchema,
)
from app.services.behavior_service import analyze_behavior_stream
from tests.test_behavior_statistics import photo


@pytest.fixture
def chains(mocker):
    purpose = {name: 5 for name in PurposePaceCompanionOutput.model_fields}
    purpose.update(travelPaceDensity='dense_schedule', companionType='friends')
    location = {name: (True if field.annotation is bool else 5)
                for name, field in LocationEnvironmentOutput.model_fields.items()}
    activity = {name: 5 for name in ActivityFoodSpendingOutput.model_fields}
    activity['spendingTendency'] = 'luxury'
    values = [MagicMock(content='Auxiliary explanation'),
              PurposePaceCompanionOutput(**purpose),
              LocationEnvironmentOutput(**location), ActivityFoodSpendingOutput(**activity)]
    names = ['summarize_chain', 'purpose_pace_companion_chain',
             'location_environment_chain', 'activity_food_spending_chain']
    result = []
    for name, value in zip(names, values, strict=True):
        chain = AsyncMock()
        chain.ainvoke.return_value = value
        mocker.patch(f'app.services.behavior_service.{name}', chain)
        result.append(chain)
    return result


def request(items):
    return BehaviorAnalysisRequest(userId='550e8400-e29b-41d4-a716-446655440000', items=items)


async def complete(items):
    events = [event async for event in analyze_behavior_stream(request(items))]
    assert events[0]['event'] == 'progress'
    assert events[-1]['event'] == 'complete'
    return events[-1]['data']


@pytest.mark.asyncio
async def test_all_chains_receive_same_structured_visit_statistics(chains):
    await complete([photo('a')])
    reports = [chain.ainvoke.call_args.args[0]['statistics_report'] for chain in chains]
    assert all(report == reports[0] for report in reports)
    stats = json.loads(reports[0])
    assert stats['visitCount'] == stats['distinctPlaceCount'] == 1
    assert '550e8400-e29b-41d4-a716-446655440000' not in reports[0]


@pytest.mark.asyncio
async def test_sparse_sample_neutralizes_unsupported_and_caps_observed_scores(chains):
    data = await complete([photo('a')])
    profile = data['tasteProfile']
    TasteProfileSchema.model_validate(profile)
    assert profile['foodPreference']['cafeDessert'] == 3
    assert profile['foodPreference']['dietaryRestriction'] == 3
    assert profile['foodPreference']['familiarFoodPreferred'] == 3
    assert profile['activityPreference']['photographyVideo'] == 3
    assert profile['activityPreference']['localInteraction'] == 3
    assert profile['activityPreference']['nightlife'] == 3
    assert profile['travelPurpose']['shopping'] == 3
    assert profile['companionType'] == 'solo'
    assert profile['spendingTendency'] == 'moderate'
    assert profile['travelPaceDensity'] == 'balanced'
    assert profile['seasonalEnvironmentPreference'] == []
    metadata = data['analysisMetadata']
    assert metadata['statistics']['visitCount'] == 1
    assert metadata['statistics']['dataSufficiency'] == 'provisional'
    placeholders = {'companionType', 'spendingTendency', 'travelPaceDensity'}
    assert placeholders <= set(metadata['fallbackFields'])
    assert placeholders <= set(metadata['requiresUserConfirmation'])
    evidence = metadata['fieldEvidence']['foodPreference.dietaryRestriction']
    assert evidence['insufficientEvidence'] is True
    assert evidence['confidence'] == 'low'
    assert 'evidence' in evidence


@pytest.mark.asyncio
async def test_repeated_cafe_visits_allow_high_score_but_single_day_does_not(chains):
    repeated = await complete([photo('a'), photo('b', '2026-07-18T14:00:00+09:00'),
                               photo('c', '2026-07-19T10:00:00+09:00')])
    assert repeated['tasteProfile']['foodPreference']['cafeDessert'] == 5
    assert repeated['analysisMetadata']['fieldEvidence']['foodPreference.cafeDessert']['confidence'] == 'medium'
    one_day = await complete([photo('a'), photo('b', '2026-07-18T14:00:00+09:00'),
                             photo('c', '2026-07-18T18:00:00+09:00')])
    assert one_day['tasteProfile']['foodPreference']['cafeDessert'] == 3


@pytest.mark.asyncio
async def test_unobserved_low_score_is_neutral_rather_than_dislike(chains):
    chains[1].ainvoke.return_value.shopping = 1
    data = await complete([photo('a')])
    assert data['tasteProfile']['travelPurpose']['shopping'] == 3


@pytest.mark.asyncio
async def test_night_photos_do_not_prove_nightlife(chains):
    items = [photo(str(i), f'2026-07-{i + 1:02d}T23:00:00+09:00', place='Park', types=['park'])
             for i in range(4)]
    for item in items:
        item.timeContext.timeBucket = 'night'
    data = await complete(items)
    assert data['tasteProfile']['activityPreference']['nightlife'] == 3


@pytest.mark.asyncio
async def test_summary_failure_does_not_abort_scoring(chains):
    chains[0].ainvoke.side_effect = RuntimeError('summary unavailable')
    data = await complete([photo('a')])
    assert 'analysisMetadata' in data
    for chain in chains[1:]:
        chain.ainvoke.assert_awaited_once()
        assert json.loads(chain.ainvoke.call_args.args[0]['statistics_report'])['visitCount'] == 1


@pytest.mark.asyncio
async def test_content_blocks_are_normalized_before_scoring(chains):
    chains[0].ainvoke.return_value.content = [{'type': 'text', 'text': 'Cafe visits.'},
                                            {'type': 'image_url', 'image_url': 'unused'}]
    await complete([photo('a')])
    for chain in chains[1:]:
        assert chain.ainvoke.call_args.args[0]['fact_sheet'] == 'Cafe visits.'


def sse_events(response):
    events = []
    for block in response.text.strip().split('\n\n'):
        lines = block.splitlines()
        kind = next(line.removeprefix('event:').strip() for line in lines if line.startswith('event:'))
        payload = next(json.loads(line.removeprefix('data:').strip()) for line in lines if line.startswith('data:'))
        events.append((kind, payload))
    return events


@pytest.mark.asyncio
async def test_scoring_failure_returns_sse_error_and_no_complete(chains, mocker):
    mocker.patch('app.core.config.settings.INTERNAL_API_KEY', 'test-key')
    chains[2].ainvoke.side_effect = RuntimeError('scoring unavailable')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/internal/ai/taste-profile/analysis',
                                     json=request([photo('a')]).model_dump(mode='json'),
                                     headers={'X-Internal-Api-Key': 'test-key'})
    assert response.status_code == 200
    events = sse_events(response)
    assert events[-1][0] == 'error'
    assert events[-1][1]['status'] == 500
    assert all(kind != 'complete' for kind, _ in events)


@pytest.mark.asyncio
async def test_all_invalid_records_rejected_before_llm(chains, mocker):
    mocker.patch('app.core.config.settings.INTERNAL_API_KEY', 'test-key')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/internal/ai/taste-profile/analysis',
                                     json=request([photo('a', 'invalid')]).model_dump(mode='json'),
                                     headers={'X-Internal-Api-Key': 'test-key'})
    assert response.status_code == 400
    for chain in chains:
        chain.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('score', [1, 2])
async def test_supported_visits_do_not_establish_dislike(chains, score):
    chains[3].ainvoke.return_value.cafeDessert = score
    data = await complete([photo('a'), photo('b', '2026-07-18T14:00:00+09:00'),
                           photo('c', '2026-07-19T10:00:00+09:00')])
    assert data['tasteProfile']['foodPreference']['cafeDessert'] == 3
    assert data['analysisMetadata']['fieldEvidence']['foodPreference.cafeDessert']['insufficientEvidence'] is False


@pytest.mark.asyncio
@pytest.mark.parametrize('season, allowed', [('summer', True), ('winter', False)])
async def test_summer_resort_requires_matching_season_and_repeated_visits(chains, season, allowed):
    items = [photo('a', types=['beach']),
             photo('b', '2026-07-18T14:00:00+09:00', types=['beach']),
             photo('c', '2026-07-19T10:00:00+09:00', types=['beach'])]
    for item in items:
        item.timeContext.season = season
    data = await complete(items)
    assert ('summer_resort' in data['tasteProfile']['seasonalEnvironmentPreference']) is allowed


@pytest.mark.asyncio
async def test_matched_place_types_only_contains_observed_types(chains):
    data = await complete([photo('a', types=['cafe', 'park'])])
    evidence = data['analysisMetadata']['fieldEvidence']['travelPurpose.gourmet']['evidence']
    assert evidence['matchedPlaceTypes'] == ['cafe']
    unsupported = data['analysisMetadata']['fieldEvidence']['foodPreference.dietaryRestriction']['evidence']
    assert unsupported['matchedPlaceTypes'] == []


@pytest.mark.asyncio
async def test_only_blank_source_ids_rejected_before_llm(chains, mocker):
    mocker.patch('app.core.config.settings.INTERNAL_API_KEY', 'test-key')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/internal/ai/taste-profile/analysis',
                                     json=request([photo(''), photo('  ', place='Other')]).model_dump(mode='json'),
                                     headers={'X-Internal-Api-Key': 'test-key'})
    assert response.status_code == 400
    for chain in chains:
        chain.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_environment_matched_types_only_include_season_qualifying_visits(chains):
    summer_beach = photo('summer-beach', types=['beach'])
    winter_resort = photo('winter-resort', '2026-12-18T10:00:00+09:00', types=['resort_hotel'])
    winter_resort.timeContext.season = 'winter'
    summer_ski = photo('summer-ski', types=['ski_resort'])
    data = await complete([summer_beach, winter_resort, summer_ski])
    evidence = data['analysisMetadata']['fieldEvidence']
    summer = evidence['seasonalEnvironmentPreference.summer_resort']['evidence']
    assert summer['matchedPlaceTypes'] == ['beach']
    assert summer['visitCount'] == 1
    winter = evidence['seasonalEnvironmentPreference.winter_sports']['evidence']
    assert winter['matchedPlaceTypes'] == []
    assert winter['visitCount'] == 0

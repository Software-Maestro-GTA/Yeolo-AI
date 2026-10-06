"""One-call inference preserves the API and guarantees auditable seasonal output."""

import importlib
import json
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.main import app
from app.schemas.behavior import BehaviorAnalysisRequest
from app.schemas.taste_profile import TasteProfileSchema
from app.services.behavior_evidence import guard_taste_profile
from app.services.behavior_service import analyze_behavior_stream
from tests.test_behavior_statistics import photo, statistics


def full_profile(seasonal=None):
    values = {}
    for group in ('travelPurpose', 'preferredLocationType', 'activityPreference', 'foodPreference'):
        schema = TasteProfileSchema.model_fields[group].annotation
        values[group] = {key: 5 for key in schema.model_fields}
    values.update(travelPaceDensity='dense_schedule', spendingTendency='luxury', companionType='friends',
                  seasonalEnvironmentPreference=[] if seasonal is None else seasonal)
    return TasteProfileSchema(**values)


@pytest.fixture
def chain(mocker):
    result = AsyncMock()
    result.ainvoke.return_value = full_profile(['dry_weather', 'off_season'])
    mocker.patch('app.services.behavior_service.taste_profile_chain', result)
    return result


def request(items):
    return BehaviorAnalysisRequest(userId='550e8400-e29b-41d4-a716-446655440000', items=items)


def adjusted(items, seasonal=None):
    return guard_taste_profile(full_profile(seasonal), statistics(items))


async def complete(items):
    events = [event async for event in analyze_behavior_stream(request(items))]
    assert events[0]['event'] == 'progress'
    assert events[-1]['event'] == 'complete'
    assert set(events[-1]['data']) == {'tasteProfile'}
    return events[-1]['data']['tasteProfile']


@pytest.mark.asyncio
async def test_single_call_receives_visit_statistics_and_returns_exact_api(chain):
    profile = await complete([photo('a')])
    chain.ainvoke.assert_awaited_once()
    payload = chain.ainvoke.call_args.args[0]
    assert set(payload) == {'statistics_report'}
    stats = json.loads(payload['statistics_report'])
    assert stats['visitCount'] == stats['distinctPlaceCount'] == 1
    assert str(request([]).userId) not in payload['statistics_report']
    assert 'Cafe A' not in payload['statistics_report']
    assert profile['seasonalEnvironmentPreference'] == ['warm_region']
    TasteProfileSchema.model_validate(profile)


def test_sparse_sample_neutralizes_unsupported_scores_and_records_placeholders():
    profile, metadata = adjusted([photo('a')])
    assert all(value == 3 for group in ('travelPurpose', 'preferredLocationType', 'activityPreference', 'foodPreference')
               for value in getattr(profile, group).model_dump().values())
    assert profile.companionType == 'solo'
    assert profile.spendingTendency == 'moderate'
    assert profile.travelPaceDensity == 'balanced'
    assert metadata.statistics['dataSufficiency'] == 'provisional'
    placeholders = {'companionType', 'spendingTendency', 'travelPaceDensity'}
    assert placeholders <= set(metadata.fallbackFields)
    assert placeholders <= set(metadata.requiresUserConfirmation)


def test_repeated_visits_allow_high_score_but_single_day_does_not():
    profile, metadata = adjusted([photo('a'), photo('b', '2026-07-18T14:00:00+09:00'),
                                  photo('c', '2026-07-19T10:00:00+09:00')])
    assert profile.foodPreference.cafeDessert == 5
    assert metadata.fieldEvidence['foodPreference.cafeDessert'].confidence == 'medium'
    profile, _ = adjusted([photo('a'), photo('b', '2026-07-18T14:00:00+09:00'),
                           photo('c', '2026-07-18T18:00:00+09:00')])
    assert profile.foodPreference.cafeDessert == 3


@pytest.mark.parametrize('score', [1, 2])
def test_supported_visits_do_not_establish_dislike(score):
    profile = full_profile()
    profile.foodPreference.cafeDessert = score
    result, _ = guard_taste_profile(profile, statistics([photo('a'), photo('b', '2026-07-18T14:00:00+09:00'),
                                                       photo('c', '2026-07-19T10:00:00+09:00')]))
    assert result.foodPreference.cafeDessert == 3


@pytest.mark.parametrize('season, expected', [('summer', 'warm_region'), ('winter', 'cold_region'),
                                            ('spring', 'spring_flower_autumn_foliage'),
                                            ('autumn', 'spring_flower_autumn_foliage')])
def test_empty_or_unsupported_llm_choices_receive_seasonal_placeholder(season, expected):
    item = photo('a')
    item.timeContext.season = season
    for proposed in ([], ['dry_weather', 'peak_season', 'warm_region']):
        profile, metadata = adjusted([item], proposed)
        assert profile.seasonalEnvironmentPreference == [expected]
        path = f'seasonalEnvironmentPreference.{expected}'
        evidence = metadata.fieldEvidence[path]
        assert evidence.confidence == 'low'
        assert evidence.insufficientEvidence is True
        assert evidence.evidence.get('fallbackReason')
        assert path in metadata.fallbackFields
        assert path in metadata.requiresUserConfirmation


def seasonal_photo(image_id, day, types, season):
    item = photo(image_id, f'2026-07-{day:02d}T10:00:00+09:00', types=types)
    item.timeContext.season = season
    return item


@pytest.mark.parametrize('types,season,expected', [(['beach'], 'summer', 'summer_resort'),
                                                 (['ski_resort'], 'winter', 'winter_sports'),
                                                 (['botanical_garden'], 'spring', 'spring_flower_autumn_foliage'),
                                                 (['botanical_garden'], 'autumn', 'spring_flower_autumn_foliage')])
def test_repeated_activity_overrides_empty_or_unrelated_llm_choices(types, season, expected):
    items = [seasonal_photo(str(day), day, types, season) for day in (1, 2, 3)]
    profile, metadata = adjusted(items, ['dry_weather'])
    assert profile.seasonalEnvironmentPreference == [expected]
    evidence = metadata.fieldEvidence[f'seasonalEnvironmentPreference.{expected}']
    assert evidence.confidence == 'medium'
    assert evidence.insufficientEvidence is False
    assert evidence.evidence['visitCount'] == 3
    assert evidence.evidence['matchedPlaceTypes'] == types


def test_multiple_strong_observed_preferences_survive_and_are_unique():
    items = [seasonal_photo(f'beach-{day}', day, ['beach'], 'summer') for day in (1, 2, 3)]
    items += [seasonal_photo(f'ski-{day}', day, ['ski_resort'], 'winter') for day in (4, 5, 6)]
    profile, _ = adjusted(items, ['winter_sports', 'winter_sports'])
    assert set(profile.seasonalEnvironmentPreference) == {'summer_resort', 'winter_sports'}
    assert len(profile.seasonalEnvironmentPreference) == 2


def test_strong_activity_is_preferred_over_other_weak_activity():
    items = [seasonal_photo(f'beach-{day}', day, ['beach'], 'summer') for day in (1, 2, 3)]
    items += [seasonal_photo('ski', 4, ['ski_resort'], 'winter')]
    profile, _ = adjusted(items)
    assert profile.seasonalEnvironmentPreference == ['summer_resort']


def test_weak_activity_beats_dominant_season_and_records_uncertainty():
    items = [seasonal_photo('ski', 1, ['ski_resort'], 'winter')]
    items += [seasonal_photo(f'cafe-{day}', day, ['cafe'], 'summer') for day in (2, 3, 4)]
    profile, metadata = adjusted(items)
    assert profile.seasonalEnvironmentPreference == ['winter_sports']
    field = metadata.fieldEvidence['seasonalEnvironmentPreference.winter_sports']
    assert field.confidence == 'low'
    assert field.insufficientEvidence is True
    assert field.evidence.get('fallbackReason')


def test_weak_candidates_choose_one_strongest_and_ties_are_order_invariant():
    beach = seasonal_photo('beach', 1, ['beach'], 'summer')
    ski = seasonal_photo('ski', 2, ['ski_resort'], 'winter')
    first, _ = adjusted([beach, ski])
    second, _ = adjusted([ski, beach])
    assert len(first.seasonalEnvironmentPreference) == 1
    assert first.seasonalEnvironmentPreference == second.seasonalEnvironmentPreference
    strongest, _ = adjusted([beach, ski, seasonal_photo('ski-2', 3, ['ski_resort'], 'winter')])
    assert strongest.seasonalEnvironmentPreference == ['winter_sports']


def test_season_distribution_ties_and_duplicate_bursts_are_deterministic():
    summer = seasonal_photo('summer', 1, ['cafe'], 'summer')
    winter = seasonal_photo('winter', 2, ['cafe'], 'winter')
    profile, _ = adjusted([summer, winter])
    reverse, _ = adjusted([winter, summer])
    duplicate, _ = adjusted([winter] * 30 + [summer])
    assert len(profile.seasonalEnvironmentPreference) == 1
    assert profile.seasonalEnvironmentPreference == reverse.seasonalEnvironmentPreference == duplicate.seasonalEnvironmentPreference
    # Frequency must be visits, not the number of distinct photos in one burst.
    burst = [seasonal_photo(f'burst-{i}', 2, ['cafe'], 'winter') for i in range(40)]
    burst_result, _ = adjusted([summer] + burst)
    assert burst_result.seasonalEnvironmentPreference == profile.seasonalEnvironmentPreference


def test_seasonally_mismatched_places_cannot_prove_activity():
    item = seasonal_photo('summer-ski', 1, ['ski_resort'], 'summer')
    profile, metadata = adjusted([item], ['winter_sports'])
    assert profile.seasonalEnvironmentPreference == ['warm_region']
    evidence = metadata.fieldEvidence['seasonalEnvironmentPreference.winter_sports'].evidence
    assert evidence['visitCount'] == 0
    assert evidence['matchedPlaceTypes'] == []


def test_matched_place_types_only_contains_observed_qualifying_types():
    _, metadata = adjusted([photo('a', types=['cafe', 'park'])])
    assert metadata.fieldEvidence['travelPurpose.gourmet'].evidence['matchedPlaceTypes'] == ['cafe']
    assert metadata.fieldEvidence['foodPreference.dietaryRestriction'].evidence['matchedPlaceTypes'] == []


def test_analysis_schema_requires_seasonal_choice_but_shared_schema_accepts_historical_empty():
    module = importlib.import_module('app.schemas.taste_profile')
    analysis_schema = module.TasteProfileAnalysisOutput
    empty = full_profile().model_dump()
    TasteProfileSchema.model_validate(empty)
    with pytest.raises(ValidationError):
        analysis_schema.model_validate(empty)
    analysis_schema.model_validate(full_profile(['warm_region']).model_dump())


def sse_events(response):
    events = []
    for block in response.text.strip().split('\n\n'):
        lines = block.splitlines()
        kind = next(line.removeprefix('event:').strip() for line in lines if line.startswith('event:'))
        payload = next(json.loads(line.removeprefix('data:').strip()) for line in lines if line.startswith('data:'))
        events.append((kind, payload))
    return events


@pytest.mark.asyncio
async def test_scoring_failure_returns_sse_error_without_complete_or_retry(chain, mocker):
    mocker.patch('app.core.config.settings.INTERNAL_API_KEY', 'test-key')
    chain.ainvoke.side_effect = RuntimeError('scoring unavailable')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/internal/ai/taste-profile/analysis',
                                     json=request([photo('a')]).model_dump(mode='json'),
                                     headers={'X-Internal-Api-Key': 'test-key'})
    assert response.status_code == 200
    events = sse_events(response)
    assert events[-1][0] == 'error'
    assert events[-1][1]['status'] == 500
    assert all(kind != 'complete' for kind, _ in events)
    chain.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('items', [[photo('a', 'invalid')], [photo(''), photo('  ', place='Other')], []])
async def test_invalid_records_rejected_before_llm(chain, mocker, items):
    mocker.patch('app.core.config.settings.INTERNAL_API_KEY', 'test-key')
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/internal/ai/taste-profile/analysis',
                                     json=request(items).model_dump(mode='json'),
                                     headers={'X-Internal-Api-Key': 'test-key'})
    assert response.status_code == 400
    chain.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_internal_trace_records_evidence_without_exposing_metadata(chain, mocker):
    active_run = mocker.MagicMock()
    mocker.patch('app.services.behavior_service.get_current_run_tree', return_value=active_run)
    result = await complete([photo('a')])
    active_run.add_metadata.assert_called_once()
    saved = active_run.add_metadata.call_args.args[0]['analysisMetadata']
    assert saved['statistics']['visitCount'] == 1
    assert saved['fieldEvidence']['seasonalEnvironmentPreference.warm_region']['confidence'] == 'low'
    assert saved['fieldEvidence']['seasonalEnvironmentPreference.warm_region']['evidence']['fallbackReason']
    assert 'seasonalEnvironmentPreference.warm_region' in saved['requiresUserConfirmation']
    assert 'analysisMetadata' not in result
    chain.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_trace_metadata_recording_failure_does_not_break_successful_inference(chain, mocker):
    active_run = mocker.MagicMock()
    active_run.add_metadata.side_effect = RuntimeError('trace unavailable')
    mocker.patch('app.services.behavior_service.get_current_run_tree', return_value=active_run)
    profile = await complete([photo('a')])
    assert profile['seasonalEnvironmentPreference'] == ['warm_region']
    chain.ainvoke.assert_awaited_once()
    active_run.add_metadata.assert_called_once()

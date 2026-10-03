"""Run one real full-profile inference with synthetic photos and verify remote tracing.

Run from the repository root: uv run python docs/taste-analysis/live-verification-full-profile-2026-10-03/verify.py
This deliberately makes exactly one application API request and never retries inference.
"""
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv

load_dotenv(ROOT / '.env')
logging.disable(logging.CRITICAL)
from httpx import ASGITransport, AsyncClient
from langsmith import Client, RunTree, tracing_context

from app.core.config import settings
from app.main import app
from app.schemas.behavior import BehaviorAnalysisRequest
from app.schemas.taste_profile import TasteProfileSchema
from app.services.behavior_evidence import ENVIRONMENT_TYPES
from app.services.behavior_statistics import build_behavior_statistics

OUT = Path(__file__).resolve().parent
PROJECT = os.getenv('LANGSMITH_PROJECT') or os.getenv('LANGCHAIN_PROJECT') or 'YEOLO-dev'


def payload():
    groups = [
        ('cafe', ['cafe', 'food'], 'summer', 6),
        ('restaurant', ['restaurant'], 'summer', 4),
        ('museum', ['museum'], 'summer', 3),
        ('beach', ['beach'], 'summer', 3),
        ('ski', ['ski_resort'], 'winter', 3),
        ('garden', ['botanical_garden'], 'spring', 3),
        ('nightlife', ['night_club'], 'summer', 3),
        ('shopping', ['shopping_mall', 'store'], 'summer', 3),
        ('wellness', ['spa'], 'summer', 3),
        ('fine_dining', ['fine_dining_restaurant'], 'summer', 1),
    ]
    items = []
    months = {'summer': 7, 'winter': 1, 'spring': 3}
    for name, types, season, count in groups:
        for index in range(count):
            captured = datetime(2026, months[season], index + 1, 23 if name == 'nightlife' else 10, tzinfo=timezone(timedelta(hours=9)))
            items.append({
                'sourceImageId': f'{name}-{index}',
                'location': {'country': 'South Korea', 'city': 'Seoul', 'region': 'Synthetic Region',
                             'district': 'Synthetic District', 'placeName': f'Synthetic {name} {index}',
                             'placeTypes': types},
                'timeContext': {'capturedAt': captured.isoformat(),
                                'dayOfWeek': ('mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun')[captured.weekday()],
                                'isWeekend': captured.weekday() >= 5,
                                'timeBucket': 'night' if name == 'nightlife' else 'morning', 'season': season},
            })
    for index in range(12):
        burst = json.loads(json.dumps(items[0]))
        burst['sourceImageId'] = f'burst-{index}'
        burst['timeContext']['capturedAt'] = (datetime(2026, 7, 1, 10, tzinfo=timezone(timedelta(hours=9))) + timedelta(minutes=index+1)).isoformat()
        items.append(burst)
    items.extend([json.loads(json.dumps(items[0])) for _ in range(3)])
    for name in ['invalid-time', 'unknown-place']:
        invalid = json.loads(json.dumps(items[0]))
        invalid['sourceImageId'] = name
        if name == 'invalid-time':
            invalid['timeContext']['capturedAt'] = 'invalid'
        else:
            invalid['location']['placeName'] = 'unknown'
        items.append(invalid)
    return {'userId': '550e8400-e29b-41d4-a716-446655440000', 'items': items}


def parse_events(body):
    rows = []
    for block in body.strip().split('\n\n'):
        kind = next((line[6:].strip() for line in block.splitlines() if line.startswith('event:')), None)
        data = '\n'.join(line[5:].strip() for line in block.splitlines() if line.startswith('data:'))
        if kind:
            rows.append({'event': kind, 'data': json.loads(data)})
    return rows


def save(name, value):
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2))


async def main():
    request = payload()
    save('request.json', request)
    expected_stats = build_behavior_statistics(BehaviorAnalysisRequest.model_validate(request).items)
    assert (expected_stats['inputPhotoCount'], expected_stats['duplicatePhotoCount'], expected_stats['invalidPhotoCount'], expected_stats['visitCount']) == (49, 3, 2, 32)
    start = time.monotonic()
    client = Client(timeout_ms=10000)
    root = RunTree(name='taste-full-profile-live-verification', run_type='chain', project_name=PROJECT,
                   inputs={'case': 'all_profile_groups', 'synthetic': True, 'photoCount': len(request['items'])},
                   tags=['taste-full-profile-verification', 'synthetic-data'], client=client)
    root.post()
    with tracing_context(parent=root, enabled=True, client=client, project_name=PROJECT):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://local-ai') as http:
            async with asyncio.timeout(120):
                response = await http.post('/internal/ai/taste-profile/analysis', json=request,
                                           headers={'X-Internal-Api-Key': settings.INTERNAL_API_KEY})
    api_seconds = round(time.monotonic()-start, 2)
    rows = parse_events(response.text)
    save('events.json', rows)
    complete = [r['data'] for r in rows if r['event'] == 'complete']
    assert response.status_code == 200 and [r['event'] for r in rows] == ['progress', 'complete']
    assert len(complete) == 1 and set(complete[0]) == {'tasteProfile'}
    profile = TasteProfileSchema.model_validate(complete[0]['tasteProfile'])
    root.end(outputs=complete[0])
    root.patch()
    await asyncio.to_thread(client.flush)
    runs = []
    for attempt in range(5):
        runs = await asyncio.to_thread(lambda: list(client.list_runs(project_name=PROJECT, trace_id=root.id)))
        llms = [r for r in runs if r.run_type == 'llm']
        evidence_runs = [r for r in runs if (r.extra or {}).get('metadata', {}).get('analysisMetadata')]
        if len(llms) == 1 and llms[0].end_time and len(evidence_runs) == 1:
            break
        await asyncio.sleep(2)
    assert len(llms) == 1 and not llms[0].error and llms[0].end_time
    assert len(evidence_runs) == 1
    meta = evidence_runs[0].extra['metadata']['analysisMetadata']
    stats = meta['statistics']
    checks = {
        'http_200': response.status_code == 200,
        'sse_progress_complete': [r['event'] for r in rows] == ['progress', 'complete'],
        'exact_public_api': set(complete[0]) == {'tasteProfile'},
        'all_eight_profile_groups': len(profile.model_dump()) == 8,
        'one_successful_remote_llm': len(llms) == 1 and not llms[0].error and bool(llms[0].end_time),
        'dedup_invalid_and_visit_grouping': stats == expected_stats,
        'burst_not_preference_evidence': stats['fieldVisitCounts']['foodPreference.cafeDessert'] == 6,
        'safe_enum_defaults': profile.companionType == 'solo' and profile.spendingTendency == 'moderate' and profile.travelPaceDensity == 'balanced',
        'enum_defaults_confirmation': all(k in meta['requiresUserConfirmation'] for k in ['companionType', 'spendingTendency', 'travelPaceDensity']),
        'all_supported_seasonal_activities': set(profile.seasonalEnvironmentPreference) == set(ENVIRONMENT_TYPES),
        'seasonal_minimum_and_no_duplicates': len(profile.seasonalEnvironmentPreference) >= 1 and len(profile.seasonalEnvironmentPreference) == len(set(profile.seasonalEnvironmentPreference)),
    }
    scores = []
    for group in ['travelPurpose', 'preferredLocationType', 'activityPreference', 'foodPreference']:
        for key, value in getattr(profile, group).model_dump().items():
            path = f'{group}.{key}'
            evidence = meta['fieldEvidence'][path]
            repeated = not evidence['insufficientEvidence']
            scores.append({'field': path, 'score': value, 'visits': evidence['evidence']['visitCount'],
                           'days': evidence['evidence']['distinctDayCount'], 'confidence': evidence['confidence']})
            checks[f'{path}.valid_and_guarded'] = isinstance(value, int) and 3 <= value <= 5 and (repeated or value == 3)
    checks['all_36_scores_checked'] = len(scores) == 36
    checks['sparse_fine_dining_neutral'] = profile.foodPreference.fineDining == 3
    checks['unobserved_dietary_neutral'] = profile.foodPreference.dietaryRestriction == 3
    checks['private_metadata_on_trace'] = bool(meta['fieldEvidence']) and 'analysisMetadata' not in complete[0]
    semantic_observations = {path: {'score': next(s['score'] for s in scores if s['field'] == path), 'expected': '4 or 5 for repeated observed activity'}
                             for path in ['foodPreference.cafeDessert', 'activityPreference.nightlife', 'travelPurpose.wellness', 'travelPurpose.culturalExperience', 'travelPurpose.shopping']}
    result = {'passed': all(checks.values()), 'model': settings.GEMINI_MODEL_NAME, 'api_seconds': api_seconds,
              'verification_seconds': round(time.monotonic()-start, 2), 'llm_run_count': len(llms),
              'total_tokens': llms[0].total_tokens, 'trace_url': client.get_run_url(run=root),
              'checks': checks, 'semantic_observations': semantic_observations, 'scores': scores,
              'tasteProfile': profile.model_dump(), 'internal_analysisMetadata': meta,
              'scope': 'Real full-profile single inference; operational/rule validation, not user-ground-truth accuracy.'}
    save('result.json', result)
    print(json.dumps({key: result[key] for key in ['passed', 'model', 'api_seconds', 'verification_seconds', 'llm_run_count', 'total_tokens', 'trace_url', 'tasteProfile', 'semantic_observations']}, ensure_ascii=False), flush=True)
    if not result['passed']:
        raise SystemExit(1)


asyncio.run(main())

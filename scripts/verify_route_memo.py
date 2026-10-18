"""Manually verify route memo translation against the configured live LLM.

Explicit --live is required. This uses diagnostic itinerary IDs and does not
query Google Maps, flights or lodging. Automated tests remain fully offline.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ['LANGSMITH_TRACING'] = 'false'
os.environ['LANGCHAIN_TRACING_V2'] = 'false'

EXAMPLES = {
    'china': {
        'names': ('雁荡路', '黄陂南路'),
        'memo': '雁荡路에서 南昌路 방면 북쪽으로 걷기. 우회전하여 南昌路에 진입. 좌회전하여 黄陂南路에 진입 목적지는 길의 왼쪽에 있습니다.',
    },
    'japan': {
        'names': ('劇場通り', 'カラオケパセラ池袋西口店'),
        'memo': '劇場通り에서 トキワ通り 방면 남쪽으로 걷기. カラオケパセラ池袋西口店(오른쪽) 지나서 좌회전. 좌회전.',
    },
}


async def run(case: str, language: str) -> None:
    """Print real model output and assert the final memo uses the requested script.

    Args:
        case: User-reported China or Japan navigation example.
        language: Target locale passed through the actual localization service.
    Raises:
        AssertionError: Gateway failure, mixed final script, or mutated route facts.
    """
    from app.agent.tools.verified_maps import InternalTransportSchema
    from app.schemas.course import CourseRequestSchema, CourseSchema
    from app.services.course_localization import localize_course
    from app.services.route_guidance import guidance_matches_language

    sample = EXAMPLES[case]
    request = CourseRequestSchema.model_validate({
        'userId': '550e8400-e29b-41d4-a716-446655440000', 'language': language, 'mbti': 'INTJ',
        'tripCondition': {'destinationCountry': '진단용', 'destinationCity': '진단용',
                          'startDate': '2027-01-10', 'totalDays': 1, 'budget': 100000},
    })
    course = CourseSchema.model_validate({
        'title': '메모 번역 진단', 'destinationCountry': '진단용', 'destinationCity': '진단용',
        'startDate': '2027-01-10', 'totalDays': 1, 'tags': [],
        'itinerary': {'days': [{'day': 1, 'date': '2027-01-10', 'stops': [{
            'sequence': index + 1, 'stayMinutes': 60, 'memo': '주변을 둘러보세요.', 'reason': '방문 장소입니다.', 'cost': 0,
            'place': {'placeId': f'diagnostic-{index}', 'placeName': name, 'category': 'SPOT', 'latitude': 0, 'longitude': 0},
            'transportToNext': {'type': 'none', 'distance': 0, 'minutes': 0, 'memo': None},
        } for index, name in enumerate(sample['names'])]}]},
    })
    first = course.itinerary.days[0].stops[0]
    first.transportToNext = InternalTransportSchema(
        type='walking', distance=700, minutes=12, cost=0, memo=sample['memo'],
        navigationSource=[{'kind': 'instruction', 'text': text + '.'}
                          for text in sample['memo'].rstrip('.').split('. ')],
    )
    original_send = httpx.AsyncClient.send
    calls = []

    async def observe(client, outgoing, *args, **kwargs):
        started = time.monotonic()
        response = await original_send(client, outgoing, *args, **kwargs)
        await response.aread()
        body = response.json()
        # Never print headers, credentials, or arbitrary gateway error bodies.
        calls.append({'status': response.status_code, 'seconds': round(time.monotonic() - started, 2),
                      'output': body.get('choices', [{}])[0].get('message', {}).get('content'),
                      'usage': body.get('usage')})
        return response

    started = time.monotonic()
    with patch.object(httpx.AsyncClient, 'send', observe):
        result = await localize_course(request, course)
    memo = result.itinerary.days[0].stops[0].transportToNext.memo
    print(json.dumps({'case': case, 'seconds': round(time.monotonic() - started, 2), 'input_memo': sample['memo'],
                      'final_memo': memo, 'place_names': [stop.place.placeName for stop in result.itinerary.days[0].stops],
                      'calls': calls}, ensure_ascii=False))
    assert calls and all(call['status'] == 200 for call in calls), 'Live Gateway invocation failed'
    assert guidance_matches_language(memo, language), 'Final memo retains foreign-script prose'
    assert all(guidance_matches_language(stop.place.placeName, language) for stop in result.itinerary.days[0].stops)
    assert (first.transportToNext.type, first.transportToNext.distance, first.transportToNext.minutes) == ('walking', 700, 12)
    assert [stop.place.placeId for stop in result.itinerary.days[0].stops] == ['diagnostic-0', 'diagnostic-1']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Authorize a paid LLM call using local server credentials')
    parser.add_argument('--case', choices=EXAMPLES, default='china')
    parser.add_argument('--language', default='ko')
    arguments = parser.parse_args()
    if not arguments.live:
        parser.error('--live is required; this command calls the real configured LLM')
    asyncio.run(run(arguments.case, arguments.language))

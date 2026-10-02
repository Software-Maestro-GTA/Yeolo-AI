"""Validate saved output or run one opt-in live request and verify its exact trace.

Usage: uv run python scripts/verify_course_output.py --request REQUEST --result RESULT
       uv run python scripts/verify_course_output.py --request REQUEST --live --output DIR
Live mode sends the chosen request to configured Gemini/Maps and LangSmith services.
Exit 0 means structural/schedule and (in live mode) trace checks passed, not optimal quality.
"""

import argparse
import asyncio
import json
import math
import os
import re
import sys
import tempfile
import time
from contextlib import aclosing
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def validate_output(request: dict, result: dict) -> dict:
    """Return errors and quality warnings without making network calls.

    Args:
        request: Original API request dictionary.
        result: Collected SSE result containing events, completed and course.
    Returns:
        Report with passed, errors, warnings, days and stops. Provider authenticity,
        future opening hours and subjective recommendation quality are not proven.
    """
    from pydantic import ValidationError

    from app.agent.tools.verified_maps import (
        individual_place_category_supported,
        meal_category_supported,
    )
    from app.schemas.course import CourseRequestSchema, CourseSchema

    errors: list[str] = []
    warnings: list[str] = []
    report = {'passed': False, 'errors': errors, 'warnings': warnings, 'days': 0, 'stops': 0}
    events = result.get('events', [])
    report['seconds'] = result.get('seconds')
    report['first_progress_seconds'] = next((event.get('seconds') for event in events if event.get('event') == 'progress'), None)
    names = [e.get('event') for e in events]
    if not result.get('completed') or names.count('complete') != 1 or names[-1:] != ['complete']:
        errors.append('Exactly one final complete event is required')
    for event in events:
        if event.get('event') not in {'progress', 'complete'}:
            errors.append('Unknown SSE event')
        if event.get('event') == 'progress' and (event.get('data', {}).get('step') != 'GENERATING_ROUTE' or not event.get('data', {}).get('message')):
            errors.append('Invalid progress payload')
    try:
        req = CourseRequestSchema.model_validate(request)
        course = CourseSchema.model_validate(result.get('course'))
        start = date.fromisoformat(req.tripCondition.startDate)
    except (ValidationError, ValueError) as exc:
        errors.append('Schema/calendar validation failed: ' + type(exc).__name__)
        return report
    trip = req.tripCondition
    for field in ['destinationCountry', 'destinationCity', 'startDate', 'totalDays']:
        if getattr(course, field) != getattr(trip, field):
            errors.append(f'Request mismatch: {field}')
    if not course.title.strip() or not course.recommendationReason.strip():
        errors.append('Empty title or recommendationReason')
    days = course.itinerary.days
    report['days'] = len(days)
    if len(days) != trip.totalDays:
        errors.append('Wrong number of days')
    seen: set[str] = set()
    malls = 0
    for index, day in enumerate(days, 1):
        prefix = f'Day {index}'
        if day.day != index or day.date != (start + timedelta(days=index-1)).isoformat():
            errors.append(f'{prefix}: day/date mismatch')
        if not day.stops:
            errors.append(f'{prefix}: no stops')
        meal_windows = {'lunch': False, 'dinner': False}
        for seq, stop in enumerate(day.stops, 1):
            report['stops'] += 1
            label = f'{prefix} stop {seq}'
            place = stop.place
            if stop.sequence != seq:
                errors.append(f'{label}: sequence mismatch')
            if not place.placeId.strip() or place.placeId in seen:
                errors.append(f'{label}: missing/duplicate place ID')
            seen.add(place.placeId)
            if not place.placeName.strip() or not place.address.strip():
                errors.append(f'{label}: missing place name/address')
            if not math.isfinite(place.latitude) or not -90 <= place.latitude <= 90 or not math.isfinite(place.longitude) or not -180 <= place.longitude <= 180:
                errors.append(f'{label}: invalid coordinates')
            category = place.category.lower()
            if not individual_place_category_supported(category):
                errors.append(f'{label}: invalid visit category {category}')
            malls += category == 'shopping_mall'
            if not stop.reason.strip():
                errors.append(f'{label}: empty reason')
            if '관심이 많은' in stop.reason:
                errors.append(f'{label}: disallowed reason phrase')
            mentioned_times = re.findall(r'(?<!\d)\d{2}:\d{2}(?!\d)', stop.reason)
            if any(value != stop.arrivalTime for value in mentioned_times):
                errors.append(f'{label}: reason arrival time disagrees with itinerary')
            hour, minute = map(int, stop.arrivalTime.split(':'))
            if hour > 23 or minute > 59:
                errors.append(f'{label}: invalid clock time')
                continue
            arrival = hour * 60 + minute
            departure = arrival + stop.stayMinutes
            if arrival < 540 or departure > 1260:
                errors.append(f'{label}: outside 09:00–21:00')
            meal_windows['lunch'] |= meal_category_supported(category, 'lunch') and 690 <= arrival and departure <= 840
            meal_windows['dinner'] |= meal_category_supported(category, 'dinner') and 1050 <= arrival and departure <= 1200
            route = stop.transportToNext
            if seq == len(day.stops):
                if route.type != 'none' or route.minutes not in {None, 0} or route.distance not in {None, 0}:
                    errors.append(f'{label}: invalid final route')
            else:
                if route.type == 'none' or route.minutes is None or route.minutes <= 0 or route.distance is None or not math.isfinite(route.distance) or route.distance <= 0:
                    errors.append(f'{label}: invalid route')
                else:
                    nxt_hour, nxt_minute = map(int, day.stops[seq].arrivalTime.split(':'))
                    gap = nxt_hour * 60 + nxt_minute - departure - route.minutes - 10
                    if gap < 0:
                        errors.append(f'{label}: schedule overlap including travel/buffer')
                    elif gap > 120:
                        warnings.append(f'{label}: {gap} unallocated minutes before next stop')
        for meal, present in meal_windows.items():
            if not present:
                errors.append(f'{prefix}: no verified meal category fitting {meal} window')
    if malls > 1:
        warnings.append(f'{malls} shopping malls; review against user preferences')
    for trace in result.get('local_traces', []):
        if trace.get('error') or not trace.get('ended'):
            errors.append('Local graph trace did not end successfully')
    report['passed'] = not errors
    return report


async def run_live(request: dict, output: Path) -> dict:
    """Execute the service with isolated history; preserve output and exact run status."""
    from dotenv import load_dotenv
    load_dotenv(ROOT / '.env')
    from langchain_core.tracers.context import collect_runs, tracing_v2_enabled
    from langsmith import Client

    from app.schemas.course import CourseRequestSchema

    client = Client()
    started = time.monotonic()
    result: dict[str, Any] = {'completed': False, 'events': [], 'course': None, 'local_traces': []}
    with tempfile.TemporaryDirectory(prefix='yeolo-verify-') as temp:
        os.environ['COURSE_HISTORY_PATH'] = str(Path(temp) / 'history.sqlite3')
        from app.core.config import settings
        from app.services.course_service import generate_course_service
        result['model'] = settings.GEMINI_MODEL_NAME
        # Callback context is inherited by the producer task without patching production code.
        with collect_runs() as collector, tracing_v2_enabled(project_name=os.getenv('LANGSMITH_PROJECT'), client=client, tags=['course-output-verification']):
            try:
                async with asyncio.timeout(150):
                    async with aclosing(await generate_course_service(CourseRequestSchema.model_validate(request))) as stream:
                        async for raw in stream:
                            if raw.startswith(':'):
                                continue
                            lines = raw.splitlines()
                            event = next(line[6:].strip() for line in lines if line.startswith('event:'))
                            data = json.loads('\n'.join(line[5:].strip() for line in lines if line.startswith('data:')))
                            result['events'].append({'event': event, 'seconds': round(time.monotonic()-started, 3), 'data': data if event != 'complete' else {}})
                            print(json.dumps({'event': event, 'seconds': result['events'][-1]['seconds'], 'message': data.get('message', '')}, ensure_ascii=False), flush=True)
                            if event == 'complete':
                                result['course'] = data['course']
                                result['completed'] = True
                                break  # Exercise immediate consumer close after completion.
            except Exception as exc:  # noqa: BLE001 - save failure report without credential-bearing errors
                result['error_type'] = type(exc).__name__
        roots = [run for run in collector.traced_runs if run.name == 'LangGraph' and run.parent_run_id is None]
        result['local_traces'] = [{'run_id': str(run.id), 'ended': bool(run.end_time), 'error': bool(run.error)} for run in roots]
    result['seconds'] = round(time.monotonic()-started, 3)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    report = validate_output(request, result)
    if len(roots) != 1:
        report['errors'].append(f'Expected one graph root, found {len(roots)}')
    remote = {'status': 'unverified'}
    try:
        await asyncio.to_thread(client.flush, timeout=15)
        if len(roots) == 1:
            for attempt in range(5):
                try:
                    run = await asyncio.to_thread(client.read_run, roots[0].id)
                    remote = {'run_id': str(run.id), 'ended': bool(run.end_time), 'error': bool(run.error), 'status': run.status, 'url': run.url}
                    if run.end_time:
                        break
                except Exception as exc:  # noqa: BLE001 - bounded indexing delay, no sensitive error dump
                    remote['query_error_type'] = type(exc).__name__
                if attempt < 4:
                    await asyncio.sleep(2)
    except Exception as exc:  # noqa: BLE001 - unavailable tracing is a failed verification, not a pass
        remote['query_error_type'] = type(exc).__name__
    report['langsmith'] = remote
    if remote.get('status') != 'success' or not remote.get('ended') or remote.get('error'):
        report['errors'].append('Exact LangSmith root success was not confirmed')
    report['passed'] = not report['errors']
    report['seconds'] = result['seconds']
    return report


def main() -> None:
    """Run offline validation by default; require --live for billable external calls."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', required=True, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--result', type=Path)
    mode.add_argument('--live', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/course-output-verification')
    args = parser.parse_args()
    request = json.loads(args.request.read_text())
    report = asyncio.run(run_live(request, args.output)) if args.live else validate_output(request, json.loads(args.result.read_text()))
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report['passed'] else 1)


if __name__ == '__main__':
    main()

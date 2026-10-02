"""Validate course outputs offline, separating errors from quality warnings."""

import copy
import json
from datetime import date, timedelta

import pytest


@pytest.fixture
def archived_output():
    """Build deterministic, synthetic output without depending on live artifacts."""
    request = {
        'userId': '550e8400-e29b-41d4-a716-446655440000', 'mbti': 'ENFP',
        'tripCondition': {'destinationCountry': '대한민국', 'destinationCity': '서울',
                          'startDate': '2026-10-05', 'totalDays': 3, 'budgetType': 'moderate'},
    }
    days = []
    for index in range(3):
        stops = []
        for sequence, (category, arrival) in enumerate([
            ('museum', '09:00'), ('restaurant', '11:30'),
            ('shopping_mall', '13:00'), ('restaurant', '17:30'),
        ], 1):
            last = sequence == 4
            stops.append({
                'sequence': sequence, 'arrivalTime': arrival, 'stayMinutes': 60,
                'memo': '합성 테스트 장소', 'reason': '테스트 입력과 일정에 맞춘 방문', 'cost': 10000,
                'place': {'placeId': f'synthetic-{index}-{sequence}', 'placeName': f'테스트 장소 {index}-{sequence}',
                          'category': category, 'address': '대한민국 서울', 'latitude': 37.55, 'longitude': 126.98},
                'transportToNext': {'type': 'none' if last else 'walking',
                                    'minutes': 0 if last else 10, 'distance': 0 if last else 800, 'cost': 0},
            })
        days.append({'day': index + 1, 'date': (date(2026, 10, 5) + timedelta(days=index)).isoformat(),
                     'memo': '합성 테스트 일정', 'stops': stops})
    course = {**request['tripCondition'], 'title': '합성 서울 3일 코스',
              'recommendationReason': '검증기 테스트용으로 고정한 일정', 'itinerary': {'days': days}}
    course.pop('budgetType')
    result = {'completed': True, 'events': [
        {'event': 'progress', 'data': {'step': 'GENERATING_ROUTE', 'message': '합성 테스트'}},
        {'event': 'complete', 'data': {}},
    ], 'course': course}
    return request, result


def validate(request, result):
    from scripts.verify_course_output import validate_output

    report = validate_output(request, result)
    assert isinstance(report['passed'], bool)
    assert isinstance(report['errors'], list)
    assert isinstance(report['warnings'], list)
    assert isinstance(report['days'], int)
    assert isinstance(report['stops'], int)
    return report


def test_synthetic_successful_three_day_output_passes(archived_output):
    request, result = archived_output
    report = validate(request, result)
    assert report['passed']
    assert report['errors'] == []
    assert report['days'] == 3
    assert report['stops'] == sum(len(day['stops']) for day in result['course']['itinerary']['days'])


@pytest.mark.parametrize('missing', ['event', 'completed_flag', 'course'])
def test_missing_completion_evidence_fails(archived_output, missing):
    request, result = archived_output
    if missing == 'event':
        result['events'] = [event for event in result['events'] if event['event'] != 'complete']
    elif missing == 'completed_flag':
        result['completed'] = False
    else:
        result['course'] = None
    report = validate(request, result)
    assert not report['passed']
    assert report['errors']


@pytest.mark.parametrize('mismatch', ['course_total', 'course_start', 'day_number', 'day_date', 'day_count'])
def test_trip_dates_and_day_count_must_match_request(archived_output, mismatch):
    request, result = archived_output
    course = result['course']
    if mismatch == 'course_total':
        course['totalDays'] = 2
    elif mismatch == 'course_start':
        course['startDate'] = '2027-01-01'
    elif mismatch == 'day_number':
        course['itinerary']['days'][1]['day'] = 1
    elif mismatch == 'day_date':
        course['itinerary']['days'][1]['date'] = course['itinerary']['days'][0]['date']
    else:
        course['itinerary']['days'].pop()
    report = validate(request, result)
    assert not report['passed']
    assert report['errors']


def test_duplicate_place_across_days_fails(archived_output):
    request, result = archived_output
    days = result['course']['itinerary']['days']
    days[1]['stops'][0]['place']['placeId'] = days[0]['stops'][0]['place']['placeId']
    report = validate(request, result)
    assert not report['passed']
    assert report['errors']


@pytest.mark.parametrize('buffer_minutes', [0, 9])
def test_adjacent_visits_require_route_and_full_ten_minute_buffer(archived_output, buffer_minutes):
    request, result = archived_output
    first, following = result['course']['itinerary']['days'][0]['stops'][:2]
    hour, minute = map(int, first['arrivalTime'].split(':'))
    too_early = hour * 60 + minute + first['stayMinutes'] + first['transportToNext']['minutes'] + buffer_minutes
    following['arrivalTime'] = f'{too_early // 60:02d}:{too_early % 60:02d}'
    report = validate(request, result)
    assert not report['passed']
    assert report['errors']


@pytest.mark.parametrize(('field', 'invalid'), [('type', 'none'), ('minutes', None), ('minutes', -1), ('minutes', 0), ('distance', None), ('distance', -1), ('distance', 0)])
def test_intermediate_route_requires_confirmed_positive_metrics(archived_output, field, invalid):
    request, result = archived_output
    result['course']['itinerary']['days'][0]['stops'][0]['transportToNext'][field] = invalid
    report = validate(request, result)
    assert not report['passed']
    assert report['errors']


@pytest.mark.parametrize('reason_field', ['stop', 'course'])
def test_empty_recommendation_reason_fails(archived_output, reason_field):
    request, result = archived_output
    if reason_field == 'stop':
        result['course']['itinerary']['days'][0]['stops'][0]['reason'] = '   '
    else:
        result['course']['recommendationReason'] = ''
    report = validate(request, result)
    assert not report['passed']
    assert report['errors']


def test_shopping_and_long_gaps_are_quality_warnings_not_invalid_output(archived_output):
    request, result = archived_output
    # Preserve valid synthetic data and meal windows; this fixture includes
    # shopping malls and a long idle interval before the 17:30 dinner.
    categories = [stop['place']['category'] for day in result['course']['itinerary']['days'] for stop in day['stops']]
    assert 'shopping_mall' in categories
    report = validate(request, result)
    assert report['passed']
    assert report['errors'] == []
    assert len(report['warnings']) >= 2


@pytest.mark.parametrize(('ended', 'error', 'expected'), [(True, None, True), (False, None, False), (True, 'CancelledError', False)])
def test_supplied_local_root_traces_must_end_without_error(archived_output, ended, error, expected):
    request, result = archived_output
    result['local_traces'] = [{'ended': True, 'error': None}, {'ended': ended, 'error': error}]
    report = validate(request, result)
    assert report['passed'] is expected
    assert bool(report['errors']) is (not expected)


def test_validation_does_not_modify_archived_output(archived_output):
    request, result = archived_output
    original = copy.deepcopy((request, result))
    validate(request, result)
    assert (request, result) == original


@pytest.mark.parametrize('invalid_event', ['duplicate_complete', 'event_after_complete', 'invalid_step'])
def test_invalid_sse_completion_or_progress_contract_fails(archived_output, invalid_event):
    request, result = archived_output
    if invalid_event == 'duplicate_complete':
        result['events'].append({'event': 'complete', 'data': {}})
    elif invalid_event == 'event_after_complete':
        result['events'].append({'event': 'progress', 'data': {'step': 'GENERATING_ROUTE', 'message': 'late'}})
    else:
        result['events'][0]['data']['step'] = 'UNKNOWN_STAGE'
    report = validate(request, result)
    assert not report['passed']
    assert report['errors']


@pytest.mark.parametrize('category', ['market', 'dessert_restaurant', 'brunch_restaurant'])
def test_non_dinner_category_cannot_satisfy_dinner_window(archived_output, category):
    request, result = archived_output
    result['course']['itinerary']['days'][1]['stops'][-1]['place']['category'] = category
    report = validate(request, result)
    assert not report['passed']
    assert report['errors']


@pytest.mark.parametrize('category', ['neighborhood', 'subway_station'])
def test_invalid_area_or_transit_output_is_rejected(archived_output, category):
    request, result = archived_output
    result['course']['itinerary']['days'][0]['stops'][0]['place']['category'] = category
    report = validate(request, result)
    assert not report['passed']
    assert report['errors']


@pytest.mark.asyncio
@pytest.mark.parametrize('remote_available', [True, False])
async def test_live_wrapper_collects_real_graph_and_checks_exact_remote_root(archived_output, tmp_path, mocker, remote_available):
    """Run real local collection and mock only service providers and remote tracing."""
    from contextlib import nullcontext
    from types import SimpleNamespace

    from langgraph.graph import END, START, StateGraph

    from scripts.verify_course_output import run_live

    request, result = archived_output
    course = result['course']
    closed = []
    builder = StateGraph(dict)
    builder.add_node('finish', lambda state: {'course': course})
    builder.add_edge(START, 'finish')
    builder.add_edge('finish', END)
    graph = builder.compile()

    async def service(request):
        async def stream():
            try:
                yield 'event: progress\ndata: ' + json.dumps({'step': 'GENERATING_ROUTE', 'message': '진행 중'}) + '\n\n'
                output = await graph.ainvoke({})
                yield 'event: complete\ndata: ' + json.dumps(output) + '\n\n'
            finally:
                closed.append(True)
        return stream()

    remote_client = mocker.Mock()
    remote_client.flush.return_value = None
    if remote_available:
        remote_client.read_run.side_effect = lambda run_id: SimpleNamespace(
            id=run_id, end_time='2026-10-01T00:00:00Z', error=None,
            status='success', url=f'https://example.test/run/{run_id}',
        )
    else:
        remote_client.read_run.side_effect = RuntimeError('Remote trace unavailable')
    mocker.patch('app.services.course_service.generate_course_service', side_effect=service)
    mocker.patch('langsmith.Client', return_value=remote_client)
    mocker.patch('langchain_core.tracers.context.tracing_v2_enabled', side_effect=lambda **kwargs: nullcontext())
    mocker.patch('dotenv.load_dotenv', return_value=False)
    mocker.patch('scripts.verify_course_output.asyncio.sleep', new_callable=mocker.AsyncMock)
    report = await run_live(request, tmp_path)
    saved = json.loads((tmp_path / 'result.json').read_text())
    assert closed == [True]  # run_live closes its service immediately after complete.
    assert saved['completed']
    assert len(saved['local_traces']) == 1
    trace = saved['local_traces'][0]
    assert trace['ended'] and not trace['error']
    assert remote_client.read_run.call_count >= 1
    assert all(str(call.args[0]) == trace['run_id'] for call in remote_client.read_run.call_args_list)
    assert report['passed'] is remote_available
    if remote_available:
        assert report['errors'] == []
        assert report['langsmith']['run_id'] == trace['run_id']
    else:
        assert report['errors']
        assert report['langsmith']['status'] != 'success'


def test_explicit_formula_estimate_passes_with_quality_warning(archived_output):
    from app.agent.tools.verified_maps import VerifiedPlace
    from app.schemas.course import PlaceSchema
    from app.services.course_routing import estimated_walking

    request, result = archived_output
    first, following = result['course']['itinerary']['days'][0]['stops'][:2]
    following['place']['latitude'] = 37.551
    route = estimated_walking(VerifiedPlace(PlaceSchema.model_validate(first['place'])), VerifiedPlace(PlaceSchema.model_validate(following['place'])))
    first['transportToNext'] = route.model_dump()
    report = validate(request, result)
    assert report['passed']
    assert any('추정' in warning for warning in report['warnings'])


@pytest.mark.parametrize('invalid', ['arbitrary_time', 'too_far', 'transit'])
def test_estimate_marker_does_not_accept_arbitrary_unverified_metrics(archived_output, invalid):
    from app.agent.tools.verified_maps import VerifiedPlace
    from app.schemas.course import PlaceSchema
    from app.services.course_routing import estimated_walking

    request, result = archived_output
    first, following = result['course']['itinerary']['days'][0]['stops'][:2]
    following['place']['latitude'] = 37.551
    route = estimated_walking(VerifiedPlace(PlaceSchema.model_validate(first['place'])), VerifiedPlace(PlaceSchema.model_validate(following['place']))).model_dump()
    if invalid == 'arbitrary_time':
        route['minutes'] = 1
    elif invalid == 'too_far':
        following['place']['latitude'] = 37.60
    else:
        route['type'] = 'transit'
    first['transportToNext'] = route
    assert not validate(request, result)['passed']

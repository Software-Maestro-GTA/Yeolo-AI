"""Exercise split graph nodes and safe failure diagnostics through the real SDK."""

import json

import httpx
import pytest
from langchain_core.callbacks import AsyncCallbackHandler

from app.agent.course_graph import build_course_graph
from app.services.course_place_copy import write_place_copy
from tests.test_course_graph import graph_dependencies as _graph_dependencies
from tests.test_course_graph import request_data as _request_data
from tests.test_course_place_copy import completion, facts, reviewed
from tests.test_course_place_copy import wire as _wire

graph_dependencies = _graph_dependencies
request_data = _request_data
wire = _wire


class ChainNames(AsyncCallbackHandler):
    """Collect local trace names/metadata without sending anything to LangSmith."""

    def __init__(self):
        self.starts = []

    async def on_chain_start(self, serialized, inputs, *, run_id, **kwargs):
        self.starts.append((kwargs.get('name'), kwargs.get('metadata', {})))


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [
    None, 'no_translation_needed', 'translation', 'translation_repair', 'translation_recovered',
    'writing', 'writing_retry', 'review', 'validation', 'grounding',
])
async def test_split_nodes_keep_optional_failures_visible_without_changing_public_course(
    request_data, graph_dependencies, wire, mocker, caplog, failure,
):
    provider, history, _, _, _ = graph_dependencies
    provider.route.return_value.memo = '길을 따라 이동하세요.' if failure == 'no_translation_needed' else 'Walk along the road.'
    mocker.patch('app.agent.course_nodes.write_place_copy', side_effect=write_place_copy)
    writers = 0
    calls = []

    def outage():
        return httpx.Response(503, json={'error': {'message': 'private-provider-key private response', 'type': 'server_error'}})

    def respond(request):
        nonlocal writers
        calls.append(request)
        body = json.loads(request.content)
        schema = body['response_format']['json_schema']['name']
        content = body['messages'][-1]['content']
        if schema == 'NavigationCopy':
            if failure == 'translation_recovered' and len(calls) == 1:
                return completion({'navigation': []})
            if failure in {'translation', 'translation_repair'}:
                if failure == 'translation_repair' and len(calls) == 1:
                    return completion({'navigation': []})
                return outage()
            source = json.loads(content.split('Verified source copy:\n')[1])
            return completion({'navigation': [{'id': unit['id'], 'text': '길을 따라 이동하세요.'}
                                               for unit in source['navigation']]})
        source = json.loads(content.split('Evidence:\n')[1].split('\nProposed copy:')[0])
        if schema == 'PlaceCopyBatch':
            writers += 1
            if failure == 'writing' or (failure == 'writing_retry' and writers == 1):
                return outage()
        elif schema != 'CopyReview':
            pytest.fail(f'Unexpected model schema: {schema}')
        output = {'stops': [{
            'slot': unit['slot'],
            'reason': f'{"가나다라마바사아자차카타파"[index]} 방문 경험을 일정에 더했어요.',
            'memo': f'{"가나다라마바사아자차카타파"[index]} 주제를 골라 현장 안내를 살펴보세요.',
        } for index, unit in enumerate(source)]}
        if schema == 'CopyReview':
            if failure == 'review':
                return outage()
            review = reviewed(output)
            if failure == 'grounding':
                for item in review['stops']:
                    item['reason_supported'] = False
            return completion(review)
        if failure == 'validation':
            for item in output['stops']:
                item['memo'] = '입장료는 1000원이에요.'
        return completion(output)

    _, clients = wire(respond)
    recorder = ChainNames()
    updates = []
    translated_facts = None
    async for update in build_course_graph(provider, history).astream(
        {'request': request_data, 'attempt': 0}, stream_mode='updates', config={'callbacks': [recorder]},
    ):
        updates.append(update)
        if 'translate_display' in update:
            translated_facts = facts(update['translate_display']['course'])
    names = [name for update in updates for name in update]
    assert names.index('translate_display') < names.index('generate_place_copy') < names.index('finalize_course')
    translated = next(update['translate_display'] for update in updates if 'translate_display' in update)
    generated = next(update['generate_place_copy'] for update in updates if 'generate_place_copy' in update)
    assert set(translated['display_diagnostics']) == {'translation'}
    assert set(generated['display_diagnostics']) == {'translation', 'place_copy'}
    translation = generated['display_diagnostics']['translation']
    copy = generated['display_diagnostics']['place_copy']
    expected_translation = ('skipped' if failure == 'no_translation_needed' else
                            'failed' if failure in {'translation', 'translation_repair'} else 'success')
    assert translation['status'] == expected_translation
    assert translation['retries'] == int(failure in {'translation_repair', 'translation_recovered'})
    copy_failed = failure in {'writing', 'review', 'validation', 'grounding'}
    expected_copy = 'partial' if failure in {'validation', 'grounding'} else 'failed' if copy_failed else 'success'
    assert copy['status'] == expected_copy
    assert (copy['total'], copy['completed'], copy['missing']) == (5, 0 if copy_failed else 5, 5 if copy_failed else 0)
    assert copy['retries'] == int(copy_failed or failure == 'writing_retry')
    if failure and failure != 'no_translation_needed':
        phase, reason = {
            'translation': ('display_translation', 'exception'),
            'translation_repair': ('display_translation_repair', 'exception'),
            'translation_recovered': ('translation_validation', 'untranslated_or_rejected'),
            'writing': ('place_copy_write', 'exception'),
            'writing_retry': ('place_copy_write', 'exception'),
            'review': ('place_copy_review', 'exception'),
            'validation': ('place_copy_validation', 'invalid_text'),
            'grounding': ('place_copy_review', 'unsupported'),
        }[failure]
        report = translation if failure.startswith('translation') else copy
        errors = [item for item in report['failures'] if item['phase'] == phase and item['reason'] == reason]
        assert errors
        if reason == 'exception':
            assert errors[0]['status_code'] == 503 and errors[0]['error_type']
        if phase in {'place_copy_write', 'place_copy_review'}:
            assert (errors[0]['day'], errors[0]['batch'], errors[0]['attempt']) == (1, 1, 1)
    chain_names = [name for name, _ in recorder.starts]
    assert ('course_display_translation' in chain_names) == (failure != 'no_translation_needed')
    assert 'course_place_copy_write' in chain_names
    if failure != 'writing':
        assert 'course_place_copy_review' in chain_names
    if failure in {'translation_repair', 'translation_recovered'}:
        assert 'course_display_translation_repair' in chain_names
    writer_metadata = next(meta for name, meta in recorder.starts if name == 'course_place_copy_write')
    assert {key: writer_metadata[key] for key in ('display_stage', 'day', 'batch', 'attempt')} == {
        'display_stage': 'place_copy', 'day': 1, 'batch': 1, 'attempt': 1,
    }
    final = updates[-1]['finalize_course']['course']
    if failure in {'validation', 'grounding'}:
        retained_field = 'reason' if failure == 'validation' else 'memo'
        rejected_field = 'memo' if failure == 'validation' else 'reason'
        assert all(getattr(stop, retained_field) and not getattr(stop, rejected_field)
                   for day in final.itinerary.days for stop in day.stops)
    assert facts(final) == translated_facts
    assert 'display_diagnostics' not in final.model_dump(context={'public_response': True})
    assert all(client.is_closed for client in clients)
    diagnostic_text = json.dumps(generated['display_diagnostics'], ensure_ascii=False)
    assert 'private-provider-key' not in diagnostic_text + caplog.text
    assert 'private response' not in diagnostic_text + caplog.text
    assert str(request_data.userId) not in diagnostic_text

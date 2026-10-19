"""Verify optional trip anchors share existing daily model calls and survive repair."""

from collections import Counter

import pytest
from pydantic import ValidationError

from app.agent.course_drafting import draft_candidates
from app.agent.course_state import Candidate, CourseDraft, DraftDay, PartialDraftError
from app.schemas.course import CourseRequestSchema
from app.schemas.course_anchors import AirportCandidate, AnchorCandidates


@pytest.fixture
def anchor_request():
    return CourseRequestSchema.model_validate({
        'userId': '550e8400-e29b-41d4-a716-446655440000', 'language': 'ko', 'mbti': 'INTJ',
        'tripCondition': {'destinationCountry': '일본', 'destinationCity': '도쿄',
                          'startDate': '2026-10-17', 'totalDays': 3, 'budget': 900000},
    })


def anchors(airport='HND', lodging='검증할 호텔'):
    return AnchorCandidates.model_validate({
        'airports': [{'name': '하네다 공항', 'english_name': 'Haneda Airport', 'iata_code': airport}],
        'lodgings': [{'name': lodging, 'english_name': 'Unverified Hotel'}],
    })


def day_draft(index, proposed=None):
    return CourseDraft(
        title='도쿄 여행', reason='',
        days=[DraftDay(candidates=[Candidate(name=f'{index}-{slot}') for slot in range(9)])],
        anchor_candidates=proposed or AnchorCandidates(),
    )


def test_malformed_optional_anchors_keep_valid_siblings_and_required_visits():
    raw = day_draft(0).model_dump()
    raw['anchor_candidates'] = {
        'airports': [None, {'name': '공항', 'iata_code': 'invalid'},
                     {'name': ' 하네다 공항 ', 'iata_code': 'HND'},
                     {'name': '중복 공항', 'iata_code': 'HND'},
                     {'name': '나리타 공항', 'iata_code': 'NRT'},
                     {'name': '다른 공항', 'iata_code': 'KIX'}],
        'lodgings': ['잘못된 항목', {'name': ' '}, {'name': 123},
                     {'name': ' 실제 호텔 ', 'english_name': ' Verified Hotel '}],
    }
    result = CourseDraft.model_validate(raw)
    assert len(result.days[0].candidates) == 9
    assert [item.iata_code for item in result.anchor_candidates.airports] == ['HND', 'NRT']
    assert [item.name for item in result.anchor_candidates.lodgings] == ['실제 호텔']
    assert result.anchor_candidates.lodgings[0].english_name == 'Verified Hotel'
    raw['anchor_candidates'] = 'malformed optional block'
    assert CourseDraft.model_validate(raw).anchor_candidates == AnchorCandidates()


def test_airport_code_whitespace_and_case_normalize_before_validation_and_deduplication():
    """Harmless model formatting must not drop a real candidate or use both slots."""
    candidate = AirportCandidate(name='하네다 공항', iata_code=' hnd ')
    assert candidate.iata_code == 'HND'
    result = AnchorCandidates.model_validate({'airports': [
        {'name': '하네다 공항', 'iata_code': ' hnd '},
        {'name': 'Haneda Airport', 'iata_code': 'HND'},
        {'name': '나리타 공항', 'iata_code': 'nrt'},
    ]})
    assert [airport.iata_code for airport in result.airports] == ['HND', 'NRT']


@pytest.mark.parametrize('invalid_code', [123, 'HND-NRT', '東京'])
def test_airport_code_normalization_does_not_invent_missing_or_invalid_codes(invalid_code):
    """Normalization only fixes case and padding, not invalid airport identifiers."""
    with pytest.raises(ValidationError):
        AirportCandidate(name='검증 전 공항', iata_code=invalid_code)
    result = AnchorCandidates.model_validate({
        'airports': [{'name': '검증 전 공항', 'iata_code': invalid_code}],
    })
    assert result.airports == []


@pytest.mark.asyncio
async def test_only_one_existing_day_call_generates_trip_anchors(mocker, anchor_request):
    calls = []

    async def generate(request, recent_ids, feedback='', *, day_index, **kwargs):
        calls.append((day_index, kwargs))
        # Stray proposals from unassigned days must not overwrite the assigned response.
        return day_draft(day_index, anchors(lodging=f'호텔 {day_index}'))

    mocker.patch('app.agent.course_drafting._draft_day_candidates', side_effect=generate)
    result = await draft_candidates(anchor_request, [], airports_required=True)
    assert len(calls) == 3
    assert [index for index, kwargs in calls if kwargs['generate_airports']] == [0]
    assert [index for index, kwargs in calls if kwargs['generate_lodgings']] == [0]
    assert result.anchor_candidates == anchors(lodging='호텔 0')
    assert [len(day.candidates) for day in result.days] == [9, 9, 9]


@pytest.mark.asyncio
async def test_partial_failure_retains_anchors_and_repair_does_not_regenerate(mocker, anchor_request):
    calls = Counter()
    assignments = []

    async def generate(request, recent_ids, feedback='', *, day_index, **kwargs):
        calls[day_index] += 1
        assignments.append((day_index, kwargs['generate_airports'], kwargs['generate_lodgings']))
        if day_index == 1 and calls[day_index] <= 2:
            raise TimeoutError('temporary model timeout')
        return day_draft(day_index, anchors())

    mocker.patch('app.agent.course_drafting._draft_day_candidates', side_effect=generate)
    with pytest.raises(PartialDraftError) as raised:
        await draft_candidates(anchor_request, [], airports_required=True)
    partial = raised.value
    assert set(partial.days) == {0, 2}
    assert partial.anchor_candidates == anchors()
    result = await draft_candidates(
        anchor_request, [], pending_days=[1], existing_days=partial.days,
        existing_anchors=partial.anchor_candidates, airports_required=True,
    )
    assert calls == Counter({0: 1, 1: 3, 2: 1})
    assert assignments[-1] == (1, False, False)
    assert result.anchor_candidates == anchors()
    assert len(result.days) == 3


@pytest.mark.asyncio
async def test_missing_anchor_type_uses_one_repair_call_without_replacing_retained_type(mocker, anchor_request):
    retained = AnchorCandidates(airports=anchors().airports)
    proposed = anchors(airport='NRT', lodging='새 숙소')
    boundary = mocker.patch('app.agent.course_drafting._draft_day_candidates', return_value=day_draft(2, proposed))
    result = await draft_candidates(
        anchor_request, [], pending_days=[2],
        existing_days={index: day_draft(index).days[0] for index in (0, 1)},
        existing_anchors=retained, airports_required=True,
    )
    boundary.assert_awaited_once()
    assert boundary.call_args.kwargs['generate_airports'] is False
    assert boundary.call_args.kwargs['generate_lodgings'] is True
    assert result.anchor_candidates.airports == retained.airports
    assert result.anchor_candidates.lodgings == proposed.lodgings


@pytest.mark.asyncio
@pytest.mark.parametrize(('total_days', 'airports_required'), [(1, False), (1, True), (3, False)])
async def test_inapplicable_anchors_are_ignored_even_when_model_returns_them(
    mocker, anchor_request, total_days, airports_required,
):
    anchor_request.tripCondition.totalDays = total_days
    calls = []

    async def generate(request, recent_ids, feedback='', *, day_index, **kwargs):
        calls.append(kwargs)
        return day_draft(day_index, anchors())

    mocker.patch('app.agent.course_drafting._draft_day_candidates', side_effect=generate)
    result = await draft_candidates(anchor_request, [], airports_required=airports_required)
    assert bool(result.anchor_candidates.airports) is airports_required
    assert bool(result.anchor_candidates.lodgings) is (total_days > 1)
    assert sum(item['generate_airports'] for item in calls) == int(airports_required)
    assert sum(item['generate_lodgings'] for item in calls) == int(total_days > 1)

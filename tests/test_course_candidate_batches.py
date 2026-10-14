"""Verify bounded place-query batches keep meal coverage and deferred options."""

from dataclasses import dataclass

import pytest

from app.services.course_candidate_batches import (
    candidate_batch,
    candidate_query_key,
)


@dataclass
class Proposal:
    name: str
    english_name: str = ''
    meal: str = 'none'


def proposals() -> list[Proposal]:
    """Meals late in the model list must survive the initial query budget."""
    return [
        Proposal('A'), Proposal('B'), Proposal('C'), Proposal('D'), Proposal('E'),
        Proposal('Breakfast', meal='breakfast'),
        Proposal('Lunch', meal='lunch'), Proposal('Dinner', meal='dinner'),
        Proposal('F'),
    ]


@pytest.mark.parametrize(('limit', 'expected'), [
    (5, ['A', 'B', 'C', 'Lunch', 'Dinner']),
    (6, ['A', 'B', 'C', 'D', 'Lunch', 'Dinner']),
    (7, ['A', 'B', 'C', 'D', 'E', 'Lunch', 'Dinner']),
])
def test_initial_queries_cover_three_nonmeals_and_lunch_dinner_in_model_order(limit, expected):
    source = proposals()
    before = list(source)
    known = set()
    selected = candidate_batch(source, known, limit)
    assert [candidate.name for candidate in selected] == expected
    assert source == before and not known
    assert all(any(candidate is original for original in source) for candidate in selected)


def test_deficient_day_expands_with_two_remaining_candidates_without_losing_deferred_proposals():
    source = proposals()
    initial = candidate_batch(source, set(), 6)
    known = {candidate_query_key(candidate) for candidate in initial}
    expanded = candidate_batch(source, known, 2, prefer_meal_balance=False)
    assert [candidate.name for candidate in expanded] == ['E', 'Breakfast']
    assert len(source) == 9
    after = known | {candidate_query_key(candidate) for candidate in expanded}
    assert [candidate.name for candidate in candidate_batch(source, after, 2, prefer_meal_balance=False)] == ['F']


def test_duplicate_keys_use_first_proposal_and_known_successes_are_skipped():
    first = Proposal(' Museum ', 'MUSEUM')
    duplicate = Proposal('museum', ' museum ')
    lunch = Proposal('Museum', 'Museum', 'lunch')
    remaining = Proposal('Park')
    assert candidate_query_key(first) == candidate_query_key(duplicate)
    assert candidate_query_key(first) != candidate_query_key(lunch)
    source = [first, duplicate, lunch, remaining]
    selected = candidate_batch(source, set(), 5)
    assert selected == [first, lunch, remaining]
    assert selected[0] is first
    assert candidate_batch(source, {candidate_query_key(first)}, 5) == [lunch, remaining]


def test_missing_meal_roles_fill_available_count_without_inventing_meals():
    source = [Proposal('Museum'), Proposal('Park'), Proposal('Lunch', meal='lunch')]
    assert candidate_batch(source, set(), 6) == source
    assert all(candidate.meal != 'dinner' for candidate in source)


def test_transient_failure_not_marked_known_is_eligible_again():
    source = proposals()
    selected = candidate_batch(source, set(), 5)
    failed = selected[1]
    known = {candidate_query_key(candidate) for candidate in selected if candidate is not failed}
    result = candidate_batch(source, known, 2, prefer_meal_balance=False)
    assert result[0] is failed
    assert len(result) == 2
    assert candidate_query_key(failed) not in known


@pytest.mark.parametrize('limit', [0, -1])
def test_nonpositive_batch_size_produces_no_queries(limit):
    assert candidate_batch(proposals(), set(), limit) == []


def test_all_known_proposals_do_not_trigger_new_queries():
    source = proposals()
    known = {candidate_query_key(candidate) for candidate in source}
    assert candidate_batch(source, known, 6) == []

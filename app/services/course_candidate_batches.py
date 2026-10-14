"""Select bounded provider query batches without discarding model proposals.

Meal roles are proposal priorities only. The Maps adapter and course planner
still establish real venue identity, category, coordinates and feasibility.
"""

from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from typing import Protocol

type CandidateQueryKey = tuple[str, str, str]


class ProposedCandidate(Protocol):
    """Fields needed to prioritize a proposal without importing graph state."""

    name: str
    english_name: str
    meal: str


def candidate_query_key(candidate: ProposedCandidate) -> CandidateQueryKey:
    """Build the existing request-local candidate identity key.

    Args:
        candidate: Model proposal containing names and a proposed meal role.
    Returns:
        Normalized local name, English name and meal role. This key is not a
        provider place ID and cannot establish that two venues are different.
    """
    return (
        candidate.name.strip().casefold(),
        candidate.english_name.strip().casefold(),
        candidate.meal,
    )


def candidate_batch[T: ProposedCandidate](
    candidates: Sequence[T],
    known_keys: AbstractSet[CandidateQueryKey],
    limit: int,
    *,
    prefer_meal_balance: bool = True,
) -> list[T]:
    """Select unqueried proposals while retaining every deferred candidate.

    Args:
        candidates: Original ordered model proposals, never modified.
        known_keys: Keys the caller can skip; not modified by this function.
            A transient failure must not be inserted as a known success.
        limit: Maximum selected count; nonpositive values produce no queries.
        prefer_meal_balance: Initially prioritize three nonmeal proposals and
            one lunch and dinner proposal. Set False to expand a deficient day
            with the next unqueried proposals in the original model order.
    Returns:
        Distinct eligible proposals in their original order. Missing meal roles
        are not invented, and proposal categories are not treated as verified.
    """
    if limit <= 0:
        return []
    unique: dict[CandidateQueryKey, T] = {}
    for candidate in candidates:
        key = candidate_query_key(candidate)
        if key not in known_keys:
            unique.setdefault(key, candidate)
    eligible = list(unique.items())
    priority: list[CandidateQueryKey] = []
    if prefer_meal_balance:
        priority.extend(key for key, candidate in eligible if candidate.meal == 'none')
        priority = priority[:3]
        for meal in ('lunch', 'dinner'):
            key = next((key for key, candidate in eligible if candidate.meal == meal), None)
            if key is not None:
                priority.append(key)
    # Slicing happens in priority order; the response retains the model order.
    selected_keys = set(list(dict.fromkeys([*priority, *unique]))[:limit])
    return [candidate for key, candidate in eligible if key in selected_keys]

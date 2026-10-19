"""Select bounded provider query batches without discarding model proposals.

Meal roles are proposal priorities only. The Maps adapter and course planner
still establish real venue identity, category, coordinates and feasibility.
"""

from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from itertools import combinations, product
from typing import Protocol

from app.services.course_budget import BudgetTier, proposal_budget_rank

type CandidateQueryKey = tuple[str, str, str]


class ProposedCandidate(Protocol):
    """Fields needed to prioritize a proposal without importing graph state."""

    name: str
    english_name: str
    meal: str
    cost: int
    budget_tier: BudgetTier


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
    daily_budget: float | None = None,
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
        daily_budget: Optional activity allowance. Compare complete local
            bundles of three nonmeals and two distinct meals before querying,
            retaining the same bounded batch size and original return order.
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
        nonmeals = [(key, candidate) for key, candidate in eligible if candidate.meal == 'none']
        lunches = [(key, candidate) for key, candidate in eligible if candidate.meal == 'lunch']
        dinners = [(key, candidate) for key, candidate in eligible if candidate.meal == 'dinner']
        if daily_budget is not None and limit >= 5:
            bundles = (
                [*visits, lunch, dinner]
                for visits, lunch, dinner in product(combinations(nonmeals, 3), lunches, dinners)
                if lunch[0][:2] != dinner[0][:2]
            )
            best = min(
                bundles,
                key=lambda bundle: (
                    proposal_budget_rank([candidate for _, candidate in bundle], daily_budget),
                    sum(candidate.cost for _, candidate in bundle),
                ),
                default=None,
            )
            if best:
                priority = [key for key, _ in best]
        if not priority:
            priority.extend(key for key, _ in nonmeals[:3])
            for choices in (lunches, dinners):
                if choices:
                    priority.append(choices[0][0])
    # Slicing happens in priority order; the response retains the model order.
    selected_keys = set(list(dict.fromkeys([*priority, *unique]))[:limit])
    return [candidate for key, candidate in eligible if key in selected_keys]

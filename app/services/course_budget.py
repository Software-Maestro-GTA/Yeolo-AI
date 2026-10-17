"""Calculate known KRW costs and rank budgets without rejecting safe courses.

Unknown transport fares and unavailable travel quotes are not invented. The
public numeric total is a subtotal when any requested component is unavailable.
"""

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING

from app.schemas.course import CourseRequestSchema, DayItinerarySchema

if TYPE_CHECKING:
    from app.agent.course_state import Visit
    from app.services.myrealtrip_quotes import TripQuotes


def course_cost(days: Sequence[DayItinerarySchema]) -> int:
    """Sum validated place estimates and any known internal transport fares.

    Args:
        days: Checked course days before public serialization removes route fares.
    Returns:
        Nonnegative known KRW subtotal. A missing or unusable fare contributes
        no known amount; this does not establish that the journey is free.
    """
    total = 0
    for day in days:
        for stop in day.stops:
            total += stop.cost
            fare = getattr(stop.transportToNext, 'cost', None)
            if type(fare) is int and fare >= 0:
                total += fare
    return total


def daily_place_budget(
    request: CourseRequestSchema, quotes: TripQuotes | None = None
) -> float:
    """Allocate the remaining stated budget after known flight/lodging quotes.

    Args:
        request: Validated finite KRW amount and positive trip duration.
        quotes: Optional actual quote outcomes; unavailable amounts stay unknown.
    Returns:
        Nonnegative per-day planning budget. Quotes exceeding the budget leave
        a zero soft target and never prevent itinerary generation.
    """
    known_quotes = quotes.known_total_krw if quotes is not None else 0
    return max(0.0, request.tripCondition.budget - known_quotes) / request.tripCondition.totalDays


def budget_rank(
    selected: Sequence[Visit], daily_budget: float | None
) -> tuple[bool, float]:
    """Prefer plans within the budget, without always preferring the cheapest.

    Args:
        selected: Proposed visits whose place identities were already verified.
            Only their stated activity/meal estimates are available at this stage.
        daily_budget: Finite nonnegative KRW target, or None to disable ranking.
    Returns:
        Over-budget flag and excess amount. All within-budget plans tie, and
        over-budget plans remain eligible for real route/schedule validation.
    Raises:
        ValueError: A caller bypasses validation with an invalid budget target.
    """
    if daily_budget is None:
        return False, 0.0
    if not math.isfinite(daily_budget) or daily_budget < 0:
        raise ValueError('Invalid daily budget target')
    subtotal = sum(candidate.cost for candidate, _ in selected)
    excess = max(0.0, subtotal - daily_budget)
    return excess > 0, excess

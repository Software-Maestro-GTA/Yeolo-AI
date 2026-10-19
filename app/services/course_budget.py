"""Allocate venue budgets after resolved travel costs, then rank local choices.

Round-trip travel and overnight lodging use provider quotes or explicit service
benchmarks. Unknown fares between itinerary stops remain separate and unpriced.
"""

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING, Literal, Protocol

from app.agent.tools.verified_maps import tourism_category_supported
from app.schemas.course import CourseRequestSchema, DayItinerarySchema

if TYPE_CHECKING:
    from app.agent.course_state import Visit
    from app.services.myrealtrip_quotes import TripQuotes


type BudgetTier = Literal['economy', 'standard', 'comfortable', 'premium']

_TIERS: tuple[BudgetTier, ...] = ('economy', 'standard', 'comfortable', 'premium')
FLIGHT_BUDGET_SHARE = 0.35
LODGING_BUDGET_SHARE = 0.25


class BudgetCandidate(Protocol):
    """Read only the planning fields shared by draft and batching proposals."""

    @property
    def cost(self) -> int: ...

    @property
    def budget_tier(self) -> BudgetTier: ...

    @property
    def meal(self) -> str: ...


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
    """Allocate activities after resolved round-trip travel and all hotel nights.

    Args:
        request: Validated finite KRW amount and positive trip duration.
        quotes: Provider quotes plus service benchmark fallbacks, resolved by
            quote_trip before the first draft. None is for isolated helpers only.
    Returns:
        Nonnegative daily allowance after subtracting each component once.
        No percentage reserve is subtracted again. An exceeded budget leaves
        a zero soft target and never blocks generation.
    """
    travel_cost = quotes.total_krw if quotes is not None else 0
    remaining = request.tripCondition.budget - travel_cost
    return max(0.0, remaining) / request.tripCondition.totalDays


def budget_guidance(
    request: CourseRequestSchema, quotes: TripQuotes | None = None
) -> str:
    """Describe the activity allowance and destination-relative venue choices.

    Args:
        request: Validated whole-trip KRW budget and duration.
        quotes: Resolved provider prices and benchmark fallback amounts.
    Returns:
        Model instructions distinguishing included travel costs from venue costs.
    """
    allowance = daily_place_budget(request, quotes)
    trip = request.tripCondition
    return (
        f'목적지: {trip.destinationCountry} / {trip.destinationCity}. '
        f'왕복 이동·전체 숙박 비용 합계(KRW): {quotes.total_krw if quotes else 0}. '
        f'일별 장소·식사 예산 상한 목표(KRW): {allowance:.0f}. '
        '왕복 이동·숙박 비용은 이미 차감했으므로 장소 cost에 다시 포함하지 마세요. '
        '이 목적지의 식당·체험끼리 비교해 가성비와 상위 수준의 대안을 함께 제안하세요. '
        'budget_tier는 해당 도시 안에서 제안한 메뉴·체험의 상대적 수준이며 '
        '전 세계 공통 원화 기준이나 사용자의 요청 예산으로 등급을 정하지 마세요. '
        '현지 가격으로 필수 식사와 관광을 구성한 뒤, 예산 안에서 가능한 더 나은 식사·체험을 선택하세요. '
        '예산이 부족하면 실속 있는 현지 식사와 무료·저비용 명소를 유지하고, '
        '예산 여유가 있으면 실제로 더 높은 수준의 다른 식당과 특색 있는 체험을 제안하세요. '
        '조회하지 못한 왕복 이동·숙박에는 서비스 기준 추정액이 포함될 수 있으며 실시간 견적으로 표현하지 마세요. '
        '표시된 budget_tier는 선택 성향이지 실제 품질 등급이나 현지 물가를 검증한 정보가 아닙니다. '
        '같은 장소의 가격을 예산에 맞춰 부풀리거나 예산을 전부 소진하려고 활동을 추가하지 마세요. '
        '음식·체험의 실제 예상 지출을 제안하고 적합한 상위 가격대 장소가 없으면 기존 실제 장소를 유지하세요. '
        '입장료가 있는 명소를 비용 미확인이라는 이유로 무료(0원)로 기재하지 마세요. '
        '예산 초과가 불가피하면 명소와 필수 식사를 유지하세요.'
    )


def _spending_rank(
    candidates: Sequence[BudgetCandidate], paid_tiers: Sequence[int], daily_budget: float | None
) -> tuple[bool, float, float, int]:
    """Compare complete local alternatives without assigning a global spending band."""
    if daily_budget is None:
        return False, 0.0, 0.0, 0
    if not math.isfinite(daily_budget) or daily_budget < 0:
        raise ValueError('Invalid daily budget target')
    excess = max(0.0, sum(candidate.cost for candidate in candidates) - daily_budget)
    meals = [_TIERS.index(candidate.budget_tier) for candidate in candidates
             if candidate.meal in {'lunch', 'dinner'}]
    return (
        excess > 0,
        excess,
        -sum(meals) / len(meals) if meals else 0.0,
        -max(paid_tiers, default=0),
    )


def proposal_budget_rank(
    candidates: Sequence[BudgetCandidate], daily_budget: float | None
) -> tuple[bool, float, float, int]:
    """Rank a proposed meal/highlight bundle before bounded place verification.

    Args:
        candidates: Jointly selected proposals with destination-relative tiers
            and unchanged estimated costs. Their identities are not yet verified.
        daily_budget: Daily activity allowance, or None to retain model order.
    Returns:
        Affordability first, then higher meal tiers and one paid highlight tier.
        A higher price within the same tiers earns no reward. This is only query
        priority; provider facts and feasibility still govern the final course.
    Raises:
        ValueError: The supplied allowance is nonfinite or negative.
    """
    paid_tiers = [_TIERS.index(candidate.budget_tier) for candidate in candidates
                  if candidate.meal == 'none' and candidate.cost > 0]
    return _spending_rank(candidates, paid_tiers, daily_budget)


def budget_rank(
    selected: Sequence[Visit], daily_budget: float | None
) -> tuple[bool, float, float, int]:
    """Prefer affordable local meal/experience levels without rewarding price alone.

    Args:
        selected: Proposed visits whose place identities were already verified.
            Only their stated activity/meal estimates are available at this stage.
        daily_budget: Finite nonnegative KRW target, or None to disable ranking.
    Returns:
        Over-budget flag, excess amount, negative mean meal tier, and negative
        best paid tourism tier. The same allowance can buy different levels in
        different cities because candidate prices differ. Price does not break
        within-budget ties. One paid highlight satisfies the experience
        preference, leaving free highlights eligible. Tiers remain model
        proposals, not verified prices or provider quality ratings.
        Over-budget plans remain eligible for route/schedule validation.
    Raises:
        ValueError: A caller bypasses validation with an invalid budget target.
    """
    experiences = [
        _TIERS.index(candidate.budget_tier)
        for candidate, venue in selected
        if candidate.meal == 'none' and candidate.cost > 0
        and tourism_category_supported(venue.place.category)
    ]
    return _spending_rank([candidate for candidate, _ in selected], experiences, daily_budget)

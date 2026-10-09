"""Choose bounded retry, refill, repair and verified-snapshot transitions.

Keep recovery policy separate from node side effects and graph connections."""

import asyncio

from app.agent.course_drafting import MIN_DAILY_DRAFT_BUDGET_SECONDS
from app.agent.course_state import CourseGenerationError, CourseRouter, CourseState

MAX_FULLNESS_REFILLS = 2


def _max_drafts(state: CourseState) -> int:
    return 3 if state["request"].tripCondition.totalDays > 1 else 2


def _optional_budget(state: CourseState) -> float:
    remaining = (
        state.get("deadline", float("inf")) - asyncio.get_running_loop().time() - 1.0
    )
    return min(25.0, max(0.0, remaining))


def _needs_fullness(state: CourseState) -> bool:
    """Distinguish required missing visits from optional history diversity."""
    snapshot = state.get("verified_fallback", {})
    return bool(snapshot and any(snapshot["rank"][:2]))


def _can_refill(state: CourseState) -> bool:
    remaining = (
        state.get("deadline", float("inf")) - asyncio.get_running_loop().time() - 1.0
    )
    return (
        state.get("refill_attempt", 0) < MAX_FULLNESS_REFILLS
        and remaining >= MIN_DAILY_DRAFT_BUDGET_SECONDS
    )


def route_after(success: str) -> CourseRouter:
    """Build the shared conditional router for one successful stage.

    Args:
        success: Next node when the current stage has no recovery feedback.

    Returns:
        Async callback choosing the existing retry, repair or snapshot path.

    Raises:
        CourseGenerationError: The callback exhausts recovery without a verified
            snapshot that can be returned safely.
    """

    async def decide(state: CourseState) -> str:
        if state.get("restore_fallback"):
            return "restore"
        if not state.get("feedback"):
            return success
        if state["attempt"] >= _max_drafts(state) or state.get("refilling"):
            if (
                not state.get("repairing")
                and _can_refill(state)
                and (_needs_fullness(state) or not state.get("verified_fallback"))
            ):
                return "refill"
            if _needs_fullness(state) and not state.get("repairing"):
                return "repair"
            if state.get("verified_fallback"):
                return "restore"
            if not state.get("repairing") and state.get("draft_data"):
                return "repair"
            raise CourseGenerationError(state["feedback"])
        return "draft"

    return decide

"""Define internal model proposals and request-local LangGraph state.

These schemas do not change the public course API."""

from collections.abc import Awaitable, Callable
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, Field

from app.agent.tools.verified_maps import (
    Destination,
    VerifiedPlace,
)
from app.schemas.course import (
    CourseRequestSchema,
    CourseSchema,
    DayItinerarySchema,
    TransportToNextSchema,
)
from app.services.course_diversity import (
    Experience,
)


class CourseGenerationError(ValueError):
    """No sufficiently verified, feasible, novel course could be produced."""


class Candidate(BaseModel):
    """Internal proposal; contains no invented map facts or arrival times."""

    name: str = Field(min_length=1, max_length=160)
    english_name: str = Field(default="", max_length=160)
    category: str = "attraction"
    stay_minutes: int = Field(default=60, ge=15, le=240)
    cost: int = Field(default=0, ge=0)
    reason: str = ""
    meal: Literal["none", "breakfast", "lunch", "dinner"] = "none"
    planned_area: str = Field(default="", max_length=100)
    experiences: list[Experience] = Field(default_factory=list, max_length=5)


class DraftDay(BaseModel):
    candidates: list[Candidate] = Field(min_length=2, max_length=10)


class CourseDraft(BaseModel):
    title: str
    reason: str
    tags: list[str] = Field(default_factory=list)
    days: list[DraftDay]


type Visit = tuple[Candidate, VerifiedPlace]


class ValidatedDay(TypedDict):
    """Keep accepted venues, actual routes and their checked schedule together."""

    selected: list[Visit]
    routes: list[TransportToNextSchema]
    day: DayItinerarySchema


class CourseState(TypedDict, total=False):
    """Request-local drafts, verified facts, recovery state and final output."""

    # Request conditions and the absolute processing deadline.
    request: CourseRequestSchema
    destination: Destination
    deadline: float

    # Daily model proposals and feedback for bounded retries.
    attempt: int
    draft_data: CourseDraft
    partial_drafts: dict[int, DraftDay]
    feedback: str

    # Verified facts, retained candidates and possible daily combinations.
    place_cache: dict[tuple, Any]
    route_cache: dict[tuple, Any]
    candidate_pools: dict[int, dict[tuple[str, str], Visit]]
    day_plans: list[list[list[Visit]]]
    validated_days: dict[int, ValidatedDay]
    selected: list[list[Visit]]
    routes: list[list[TransportToNextSchema]]
    failures: list[str]
    rejected_plans: dict[int, set[tuple]]

    # Recent history guides diversity after venue and schedule safety.
    recent: list[set[str]]
    recent_profiles: list[dict]
    history_unavailable: bool
    repeated: bool
    diversity_scores: dict

    # Recovery retains successful days and the best complete safe snapshot.
    repairing: bool
    refill_attempt: int
    refilling: bool
    discovered_days: set[int]
    attraction_discovered_days: set[int]
    verified_fallback: dict
    restore_fallback: bool

    # Final schedules and the public course response.
    days: list[DayItinerarySchema]
    course: CourseSchema


type CourseNode = Callable[[CourseState], Awaitable[dict]]


type CourseRouter = Callable[[CourseState], Awaitable[str]]


class PartialDraftError(TimeoutError):
    """Retain completed day proposals when a subset of model calls failed."""

    def __init__(self, days: dict[int, DraftDay], errors: dict[int, Exception]) -> None:
        super().__init__("Some daily candidate calls did not complete")
        self.days, self.errors = days, errors

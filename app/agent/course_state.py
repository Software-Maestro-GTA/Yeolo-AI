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
    StopSchema,
    TransportToNextSchema,
)
from app.schemas.course_anchors import AnchorCandidates
from app.services.course_diversity import (
    Experience,
)
from app.services.myrealtrip_quotes import TripQuotes


class CourseGenerationError(ValueError):
    """No sufficiently verified, feasible, novel course could be produced."""


class Candidate(BaseModel):
    """Internal proposal; contains no invented map facts or arrival times."""

    name: str = Field(min_length=1, max_length=160)
    english_name: str = Field(default="", max_length=160)
    category: str = "attraction"
    stay_minutes: int = Field(default=60, ge=15, le=240)
    cost: int = Field(default=0, ge=0)
    budget_tier: Literal["economy", "standard", "comfortable", "premium"] = Field(
        default="standard",
        description="Destination-relative spending level for the proposed use, independent of request budget; not a verified rating",
    )
    meal: Literal["none", "breakfast", "lunch", "dinner"] = "none"
    planned_area: str = Field(default="", max_length=100)
    experiences: list[Experience] = Field(default_factory=list, max_length=5)


class DraftDay(BaseModel):
    candidates: list[Candidate] = Field(min_length=2, max_length=10)


class CourseDraft(BaseModel):
    title: str
    tags: list[str] = Field(default_factory=list)
    days: list[DraftDay]
    anchor_candidates: AnchorCandidates = Field(default_factory=AnchorCandidates)


class ScheduledStopSchema(StopSchema):
    """Keep calculated local arrival time for internal feasibility checks only."""

    arrivalTime: str = Field(..., pattern=r"^\d{2}:\d{2}$", exclude=True)


class ScheduledDaySchema(DayItinerarySchema):
    """Retain checked arrival times without adding fields to the public result."""

    stops: list[ScheduledStopSchema]


type Visit = tuple[Candidate, VerifiedPlace]


class ValidatedDay(TypedDict):
    """Keep verified venues and measured/unknown transfers with internal ordering."""

    selected: list[Visit]
    routes: list[TransportToNextSchema]
    day: ScheduledDaySchema


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
    anchor_candidates: AnchorCandidates
    anchor_selection: Any
    feedback: str

    # Verified facts, retained candidates and possible daily combinations.
    place_cache: dict[tuple, Any]
    queried_place_keys: set[tuple[str, str, str]]
    pending_place_days: set[int]
    expanding_places: bool
    route_cache: dict[tuple, Any]
    route_pass_failures: dict[tuple, Any]
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
    verified_fallback: dict
    restore_fallback: bool

    # Final schedules and the public course response.
    days: list[DayItinerarySchema]
    course: CourseSchema
    trip_quotes: TripQuotes


type CourseNode = Callable[[CourseState], Awaitable[dict]]


type CourseRouter = Callable[[CourseState], Awaitable[str]]


class PartialDraftError(TimeoutError):
    """Retain completed day proposals when a subset of model calls failed."""

    def __init__(
        self,
        days: dict[int, DraftDay],
        errors: dict[int, Exception],
        anchor_candidates: AnchorCandidates | None = None,
    ) -> None:
        super().__init__("Some daily candidate calls did not complete")
        self.days, self.errors = days, errors
        self.anchor_candidates = anchor_candidates or AnchorCandidates()

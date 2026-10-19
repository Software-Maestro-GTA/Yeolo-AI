"""Keep optional trip-wide airport and lodging proposals separate from visits.

These names are unverified model candidates, never supplier or Maps identities.
Malformed optional proposals are discarded without invalidating daily visits.
"""

from typing import Any

from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)


class _NamedAnchorCandidate(BaseModel):
    """A searchable name without prices, coordinates or availability."""

    name: str = Field(min_length=1, max_length=160, strict=True)
    english_name: str = Field(default="", max_length=160, strict=True)

    @field_validator("name", "english_name", mode="before")
    @classmethod
    def strip_names(cls, value: Any) -> Any:
        """Reject blank primary names after trimming model whitespace."""
        return value.strip() if isinstance(value, str) else value


class NamedLodgingCandidate(_NamedAnchorCandidate):
    """A property name to verify independently of room availability or prices."""


class AirportCandidate(_NamedAnchorCandidate):
    """An airport proposal whose IATA/name agreement still requires verification."""

    iata_code: str = Field(pattern=r"^[A-Z]{3}$", strict=True)

    @field_validator("iata_code", mode="before")
    @classmethod
    def normalize_iata(cls, value: Any) -> Any:
        """Keep valid code hints despite whitespace/case; never invent a code."""
        return value.strip().upper() if isinstance(value, str) else value


class AnchorCandidates(BaseModel):
    """Bound optional proposals to two valid, distinct entries of each type.

    Parsing drops malformed entries and excess alternatives. The existing model
    retry budget is reserved for required daily visits, not optional anchors.
    """

    airports: list[AirportCandidate] = Field(default_factory=list, max_length=2)
    lodgings: list[NamedLodgingCandidate] = Field(default_factory=list, max_length=2)

    @model_validator(mode="before")
    @classmethod
    def optional_payload(cls, value: Any) -> Any:
        """Treat missing or malformed optional payloads as an empty proposal."""
        return value if isinstance(value, (dict, cls)) else {}

    @field_validator("airports", "lodgings", mode="before")
    @classmethod
    def valid_entries(cls, value: Any, info: ValidationInfo) -> list[_NamedAnchorCandidate]:
        """Keep at most two validated candidates without losing valid siblings."""
        if not isinstance(value, list):
            return []
        schema = AirportCandidate if info.field_name == "airports" else NamedLodgingCandidate
        candidates: list[_NamedAnchorCandidate] = []
        seen: set[str] = set()
        for item in value:
            try:
                candidate = schema.model_validate(item)
            except ValidationError:
                continue
            key = (
                candidate.iata_code
                if isinstance(candidate, AirportCandidate)
                else candidate.name.casefold()
            )
            if key in seen:
                continue
            seen.add(key)
            candidates.append(candidate)
            if len(candidates) == 2:
                break
        return candidates

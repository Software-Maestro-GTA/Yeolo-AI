"""Prepare grounded UI interests and assemble an LLM-selected city without map calls."""

from dataclasses import dataclass
from typing import Any

from app.schemas.taste_profile import AnalysisMetadataSchema, TasteProfileSchema
from app.schemas.taste_summary import (
    SUMMARY_TYPE_COUNT,
    CityRecommendation,
    SummaryType,
    TasteSummary,
    TasteSummaryDraft,
)

MIN_ANALYSIS_PHOTOS = 5
MIN_ANALYSIS_DAYS = 3


class InsufficientTasteEvidenceError(ValueError):
    """Signal a recoverable lack of evidence, mapped to API-AI-1 status 400."""

    def __init__(self) -> None:
        super().__init__('분석 가능한 기록이 부족해요. 다른 날짜와 장소의 사진을 추가해 주세요.')


# A display registry for the existing profile, not the proposed v2 scoring model.
# Each group contributes at most one slider to avoid restating the same evidence.
TYPE_DEFINITIONS = (
    ("CAFE_DESSERT", "foodPreference.cafeDessert", "cafes and desserts", "food"),
    ("CULTURE", "activityPreference.viewing", "museums, galleries and performances", "culture"),
    ("BEACH", "preferredLocationType.beachResort", "beaches and resorts", "nature"),
    ("OUTDOOR", "activityPreference.adventure", "outdoor sports venues", "activity"),
    ("NATURE", "travelPurpose.natureExploration", "natural scenery and parks", "nature"),
    ("FOOD", "travelPurpose.gourmet", "food venues", "food"),
    ("SHOPPING", "activityPreference.shopping", "shopping places", "shopping"),
    ("NIGHTLIFE", "activityPreference.nightlife", "bars and nightlife venues", "nightlife"),
    ("WELLNESS", "travelPurpose.wellness", "spa and wellness venues", "wellness"),
    ("EXPERIENCE", "activityPreference.experience", "workshops and activity venues", "activity"),
)


def validate_taste_evidence(statistics: dict[str, Any]) -> None:
    """Require five valid photos on three days and three observed interest groups.

    Args:
        statistics: Valid deduplicated photo counts, visit counts and distinct-day counts.
    Raises:
        InsufficientTasteEvidenceError: The sample or observed group coverage is insufficient.
    """
    if statistics['validPhotoCount'] < MIN_ANALYSIS_PHOTOS or statistics['distinctDayCount'] < MIN_ANALYSIS_DAYS:
        raise InsufficientTasteEvidenceError
    visits = statistics['fieldVisitCounts']
    days = statistics['fieldDayCounts']
    supported_groups = {
        group for _, path, _, group in TYPE_DEFINITIONS
        if visits.get(path, 0) > 0 and days.get(path, 0) > 0
    }
    if len(supported_groups) < SUMMARY_TYPE_COUNT:
        raise InsufficientTasteEvidenceError


@dataclass(frozen=True)
class SummaryContext:
    """Request-scoped allowed facts; never contains raw photos or trace metadata."""

    language: str
    types: list[dict[str, Any]]
    recommendation_interests: list[dict[str, Any]]

    def model_input(self) -> dict[str, Any]:
        """Return the minimum facts needed by the summary model."""
        return {
            "language": "ko",
            "hasSupportedPreferences": bool(self.types),
            "types": self.types,
            "recommendationInterests": self.recommendation_interests,
        }


def prepare_summary_context(
    profile: TasteProfileSchema, metadata: AnalysisMetadataSchema, language: str,
) -> SummaryContext:
    """Select three observed interests, including explicitly provisional signals.

    Args:
        profile: Final guarded profile, whose numeric values must remain unchanged.
        metadata: Internal visit evidence separating observed signals from unobserved placeholders.
        language: Legacy locale input; summary generation always uses Korean.
    Returns:
        Exactly three display axes and all supported interests for the model to
        recommend a city, including interests not displayed as sliders. No network I/O.
    Raises:
        InsufficientTasteEvidenceError: The sample is insufficient or fewer than three groups were observed.
    """
    validate_taste_evidence(metadata.statistics)
    ranked = []
    for order, (code, path, subject, group) in enumerate(TYPE_DEFINITIONS):
        evidence = metadata.fieldEvidence.get(path)
        section, key = path.split(".")
        score = getattr(getattr(profile, section), key)
        if evidence is None:
            continue
        days = evidence.evidence.get("distinctDayCount", 0)
        visits = evidence.evidence.get("visitCount", 0)
        if visits <= 0 or days <= 0:
            continue
        # Repetition governs score confidence, not eligibility for an observed UI axis.
        provisional = evidence.insufficientEvidence or path in metadata.fallbackFields or score < 4
        ranked.append(((-score, -days, -visits, order), group, {
            "typeCode": code, "subject": subject, "sourceField": path,
            "sourceScore": score, "score": (score - 1) * 25,
            "visitCount": visits, "distinctDayCount": days,
            "confidence": evidence.confidence, "isProvisional": provisional,
        }))
    selected = []
    groups = set()
    for _, group, item in sorted(ranked):
        if group not in groups:
            selected.append(item)
            groups.add(group)
        if len(selected) == SUMMARY_TYPE_COUNT:
            break

    if len(selected) != SUMMARY_TYPE_COUNT:
        raise InsufficientTasteEvidenceError

    recommendation_interests = [
        {key: item[key] for key in (
            "typeCode", "subject", "sourceScore", "visitCount",
            "distinctDayCount", "confidence", "isProvisional",
        )}
        for _, _, item in sorted(ranked)
    ]
    return SummaryContext("ko", selected, recommendation_interests)


def assemble_taste_summary(draft: TasteSummaryDraft, context: SummaryContext) -> TasteSummary:
    """Validate generated type references and attach server-owned scores.

    Args:
        draft: Schema-validated copy and city recommendation; no editable scores or endpoints.
        context: Exact inputs used for that invocation.
    Returns:
        API-AI-1 summary in deterministic type order.
    Raises:
        ValueError: The model invents, duplicates or omits an axis.
    """
    titles = {item.typeCode: item.typeName for item in draft.types}
    if len(titles) != len(draft.types) or set(titles) != {item["typeCode"] for item in context.types}:
        raise ValueError("Summary types must match the supported input types exactly")
    low, high = "약함", "강함"
    return TasteSummary(
        intro=draft.intro, hashtags=draft.hashtags,
        types=[SummaryType(
            typeCode=item["typeCode"], typeName=titles[item["typeCode"]],
            lowName=low, highName=high, score=item["score"],
        ) for item in context.types],
        recommendCity=CityRecommendation(
            countryName=draft.recommendCity.countryName, cityName=draft.recommendCity.cityName,
            reason=draft.recommendCity.reason,
        ),
    )

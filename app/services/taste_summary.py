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


class InsufficientTasteEvidenceError(ValueError):
    """Signal a recoverable lack of evidence, mapped to API-AI-1 status 400."""

    def __init__(self) -> None:
        super().__init__('분석 가능한 사진을 최소 5장 추가해 주세요.')


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

UNCONFIRMED_TYPE_NAMES = {
    "CAFE_DESSERT": "카페 취향 미확인", "CULTURE": "문화 취향 미확인",
    "BEACH": "해변 취향 미확인", "OUTDOOR": "야외활동 취향 미확인",
    "NATURE": "자연 취향 미확인", "FOOD": "미식 취향 미확인",
    "SHOPPING": "쇼핑 취향 미확인", "NIGHTLIFE": "밤문화 취향 미확인",
    "WELLNESS": "휴식 취향 미확인", "EXPERIENCE": "체험 취향 미확인",
}


def validate_taste_evidence(statistics: dict[str, Any]) -> None:
    """Require only five valid photos; dates and interest coverage are not gates.

    Args:
        statistics: Photo counts after duplicate IDs and unusable metadata are excluded.
    Raises:
        InsufficientTasteEvidenceError: Fewer than five usable photos remain.
    """
    if statistics['validPhotoCount'] < MIN_ANALYSIS_PHOTOS:
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
            "hasSupportedPreferences": bool(self.recommendation_interests),
            "types": self.types,
            "recommendationInterests": self.recommendation_interests,
        }


def prepare_summary_context(
    profile: TasteProfileSchema, metadata: AnalysisMetadataSchema, language: str,
) -> SummaryContext:
    """Prefer observed interests and fill missing display axes as unconfirmed.

    Args:
        profile: Final guarded profile, whose numeric values must remain unchanged.
        metadata: Internal visit evidence separating observed signals from unobserved placeholders.
        language: Legacy locale input; summary generation always uses Korean.
    Returns:
        Three display axes with explicit observation flags and observed recommendation
        interests. Unobserved axes are neutral placeholders, never recommendation evidence.
    Raises:
        InsufficientTasteEvidenceError: Fewer than five usable photos remain.
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
            "confidence": evidence.confidence, "isProvisional": provisional, "isObserved": True,
        }))
    selected = []
    groups = set()
    for _, group, item in sorted(ranked):
        if group not in groups:
            selected.append(item)
            groups.add(group)
        if len(selected) == SUMMARY_TYPE_COUNT:
            break

    # Preserve the three-axis UI without inventing preference evidence or scores.
    for code, path, subject, group in TYPE_DEFINITIONS:
        if len(selected) == SUMMARY_TYPE_COUNT:
            break
        if group in groups:
            continue
        selected.append({
            "typeCode": code, "subject": subject, "sourceField": path,
            "sourceScore": 3, "score": 50, "visitCount": 0, "distinctDayCount": 0,
            "confidence": "low", "isProvisional": True, "isObserved": False,
        })
        groups.add(group)

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
            typeCode=item["typeCode"],
            typeName=titles[item["typeCode"]] if item["isObserved"] else UNCONFIRMED_TYPE_NAMES[item["typeCode"]],
            lowName=low, highName=high, score=item["score"],
        ) for item in context.types],
        recommendCity=CityRecommendation(
            countryName=draft.recommendCity.countryName, cityName=draft.recommendCity.cityName,
            reason=draft.recommendCity.reason,
        ),
    )

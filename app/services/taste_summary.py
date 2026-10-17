"""Prepare grounded UI interests and bounded city candidates without extra calls."""

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
from app.services.behavior_evidence import MIN_EVIDENCE_DAYS, MIN_EVIDENCE_VISITS
from app.services.taste_city_catalog import CITY_CANDIDATES


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

MAX_CITY_CANDIDATES = 8

AXIS_LABELS = {
    "ko": ("약함", "강함"), "en": ("Weaker", "Stronger"),
    "ja": ("弱め", "強め"), "zh": ("较弱", "较强"), "zh-Hant": ("較弱", "較強"),
    "fr": ("Faible", "Fort"), "es": ("Menor", "Mayor"),
    "de": ("Schwächer", "Stärker"), "pt": ("Menor", "Maior"),
}


def validate_taste_evidence(statistics: dict[str, Any]) -> None:
    """Require repeated evidence for three distinct interest groups before inference.

    Args:
        statistics: Deduplicated visit counts and distinct-day counts by field.
    Raises:
        InsufficientTasteEvidenceError: Fewer than three groups meet the guard threshold.
    """
    visits = statistics['fieldVisitCounts']
    days = statistics['fieldDayCounts']
    supported_groups = {
        group for _, path, _, group in TYPE_DEFINITIONS
        if visits.get(path, 0) >= MIN_EVIDENCE_VISITS and days.get(path, 0) >= MIN_EVIDENCE_DAYS
    }
    if len(supported_groups) < SUMMARY_TYPE_COUNT:
        raise InsufficientTasteEvidenceError


@dataclass(frozen=True)
class SummaryContext:
    """Request-scoped allowed facts; never contains raw photos or trace metadata."""

    language: str
    types: list[dict[str, Any]]
    cities: list[dict[str, Any]]
    recommendation_interests: list[dict[str, Any]]

    def model_input(self) -> dict[str, Any]:
        """Return the minimum facts needed by the summary model."""
        return {
            "language": self.language,
            "hasSupportedPreferences": bool(self.types),
            "types": self.types,
            "cityCandidates": self.cities,
            "recommendationInterests": self.recommendation_interests,
        }


def _select_cities(
    supported: list[tuple[str, dict[str, Any]]], language: str,
) -> list[dict[str, Any]]:
    """Rank cities locally, using each related interest group's strongest match.

    Args:
        supported: Evidence-backed interests and their shared group identities.
        language: Normalized locale for existing Korean/English proper names.
    Returns:
        At most eight cities matching the supported interests.
        Ties favor coverage, then city codes rather than catalog insertion order.
    """
    ranked = []
    for city in CITY_CANDIDATES:
        weights: dict[str, int] = {}
        matched = []
        for group, interest in supported:
            if interest["typeCode"] in city.interests:
                weights[group] = max(weights.get(group, 0), interest["sourceScore"] - 3)
                matched.append(interest["typeCode"])
        if not matched:
            continue
        korean = language.split("-")[0] == "ko"
        candidate = {
            "cityCode": city.code,
            "countryName": city.country_ko if korean else city.country_en,
            "cityName": city.city_ko if korean else city.city_en,
            "interestCodes": list(city.interests), "matchedTypeCodes": sorted(matched),
        }
        ranked.append(((-sum(weights.values()), -len(weights), -len(matched), city.code), candidate))
    return [candidate for _, candidate in sorted(ranked, key=lambda item: item[0])[:MAX_CITY_CANDIDATES]]


def prepare_summary_context(
    profile: TasteProfileSchema, metadata: AnalysisMetadataSchema, language: str,
) -> SummaryContext:
    """Select exactly three distinct interests supported by guarded evidence.

    Args:
        profile: Final guarded profile, whose numeric values must remain unchanged.
        metadata: Internal evidence used to exclude compatibility placeholders.
        language: Normalized request locale.
    Returns:
        Exactly three display axes and up to eight cities matched using all supported
        interests, including interests not displayed as sliders. No network I/O.
    Raises:
        InsufficientTasteEvidenceError: Fewer than three groups have supported scores of 4 or 5.
    """
    ranked = []
    for order, (code, path, subject, group) in enumerate(TYPE_DEFINITIONS):
        evidence = metadata.fieldEvidence.get(path)
        section, key = path.split(".")
        score = getattr(getattr(profile, section), key)
        if evidence is None or evidence.insufficientEvidence or path in metadata.fallbackFields or score < 4:
            continue
        days = evidence.evidence.get("distinctDayCount", 0)
        visits = evidence.evidence.get("visitCount", 0)
        ranked.append(((-score, -days, -visits, order), group, {
            "typeCode": code, "subject": subject, "sourceField": path,
            "sourceScore": score, "score": (score - 1) * 25,
            "visitCount": visits, "distinctDayCount": days,
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

    supported = [(group, item) for _, group, item in ranked]
    cities = _select_cities(supported, language)
    recommendation_interests = [
        {key: item[key] for key in ("typeCode", "subject", "sourceScore")}
        for _, _, item in sorted(ranked)
    ]
    return SummaryContext(language, selected, cities, recommendation_interests)


def assemble_taste_summary(draft: TasteSummaryDraft, context: SummaryContext) -> TasteSummary:
    """Validate generated references and attach server-owned scores and city names.

    Args:
        draft: Validated model copy, with no permission to set scores or endpoints.
        context: Exact inputs used for that invocation.
    Returns:
        API-AI-1 summary in deterministic type order.
    Raises:
        ValueError: The model invents, duplicates, omits an axis or selects an unrelated city.
    """
    titles = {item.typeCode: item.typeName for item in draft.types}
    if len(titles) != len(draft.types) or set(titles) != {item["typeCode"] for item in context.types}:
        raise ValueError("Summary types must match the supported input types exactly")
    city = next((item for item in context.cities if item["cityCode"] == draft.recommendCity.cityCode), None)
    if city is None:
        raise ValueError("Summary city must be one of the matching candidates")
    locale = context.language.split("-")[0]
    if context.language in {"zh-TW", "zh-HK"} or "Hant" in context.language:
        locale = "zh-Hant"
    low, high = AXIS_LABELS.get(locale, AXIS_LABELS["en"])
    return TasteSummary(
        intro=draft.intro, hashtags=draft.hashtags,
        types=[SummaryType(
            typeCode=item["typeCode"], typeName=titles[item["typeCode"]],
            lowName=low, highName=high, score=item["score"],
        ) for item in context.types],
        recommendCity=CityRecommendation(
            countryName=city["countryName"], cityName=city["cityName"], reason=draft.recommendCity.reason,
        ),
    )

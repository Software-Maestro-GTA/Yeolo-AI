"""Select supported summary axes and assemble immutable scores/city identities."""

from dataclasses import dataclass
from typing import Any

from app.schemas.taste_profile import AnalysisMetadataSchema, TasteProfileSchema
from app.schemas.taste_summary import (
    CityRecommendation,
    SummaryType,
    TasteSummary,
    TasteSummaryDraft,
)

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

# Initial editorial recommendation candidates. These are broad interest tags,
# not live claims about availability, prices, weather or opening hours.
CITY_CANDIDATES = (
    ("SEOUL", "대한민국", "서울", "South Korea", "Seoul", ("CAFE_DESSERT", "CULTURE", "FOOD", "SHOPPING", "NIGHTLIFE")),
    ("BUSAN", "대한민국", "부산", "South Korea", "Busan", ("BEACH", "FOOD", "CAFE_DESSERT")),
    ("KYOTO", "일본", "교토", "Japan", "Kyoto", ("CULTURE", "NATURE", "FOOD")),
    ("TOKYO", "일본", "도쿄", "Japan", "Tokyo", ("SHOPPING", "FOOD", "CULTURE", "EXPERIENCE", "NIGHTLIFE")),
    ("LISBON", "포르투갈", "리스본", "Portugal", "Lisbon", ("CULTURE", "CAFE_DESSERT", "FOOD")),
    ("BARCELONA", "스페인", "바르셀로나", "Spain", "Barcelona", ("CULTURE", "BEACH", "NIGHTLIFE")),
    ("VANCOUVER", "캐나다", "밴쿠버", "Canada", "Vancouver", ("NATURE", "OUTDOOR", "BEACH")),
    ("SINGAPORE", "싱가포르", "싱가포르", "Singapore", "Singapore", ("SHOPPING", "FOOD", "EXPERIENCE", "WELLNESS")),
)

AXIS_LABELS = {
    "ko": ("약함", "강함"), "en": ("Weaker", "Stronger"),
    "ja": ("弱め", "強め"), "zh": ("较弱", "较强"), "zh-Hant": ("較弱", "較強"),
    "fr": ("Faible", "Fort"), "es": ("Menor", "Mayor"),
    "de": ("Schwächer", "Stärker"), "pt": ("Menor", "Maior"),
}


@dataclass(frozen=True)
class SummaryContext:
    """Request-scoped allowed facts; never contains raw photos or trace metadata."""

    language: str
    types: list[dict[str, Any]]
    cities: list[dict[str, Any]]

    def model_input(self) -> dict[str, Any]:
        """Return the minimum facts needed by the summary model."""
        return {
            "language": self.language,
            "hasSupportedPreferences": bool(self.types),
            "types": self.types,
            "cityCandidates": self.cities,
        }


def prepare_summary_context(
    profile: TasteProfileSchema, metadata: AnalysisMetadataSchema, language: str,
) -> SummaryContext:
    """Select at most three distinct interests supported by guarded evidence.

    Args:
        profile: Final guarded profile, whose numeric values must remain unchanged.
        metadata: Internal evidence used to exclude compatibility placeholders.
        language: Normalized request locale.
    Returns:
        Allowed axes and matched editorial cities; no unsupported profile fields.
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
        if len(selected) == 3:
            break

    codes = {item["typeCode"] for item in selected}
    cities = []
    for code, country_ko, city_ko, country_en, city_en, interests in CITY_CANDIDATES:
        matched = codes.intersection(interests)
        if codes and not matched:
            continue
        korean = language.split("-")[0] == "ko"
        cities.append({
            "cityCode": code,
            "countryName": country_ko if korean else country_en,
            "cityName": city_ko if korean else city_en,
            "interestCodes": list(interests), "matchedTypeCodes": sorted(matched),
        })
    return SummaryContext(language, selected, cities)


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

"""Deterministic guards for claims supported by visit metadata."""

from typing import Any

from app.schemas.taste_profile import (
    AnalysisMetadataSchema,
    FieldEvidenceSchema,
    TasteProfileSchema,
)

# Deliberately narrow mappings: a generic restaurant does not establish local food,
# fame, fine dining, dietary restrictions or relative interest in food.
FIELD_TYPES: dict[str, set[str]] = {
    "travelPurpose.relaxation": {"beach", "resort_hotel", "spa"},
    "travelPurpose.sightseeing": {
        "tourist_attraction",
        "museum",
        "historical_landmark",
    },
    "travelPurpose.culturalExperience": {
        "museum",
        "art_gallery",
        "performing_arts_theater",
    },
    "travelPurpose.gourmet": {"restaurant", "cafe", "bakery", "food"},
    "travelPurpose.natureExploration": {
        "national_park",
        "park",
        "hiking_area",
        "beach",
        "mountain",
    },
    "travelPurpose.activity": {
        "amusement_park",
        "sports_activity_location",
        "ski_resort",
        "hiking_area",
    },
    "travelPurpose.shopping": {"shopping_mall", "department_store", "store"},
    "travelPurpose.festivalEvent": {"festival", "event_venue"},
    "travelPurpose.wellness": {"spa", "wellness_center", "yoga_studio"},
    "travelPurpose.selfDevelopment": {"library", "educational_institution", "workshop"},
    "preferredLocationType.natureHinterland": {
        "national_park",
        "hiking_area",
        "mountain",
    },
    "preferredLocationType.beachResort": {"beach", "resort_hotel"},
    "preferredLocationType.mountainPlateau": {"mountain", "hiking_area", "ski_resort"},
    "preferredLocationType.historicalCity": {"historical_landmark", "historical_place"},
    "preferredLocationType.themeParkResort": {
        "amusement_park",
        "theme_park",
        "resort_hotel",
    },
    "activityPreference.viewing": {"museum", "art_gallery", "performing_arts_theater"},
    "activityPreference.experience": {
        "workshop",
        "amusement_park",
        "sports_activity_location",
    },
    "activityPreference.adventure": {
        "hiking_area",
        "ski_resort",
        "adventure_sports_center",
    },
    "activityPreference.gourmetExploration": {"restaurant", "cafe", "bakery", "food"},
    "activityPreference.nightlife": {"night_club", "bar"},
    "activityPreference.shopping": {"shopping_mall", "department_store", "store"},
    "activityPreference.relaxation": {"beach", "spa", "resort_hotel"},
    "foodPreference.streetFood": {"food_stall", "street_food"},
    "foodPreference.cafeDessert": {"cafe", "bakery", "dessert_shop", "coffee_shop"},
    "foodPreference.fineDining": {"fine_dining_restaurant"},
}
ENVIRONMENT_TYPES = {
    "summer_resort": ({"beach", "resort_hotel"}, {"summer"}),
    "winter_sports": ({"ski_resort"}, {"winter"}),
}
COMPATIBILITY_DEFAULTS = {
    "companionType": "solo",
    "spendingTendency": "moderate",
    "travelPaceDensity": "balanced",
}


def guard_taste_profile(
    profile: TasteProfileSchema, statistics: dict[str, Any]
) -> tuple[TasteProfileSchema, AnalysisMetadataSchema]:
    """Neutralize unsupported scores and attach explicit evidence metadata.

    Args:
        profile: Structured LLM output, already validated for compatibility.
        statistics: Deterministic visit statistics, including union-based field counts.

    Returns:
        The corrected profile and metadata. Confidence is heuristic, not calibrated.
    """
    result = profile.model_dump()
    fields: dict[str, FieldEvidenceSchema] = {}
    fallbacks: list[str] = []
    confirmations: list[str] = []
    visits = statistics["fieldVisitCounts"]
    days = statistics["fieldDayCounts"]

    def evidence(path: str) -> FieldEvidenceSchema:
        count, day_count = visits.get(path, 0), days.get(path, 0)
        sufficient = count >= 3 and day_count >= 2
        return FieldEvidenceSchema(
            evidence={
                "visitCount": count,
                "distinctDayCount": day_count,
                "matchedPlaceTypes": statistics["fieldMatchedPlaceTypes"].get(path, []),
            },
            confidence="medium" if sufficient else "low",
            insufficientEvidence=not sufficient,
        )

    for group, values in result.items():
        if not isinstance(values, dict):
            continue
        for key in values:
            path = f"{group}.{key}"
            item_evidence = evidence(path)
            fields[path] = item_evidence
            if item_evidence.insufficientEvidence:
                values[key] = 3
                fallbacks.append(path)
                confirmations.append(path)
            elif values[key] < 3:
                values[key] = 3
                fallbacks.append(path)

    for path, value in COMPATIBILITY_DEFAULTS.items():
        result[path] = value
        fields[path] = FieldEvidenceSchema(
            evidence={
                "reason": "Compatibility placeholder; photo metadata cannot infer this field."
            },
            confidence="low",
            insufficientEvidence=True,
        )
        fallbacks.append(path)
        confirmations.append(path)

    allowed = []
    for key in (
        "warm_region",
        "cold_region",
        "summer_resort",
        "winter_sports",
        "spring_flower_autumn_foliage",
        "dry_weather",
        "off_season",
        "peak_season",
    ):
        path = f"seasonalEnvironmentPreference.{key}"
        item_evidence = evidence(path)
        fields[path] = item_evidence
        if (
            not item_evidence.insufficientEvidence
            and key in result["seasonalEnvironmentPreference"]
        ):
            allowed.append(key)
        elif item_evidence.insufficientEvidence:
            fallbacks.append(path)
            confirmations.append(path)
    result["seasonalEnvironmentPreference"] = allowed
    return TasteProfileSchema.model_validate(result), AnalysisMetadataSchema(
        statistics=statistics,
        fieldEvidence=fields,
        fallbackFields=sorted(fallbacks),
        requiresUserConfirmation=sorted(confirmations),
    )

"""Order-independent visit statistics from preprocessed photo metadata."""

import json
import unicodedata
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from app.schemas.behavior import BehaviorItemSchema
from app.services.behavior_evidence import ENVIRONMENT_TYPES, FIELD_TYPES

VISIT_WINDOW = timedelta(hours=2)
UNKNOWN_PLACES = {
    "",
    "unknown",
    "unknown place",
    "unknown_place",
    "알 수 없음",
    "미상",
    "미확인",
    "n/a",
    "null",
    "none",
}


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _bucket(hour: int) -> str:
    if hour < 6:
        return "dawn"
    if hour < 12:
        return "morning"
    if hour < 18:
        return "afternoon"
    if hour < 22:
        return "evening"
    return "night"


def build_behavior_statistics(items: list[BehaviorItemSchema]) -> dict[str, Any]:
    """Deduplicate photos and count anchored visits with normalized place identity.

    Args:
        items: Preprocessed photos. Missing IDs, invalid times and unknown places are excluded.

    Returns:
        JSON-serializable counts, visit distributions and field evidence counts.
        Sufficiency (10 days / 15 places) is an unvalidated sampling heuristic.
    """
    missing_id_count = sum(not item.sourceImageId.strip() for item in items)
    unique: dict[str, BehaviorItemSchema] = {}
    for item in sorted(
        items,
        key=lambda row: json.dumps(
            row.model_dump(), sort_keys=True, ensure_ascii=False
        ),
    ):
        if item.sourceImageId.strip():
            unique.setdefault(item.sourceImageId, item)
    valid = []
    for item in unique.values():
        try:
            captured = datetime.fromisoformat(item.timeContext.capturedAt)
            if captured.tzinfo is None or captured.utcoffset() is None:
                continue
            instant = captured.astimezone(UTC)
        except ValueError, OverflowError:
            continue
        location = item.location
        if _normalize(location.placeName) in UNKNOWN_PLACES:
            continue
        place = tuple(
            _normalize(getattr(location, key))
            for key in ("country", "city", "region", "district", "placeName")
        )
        valid.append(
            (
                place,
                captured.date().isoformat(),
                instant,
                item.sourceImageId,
                captured,
                item,
            )
        )
    valid.sort(key=lambda row: row[:4])
    visits: list[dict[str, Any]] = []
    for place, day, instant, _, captured, item in valid:
        if (
            not visits
            or visits[-1]["place"] != place
            or visits[-1]["day"] != day
            or instant - visits[-1]["instant"] > VISIT_WINDOW
        ):
            visits.append(
                {
                    "place": place,
                    "day": day,
                    "instant": instant,
                    "weekday": ("mon", "tue", "wed", "thu", "fri", "sat", "sun")[
                        captured.weekday()
                    ],
                    "timeBucket": _bucket(captured.hour),
                    "season": item.timeContext.season,
                    "types": set(),
                }
            )
        visits[-1]["types"].update(
            _normalize(value) for value in item.location.placeTypes if _normalize(value)
        )
    categories: Counter[str] = Counter()
    category_days: dict[str, set[str]] = defaultdict(set)
    field_visits: Counter[str] = Counter()
    field_days: dict[str, set[str]] = defaultdict(set)
    field_types: dict[str, set[str]] = defaultdict(set)
    distributions: dict[str, Counter[str]] = {
        key: Counter()
        for key in ("dayOfWeek", "weekend", "timeBucket", "season", "city")
    }
    for visit in visits:
        for category in visit["types"]:
            categories[category] += 1
            category_days[category].add(visit["day"])
        for path, types in FIELD_TYPES.items():
            if visit["types"] & types:
                field_visits[path] += 1
                field_days[path].add(visit["day"])
                field_types[path].update(visit["types"] & types)
        for key, (types, seasons) in ENVIRONMENT_TYPES.items():
            if visit["types"] & types and visit["season"] in seasons:
                path = f"seasonalEnvironmentPreference.{key}"
                field_visits[path] += 1
                field_days[path].add(visit["day"])
                field_types[path].update(visit["types"] & types)
        distributions["dayOfWeek"][visit["weekday"]] += 1
        distributions["weekend"][
            "weekend" if visit["weekday"] in {"sat", "sun"} else "weekday"
        ] += 1
        distributions["timeBucket"][visit["timeBucket"]] += 1
        distributions["season"][visit["season"]] += 1
        distributions["city"][visit["place"][1]] += 1
    day_count = len({visit["day"] for visit in visits})
    place_count = len({visit["place"] for visit in visits})
    unique_count = len(unique) + missing_id_count
    return {
        "inputPhotoCount": len(items),
        "uniquePhotoCount": unique_count,
        "duplicatePhotoCount": len(items) - unique_count,
        "invalidPhotoCount": unique_count - len(valid),
        "validPhotoCount": len(valid),
        "visitCount": len(visits),
        "distinctDayCount": day_count,
        "distinctPlaceCount": place_count,
        "distributions": {
            key: dict(sorted(value.items())) for key, value in distributions.items()
        },
        "placeTypeVisitCounts": dict(sorted(categories.items())),
        "placeTypeDayCounts": {
            key: len(value) for key, value in sorted(category_days.items())
        },
        "fieldVisitCounts": dict(sorted(field_visits.items())),
        "fieldMatchedPlaceTypes": {
            key: sorted(value) for key, value in sorted(field_types.items())
        },
        "fieldDayCounts": {
            key: len(value) for key, value in sorted(field_days.items())
        },
        "dataSufficiency": "sufficient"
        if day_count >= 10 and place_count >= 15
        else "provisional",
    }

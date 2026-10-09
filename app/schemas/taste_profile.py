from typing import Any, Literal

from pydantic import BaseModel, Field

# ----------------- 1. 최종 API 응답용 및 도메인 스키마 -----------------

class TravelPurposeSchema(BaseModel):
    relaxation: int = Field(..., ge=1, le=5, description="휴양형 (1-5)")
    sightseeing: int = Field(..., ge=1, le=5, description="관광형 (1-5)")
    culturalExperience: int = Field(..., ge=1, le=5, description="문화체험형 (1-5)")
    gourmet: int = Field(..., ge=1, le=5, description="미식형 (1-5)")
    natureExploration: int = Field(..., ge=1, le=5, description="자연탐방형 (1-5)")
    activity: int = Field(..., ge=1, le=5, description="액티비티형 (1-5)")
    shopping: int = Field(..., ge=1, le=5, description="쇼핑형 (1-5)")
    festivalEvent: int = Field(..., ge=1, le=5, description="축제·이벤트형 (1-5)")
    wellness: int = Field(..., ge=1, le=5, description="웰니스형 (1-5)")
    selfDevelopment: int = Field(..., ge=1, le=5, description="자기계발형 (1-5)")

class PreferredLocationTypeSchema(BaseModel):
    bigCity: int = Field(..., ge=1, le=5, description="대도시형 (1-5)")
    smallTownAlley: int = Field(..., ge=1, le=5, description="소도시·골목형 (1-5)")
    natureHinterland: int = Field(..., ge=1, le=5, description="자연·오지형 (1-5)")
    beachResort: int = Field(..., ge=1, le=5, description="해변·휴양지형 (1-5)")
    mountainPlateau: int = Field(..., ge=1, le=5, description="산악·고원형 (1-5)")
    historicalCity: int = Field(..., ge=1, le=5, description="역사도시형 (1-5)")
    themeParkResort: int = Field(..., ge=1, le=5, description="테마파크·리조트형 (1-5)")
    famousSpotPreferred: int = Field(..., ge=1, le=5, description="유명 관광지 선호형 (1-5)")
    hiddenSpotPreferred: int = Field(..., ge=1, le=5, description="숨은 명소 선호형 (1-5)")

class ActivityPreferenceSchema(BaseModel):
    viewing: int = Field(..., ge=1, le=5, description="관람형 (1-5)")
    experience: int = Field(..., ge=1, le=5, description="체험형 (1-5)")
    adventure: int = Field(..., ge=1, le=5, description="모험형 (1-5)")
    photographyVideo: int = Field(..., ge=1, le=5, description="사진·영상형 (1-5)")
    gourmetExploration: int = Field(..., ge=1, le=5, description="미식 탐방형 (1-5)")
    nightlife: int = Field(..., ge=1, le=5, description="밤문화형 (1-5)")
    shopping: int = Field(..., ge=1, le=5, description="쇼핑형 (1-5)")
    relaxation: int = Field(..., ge=1, le=5, description="휴식형 (1-5)")
    localInteraction: int = Field(..., ge=1, le=5, description="현지인 교류형 (1-5)")

class FoodPreferenceSchema(BaseModel):
    localFoodActive: int = Field(..., ge=1, le=5, description="현지 음식 적극 체험형 (1-5)")
    famousRestaurantCentered: int = Field(..., ge=1, le=5, description="유명 맛집 중심형 (1-5)")
    streetFood: int = Field(..., ge=1, le=5, description="길거리 음식형 (1-5)")
    cafeDessert: int = Field(..., ge=1, le=5, description="카페·디저트형 (1-5)")
    fineDining: int = Field(..., ge=1, le=5, description="파인다이닝형 (1-5)")
    familiarFoodPreferred: int = Field(..., ge=1, le=5, description="익숙한 음식 선호형 (1-5)")
    dietaryRestriction: int = Field(..., ge=1, le=5, description="식단 제한형 (1-5)")
    sightseeingOverFood: int = Field(..., ge=1, le=5, description="음식보다 관광 중시형 (1-5)")

class TasteProfileSchema(BaseModel):
    travelPurpose: TravelPurposeSchema = Field(..., description="여행 목적 선호도")
    travelPaceDensity: Literal["slow_stay", "balanced", "dense_schedule", "spontaneous", "long_stay"] = Field(..., description="여행 속도/일정 밀도")
    preferredLocationType: PreferredLocationTypeSchema = Field(..., description="선호 장소 유형")
    activityPreference: ActivityPreferenceSchema = Field(..., description="활동 취향")
    spendingTendency: Literal["cost_effective", "moderate", "luxury"] = Field(..., description="소비 성향")
    companionType: Literal[
        "solo", "couple", "friends", "family", "with_children", "with_parents", "group", "with_pet", "social"
    ] = Field(..., description="동행 형태")
    foodPreference: FoodPreferenceSchema = Field(..., description="음식 취향")
    seasonalEnvironmentPreference: list[
        Literal[
            "warm_region", "cold_region", "summer_resort", "winter_sports",
            "spring_flower_autumn_foliage", "dry_weather", "off_season", "peak_season"
        ]
    ] = Field(default_factory=list, description="계절/환경 취향")

class FieldEvidenceSchema(BaseModel):
    """항목별 관측 근거와 휴리스틱 신뢰도. 확률로 해석하지 않습니다."""

    evidence: dict[str, Any] = Field(default_factory=dict, description="관측 방문 수 또는 근거 부족 이유")
    confidence: Literal["low", "medium"] = Field(..., description="검증되지 않은 근거 강도")
    insufficientEvidence: bool = Field(..., description="사용자 확인이 필요한 근거 부족 여부")


class AnalysisMetadataSchema(BaseModel):
    """취향 결과의 통계와 기본값을 구분하는 추가 메타데이터."""

    statistics: dict[str, Any] = Field(..., description="방문 기반 통계 및 표본 충분성")
    fieldEvidence: dict[str, FieldEvidenceSchema] = Field(..., description="프로필 경로별 근거")
    fallbackFields: list[str] = Field(default_factory=list, description="중립값 또는 호환용 기본값을 적용한 경로")
    requiresUserConfirmation: list[str] = Field(default_factory=list, description="확정 취향으로 사용하기 전에 확인할 경로")


class TasteProfileAnalysisOutput(TasteProfileSchema):
    """신규 분석 출력은 계절 선호 최소 1개를 요구하며 기존 입력은 호환합니다."""

    seasonalEnvironmentPreference: list[
        Literal[
            "warm_region", "cold_region", "summer_resort", "winter_sports",
            "spring_flower_autumn_foliage", "dry_weather", "off_season", "peak_season"
        ]
    ] = Field(..., min_length=1, description="관측 또는 잠정 선택한 계절/환경 취향 최소 1개")

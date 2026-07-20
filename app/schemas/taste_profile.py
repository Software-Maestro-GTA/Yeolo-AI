"""
@file taste_profile.py
@description DOM-1 (성향 정보) 및 API-BA-6 응답 스키마와 LLM 체인 출력을 위한 Pydantic 스키마 정의
@requirements REQ-11
@functional FUN-1
@api API-BA-6
@author Antigravity Agent
"""

from typing import List, Literal
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
    sourceType: Literal["survey", "behavior", "mixed"] = Field("behavior", description="성향 생성 방식")
    travelPurpose: TravelPurposeSchema = Field(..., description="여행 목적 선호도")
    travelPaceDensity: Literal["slow_stay", "balanced", "dense_schedule", "spontaneous", "long_stay"] = Field(..., description="여행 속도/일정 밀도")
    preferredLocationType: PreferredLocationTypeSchema = Field(..., description="선호 장소 유형")
    activityPreference: ActivityPreferenceSchema = Field(..., description="활동 취향")
    spendingTendency: Literal["cost_effective", "moderate", "luxury"] = Field(..., description="소비 성향")
    companionType: Literal[
        "solo", "couple", "friends", "family", "with_children", "with_parents", "group", "with_pet", "social"
    ] = Field(..., description="동행 형태")
    foodPreference: FoodPreferenceSchema = Field(..., description="음식 취향")
    seasonalEnvironmentPreference: List[
        Literal[
            "warm_region", "cold_region", "summer_resort", "winter_sports",
            "spring_flower_autumn_foliage", "dry_weather", "off_season", "peak_season"
        ]
    ] = Field(default_factory=list, description="계절/환경 취향")

class BehaviorAnalysisResponse(BaseModel):
    tasteProfile: TasteProfileSchema = Field(..., description="조립된 성향 프로필 결과")

# ----------------- 2. LLM 병렬 채점 체인용 Pydantic Outputs -----------------

class PurposePaceCompanionOutput(BaseModel):
    relaxation: int = Field(..., ge=1, le=5, description="휴양형 선호도 (1-5)")
    sightseeing: int = Field(..., ge=1, le=5, description="관광형 선호도 (1-5)")
    culturalExperience: int = Field(..., ge=1, le=5, description="문화체험형 선호도 (1-5)")
    gourmet: int = Field(..., ge=1, le=5, description="미식형 선호도 (1-5)")
    natureExploration: int = Field(..., ge=1, le=5, description="자연탐방형 선호도 (1-5)")
    activity: int = Field(..., ge=1, le=5, description="액티비티형 선호도 (1-5)")
    shopping: int = Field(..., ge=1, le=5, description="쇼핑형 선호도 (1-5)")
    festivalEvent: int = Field(..., ge=1, le=5, description="축제·이벤트형 선호도 (1-5)")
    wellness: int = Field(..., ge=1, le=5, description="웰니스형 선호도 (1-5)")
    selfDevelopment: int = Field(..., ge=1, le=5, description="자기계발형 선호도 (1-5)")
    travelPaceDensity: Literal["slow_stay", "balanced", "dense_schedule", "spontaneous", "long_stay"] = Field(..., description="여행 속도/일정 밀도")
    companionType: Literal[
        "solo", "couple", "friends", "family", "with_children", "with_parents", "group", "with_pet", "social"
    ] = Field(..., description="동행 형태")

class LocationEnvironmentOutput(BaseModel):
    bigCity: int = Field(..., ge=1, le=5, description="대도시형 선호도 (1-5)")
    smallTownAlley: int = Field(..., ge=1, le=5, description="소도시·골목형 선호도 (1-5)")
    natureHinterland: int = Field(..., ge=1, le=5, description="자연·오지형 선호도 (1-5)")
    beachResort: int = Field(..., ge=1, le=5, description="해변·휴양지형 선호도 (1-5)")
    mountainPlateau: int = Field(..., ge=1, le=5, description="산악·고원형 선호도 (1-5)")
    historicalCity: int = Field(..., ge=1, le=5, description="역사도시형 선호도 (1-5)")
    themeParkResort: int = Field(..., ge=1, le=5, description="테마파크·리조트형 선호도 (1-5)")
    famousSpotPreferred: int = Field(..., ge=1, le=5, description="유명 관광지 선호도 (1-5)")
    hiddenSpotPreferred: int = Field(..., ge=1, le=5, description="숨은 명소 선호도 (1-5)")
    # 계절/환경 선호 Boolean 필드
    warm_region: bool = Field(..., description="따뜻한 지역 선호 여부")
    cold_region: bool = Field(..., description="추운 지역 선호 여부")
    summer_resort: bool = Field(..., description="여름 휴양 선호 여부")
    winter_sports: bool = Field(..., description="겨울 스포츠 선호 여부")
    spring_flower_autumn_foliage: bool = Field(..., description="봄꽃·가을 단풍 선호 여부")
    dry_weather: bool = Field(..., description="건조한 날씨 선호 여부")
    off_season: bool = Field(..., description="비수기 여행 선호 여부")
    peak_season: bool = Field(..., description="성수기 분위기 선호 여부")

class ActivityFoodSpendingOutput(BaseModel):
    viewing: int = Field(..., ge=1, le=5, description="관람형 선호도 (1-5)")
    experience: int = Field(..., ge=1, le=5, description="체험형 선호도 (1-5)")
    adventure: int = Field(..., ge=1, le=5, description="모험형 선호도 (1-5)")
    photographyVideo: int = Field(..., ge=1, le=5, description="사진·영상형 선호도 (1-5)")
    gourmetExploration: int = Field(..., ge=1, le=5, description="미식 탐방형 선호도 (1-5)")
    nightlife: int = Field(..., ge=1, le=5, description="밤문화형 선호도 (1-5)")
    shopping: int = Field(..., ge=1, le=5, description="쇼핑형 선호도 (1-5)")
    relaxation: int = Field(..., ge=1, le=5, description="휴식형 선호도 (1-5)")
    localInteraction: int = Field(..., ge=1, le=5, description="현지인 교류형 선호도 (1-5)")
    spendingTendency: Literal["cost_effective", "moderate", "luxury"] = Field(..., description="소비 성향")
    localFoodActive: int = Field(..., ge=1, le=5, description="현지 음식 적극 체험형 선호도 (1-5)")
    famousRestaurantCentered: int = Field(..., ge=1, le=5, description="유명 맛집 중심형 선호도 (1-5)")
    streetFood: int = Field(..., ge=1, le=5, description="길거리 음식형 선호도 (1-5)")
    cafeDessert: int = Field(..., ge=1, le=5, description="카페·디저트형 선호도 (1-5)")
    fineDining: int = Field(..., ge=1, le=5, description="파인다이닝형 선호도 (1-5)")
    familiarFoodPreferred: int = Field(..., ge=1, le=5, description="익숙한 음식 선호형 선호도 (1-5)")
    dietaryRestriction: int = Field(..., ge=1, le=5, description="식단 제한형 선호도 (1-5)")
    sightseeingOverFood: int = Field(..., ge=1, le=5, description="음식보다 관광 중시형 선호도 (1-5)")

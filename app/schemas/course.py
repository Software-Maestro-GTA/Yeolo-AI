"""Public request and compact SSE response contract for travel course generation."""

import re
from typing import Literal, Self
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    Field,
    FieldSerializationInfo,
    field_serializer,
    field_validator,
    model_validator,
)

from app.schemas.taste_profile import TasteProfileSchema
from app.services.place_categories import (
    PLACE_CATEGORY_LABELS,
    PlaceCategoryCode,
    map_actual_place_category,
)


class TripConditionSchema(BaseModel):
    destinationCountry: str = Field(..., min_length=1, description="목적지 국가")
    destinationCity: str = Field(..., min_length=1, description="목적지 도시")
    startDate: str = Field(
        ..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="시작일 (YYYY-MM-DD)"
    )
    totalDays: int = Field(..., ge=1, description="총 여행 일수")
    budget: float = Field(
        ...,
        strict=True,
        ge=0,
        allow_inf_nan=False,
        description="전체 여행 예상 예산 금액 (KRW), 초과해도 생성 허용",
    )


class CourseRequestSchema(BaseModel):
    userId: UUID = Field(..., description="사용자 ID (UUID)")
    language: str = Field(
        ..., description="호환용 BCP 47 언어 코드; 표시 문구는 한국어, 이동 안내는 공급자 원문"
    )
    mbti: str | None = Field(None, description="사용자 MBTI")
    tasteProfile: TasteProfileSchema | None = Field(
        None, description="성향 프로필 데이터"
    )
    tripCondition: TripConditionSchema = Field(..., description="여행 제약 조건")

    @field_validator("language")
    @classmethod
    def normalize_language(cls, value: str) -> str:
        """Accept legacy locale notation and use Korean for all output."""
        value = value.strip().replace("_", "-")
        if not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", value):
            raise ValueError("language must be a BCP 47 language code")
        return "ko"

    @model_validator(mode="after")
    def check_mbti_or_taste_profile(self) -> Self:
        if not self.mbti and not self.tasteProfile:
            raise ValueError("mbti 또는 tasteProfile 중 최소 하나는 전달되어야 합니다.")
        return self


class PlaceSchema(BaseModel):
    """Verified identity and map marker only; rich details stay in Google Maps."""

    placeId: str = Field(
        ..., validation_alias=AliasChoices("googlePlaceId", "placeId"),
        serialization_alias="googlePlaceId", description="검증된 Google Maps Place ID",
    )
    placeName: str = Field(..., description="한국어로 표기한 검증 장소명; Google 공식 표시 이름은 조회하지 않음")
    category: PlaceCategoryCode = Field(
        ...,
        description="앱 장소 분류: FOOD(맛집·미식), CAFE(카페·디저트), SPOT(스팟·명소), CULTURE(문화·전시), NATURE(자연·산책), ACTIVITY(액티비티·체험), NIGHTLIFE(야경·나이트), LODGING(숙소), AIRPORT(공항)",
        json_schema_extra={"enum": list(PLACE_CATEGORY_LABELS)},
    )
    latitude: float = Field(..., description="위도")
    longitude: float = Field(..., description="경도")

    @field_serializer("placeId")
    def serialize_place_id(self, value: str, info: FieldSerializationInfo) -> str:
        """Return the standalone public ID while preserving internal resource names.

        Args:
            value: Verified ID or internally normalized ``places/ID`` value.
            info: Serialization context identifying the public course response.
        Returns:
            Standalone ID for public responses, or the unchanged internal value.
        """
        if isinstance(info.context, dict) and info.context.get("public_response"):
            return value.removeprefix("places/")
        return value

    @field_serializer("category")
    def serialize_category(self, value: str, info: FieldSerializationInfo) -> str:
        """Map only explicit public output; keep raw types in internal dumps."""
        if isinstance(info.context, dict) and info.context.get("public_response"):
            return map_actual_place_category(value, getattr(self, "providerTypes", ()))
        return value


class TransportToNextSchema(BaseModel):
    type: Literal["walking", "transit", "driving", "taxi", "unknown", "none"] = Field(
        ..., description="다음 장소까지 이동 수단; unknown은 조회 미확보, none은 다음 이동 없음"
    )
    distance: float | None = Field(None, description="실제 이동 거리 (미터)")
    minutes: int | None = Field(None, description="실제 이동 소요 시간 (분)")
    memo: str | None = Field(None, description="공급자 경로 응답의 원문 이동 안내; 확보한 정보가 없으면 null")

    @model_validator(mode="after")
    def check_unknown_metrics(self) -> Self:
        """Unknown transfers cannot masquerade as measured or free travel."""
        if self.type == "unknown" and (
            self.distance is not None or self.minutes is not None
            or getattr(self, "cost", None) is not None
        ):
            raise ValueError("unknown transport requires null distance, minutes and fare")
        return self


class StopSchema(BaseModel):
    sequence: int = Field(..., ge=1, description="방문 순서")
    stayMinutes: int = Field(..., ge=1, description="체류 시간 (분)")
    memo: str = Field(..., description="검증을 통과한 LLM 방문 팁; 생성·검증 미확보 시 빈 문자열")
    reason: str = Field(..., description="취향과 검증된 장소에 근거한 LLM 추천 이유; 생성·검증 미확보 시 빈 문자열")
    cost: int = Field(default=0, ge=0, description="예상 활동·식사·입장 비용 또는 해당 일차 숙박 비용 (KRW); 첫날·마지막 날 공항은 왕복 항공료를 나눈 편도 예상 배분액이며 실제 편도 견적은 아님. 항공·숙박 비용은 totalCost에 중복 합산하지 않음")
    place: PlaceSchema = Field(..., description="지도 연결용 장소 정보")
    transportToNext: TransportToNextSchema = Field(
        ..., description="다음 장소까지 실제 이동 정보"
    )


class DayItinerarySchema(BaseModel):
    day: int = Field(..., ge=1, description="일차")
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="날짜")
    stops: list[StopSchema] = Field(..., description="일자별 방문 장소")


class ItinerarySchema(BaseModel):
    days: list[DayItinerarySchema] = Field(..., description="일자별 여정")


class CourseSchema(BaseModel):
    title: str = Field(..., description="한국어로 작성된 코스 제목")
    destinationCountry: str = Field(..., description="목적지 국가")
    destinationCity: str = Field(..., description="목적지 도시")
    startDate: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="시작일")
    totalDays: int = Field(..., ge=1, description="총 여행 일수")
    totalCost: int = Field(
        default=0,
        ge=0,
        description="일정 비용과 왕복 교통·숙박 실조회 또는 기준 추정액의 합계 (KRW); 공항·숙소 stop 비용은 중복 합산하지 않음",
    )
    tags: list[str] = Field(
        default_factory=list, description="한국어로 작성된 여행 태그"
    )
    itinerary: ItinerarySchema = Field(..., description="확정된 세부 여정")


class CourseResponseSchema(BaseModel):
    course: CourseSchema = Field(..., description="생성된 여행 코스")


class ProgressEventData(BaseModel):
    step: Literal["GENERATING_ROUTE"] = "GENERATING_ROUTE"
    message: str = Field(..., description="한국어로 작성된 진행 안내")

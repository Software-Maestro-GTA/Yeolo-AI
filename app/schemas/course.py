from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from app.schemas.taste_profile import TasteProfileSchema


class TripConditionSchema(BaseModel):
    destinationCountry: str = Field(..., description="목적지 국가")
    destinationCity: str = Field(..., description="목적지 도시")
    startDate: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="시작일 (YYYY-MM-DD)")
    totalDays: int = Field(..., ge=1, description="총 여행 일수")
    budgetType: Literal["cost_effective", "moderate", "luxury"] = Field(..., description="예산 유형")


class CourseRequestSchema(BaseModel):
    userId: UUID = Field(..., description="사용자 ID (UUID)")
    mbti: str | None = Field(None, description="사용자 MBTI (string|null)")
    tasteProfile: TasteProfileSchema | None = Field(None, description="성향 프로필 데이터 (object|null)")
    tripCondition: TripConditionSchema = Field(..., description="여행 제약 조건")

    @model_validator(mode="after")
    def check_mbti_or_taste_profile(self):
        if not self.mbti and not self.tasteProfile:
            raise ValueError("mbti 또는 tasteProfile 중 최소 하나는 전달되어야 합니다.")
        return self


class PlaceSchema(BaseModel):
    placeId: str = Field(..., description="Google Maps Place ID 또는 서비스 장소 식별자")
    placeName: str = Field(..., description="장소 한글 명칭")
    placeEngName: str = Field(default="", description="장소 영문 명칭")
    category: str = Field(..., description="장소 카테고리")
    address: str = Field(default="", description="장소 주소")
    latitude: float = Field(..., description="위도")
    longitude: float = Field(..., description="경도")
    rating: float | None = Field(None, description="장소 평점")
    photoUrl: str = Field(default="", description="대표 사진 URL")
    openingHours: list[str] = Field(default_factory=list, description="영업시간 정보 목록")


class TransportToNextSchema(BaseModel):
    type: Literal["walking", "transit", "driving", "taxi", "none"] = Field(..., description="다음 장소까지 이동 수단")
    distance: float | None = Field(None, description="이동 거리 (미터)")
    minutes: int | None = Field(None, description="이동 소요 시간 (분)")
    cost: int | None = Field(None, description="예상 이동 비용 (원)")
    memo: str | None = Field(None, description="이동 관련 참고 메모")


class StopSchema(BaseModel):
    sequence: int = Field(..., description="방문 순서 (1부터 시작)")
    arrivalTime: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="도착 시각 (HH:mm)")
    stayMinutes: int = Field(..., ge=1, description="체류 시간 (분)")
    memo: str = Field(..., description="메모 및 안내 사항")
    reason: str = Field(..., description="해당 장소 추천 이유")
    place: PlaceSchema = Field(..., description="장소 상세 정보 (Google Maps API 연동)")
    transportToNext: TransportToNextSchema = Field(..., description="다음 장소까지 이동 정보 (Google Routes API 연동)")


class DayItinerarySchema(BaseModel):
    day: int = Field(..., description="일차 (1부터 시작)")
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="날짜 (YYYY-MM-DD)")
    memo: str = Field(..., description="해당 일자 요약 메모")
    stops: list[StopSchema] = Field(..., description="일자별 스톱 목록")


class ItinerarySchema(BaseModel):
    days: list[DayItinerarySchema] = Field(..., description="일자별 여정 목록")


class CourseSchema(BaseModel):
    title: str = Field(..., description="코스 제목")
    destinationCountry: str = Field(..., description="목적지 국가")
    destinationCity: str = Field(..., description="목적지 도시")
    coverImageUrl: str = Field(default="", description="코스 커버 이미지 URL")
    startDate: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="시작일 (YYYY-MM-DD)")
    totalDays: int = Field(..., ge=1, description="총 여행 일수")
    tags: list[str] = Field(default_factory=list, description="코스 태그 목록")
    recommendationReason: str = Field(..., description="전체 코스 핵심 추천 이유")
    itinerary: ItinerarySchema = Field(..., description="일자별 세부 여정")


class CourseResponseSchema(BaseModel):
    course: CourseSchema = Field(..., description="생성된 여행 코스 정보")


class ProgressEventData(BaseModel):
    step: str = Field(..., description="진행 단계 코드")
    message: str = Field(..., description="진행 단계 안내 메시지")

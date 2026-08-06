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


class StopSchema(BaseModel):
    sequence: int = Field(..., description="방문 순서 (1부터 시작)")
    placeName: str = Field(..., description="장소명")
    category: str = Field(..., description="장소 카테고리")
    arrivalTime: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="도착 시각 (HH:mm)")
    stayMinutes: int = Field(..., ge=1, description="체류 시간 (분)")
    memo: str = Field(..., description="메모 및 안내 사항")
    transportToNext: Literal["walking", "transit", "driving", "taxi", "none"] = Field(..., description="다음 장소까지 이동 수단")
    travelMinutesToNext: int = Field(..., ge=0, description="다음 장소까지 소요 시간 (분)")
    cost: int = Field(..., ge=0, description="예상 비용 (원)")
    reason: str = Field(..., description="해당 장소 추천 이유")


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

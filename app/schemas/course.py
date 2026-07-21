"""
@file course.py
@description API-BA-1 및 DOM-2 (코스 정보) 사양에 따른 여행 코스 생성 요청/응답 Pydantic 스키마 정의
@requirements REQ-7
@functional FUN-2
@api API-BA-1
@author Antigravity Agent
"""

from typing import List, Literal
from uuid import UUID
from pydantic import BaseModel, Field
from app.schemas.taste_profile import TasteProfileSchema


class TripConditionSchema(BaseModel):
    destinationCountry: str = Field(..., description="목적지 국가")
    destinationCity: str = Field(..., description="목적지 도시")
    startDate: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="시작일 (YYYY-MM-DD)")
    totalDays: int = Field(..., ge=1, description="총 여행 일수")
    budgetType: Literal["cost_effective", "standard", "luxury"] = Field(..., description="예산 유형")


class CourseRequestSchema(BaseModel):
    userId: UUID = Field(..., description="사용자 ID (UUID)")
    tasteProfile: TasteProfileSchema = Field(..., description="성향 프로필 데이터")
    tripCondition: TripConditionSchema = Field(..., description="여행 제약 조건")


class StopSchema(BaseModel):
    sequence: int = Field(..., description="방문 순서 (1부터 시작)")
    placeId: str = Field(..., description="장소 고유 식별자")
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
    stops: List[StopSchema] = Field(..., description="일자별 스톱 목록")


class ItinerarySchema(BaseModel):
    days: List[DayItinerarySchema] = Field(..., description="일자별 여정 목록")


class ConstraintsSchema(BaseModel):
    budgetType: Literal["cost_effective", "standard", "luxury"] = Field(..., description="예산 유형")
    maxTravelMinutesPerDay: int = Field(..., description="하루 최대 총 이동 시간 (분)")
    preferredTransport: List[Literal["walking", "transit", "driving", "taxi"]] = Field(..., description="선호 이동 수단")
    pace: Literal["relaxed", "balanced", "dense"] = Field(..., description="일정 밀도 (pace)")


class CourseSchema(BaseModel):
    title: str = Field(..., description="코스 제목")
    destinationCountry: str = Field(..., description="목적지 국가")
    destinationCity: str = Field(..., description="목적지 도시")
    startDate: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$", description="시작일 (YYYY-MM-DD)")
    totalDays: int = Field(..., ge=1, description="총 여행 일수")
    totalCost: int = Field(..., ge=0, description="전체 코스 총 예상 비용")
    tags: List[str] = Field(default_factory=list, description="코스 태그 목록")
    recommendationReason: str = Field(..., description="전체 코스 핵심 추천 이유")
    constraints: ConstraintsSchema = Field(..., description="적용된 제약 조건")
    itinerary: ItinerarySchema = Field(..., description="일자별 세부 여정")



class CourseResponseSchema(BaseModel):
    course: CourseSchema = Field(..., description="생성된 여행 코스 정보")


class ProgressEventData(BaseModel):
    step: str = Field(..., description="진행 단계 코드")
    message: str = Field(..., description="진행 단계 안내 메시지")

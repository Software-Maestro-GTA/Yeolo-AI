"""
@file behavior.py
@description API-BA-6 (전처리 이미지 메타데이터 기반 성향 분석) API의 요청 본문 검증을 위한 Pydantic 스키마 정의
@requirements REQ-11
@functional FUN-1
@api API-BA-6
@author Antigravity Agent
"""

from typing import List, Literal
from pydantic import BaseModel, Field, UUID4

class LocationSchema(BaseModel):
    country: str = Field(..., description="국가 정보")
    city: str = Field(..., description="도시 정보")
    region: str = Field(..., description="지역(도/시) 정보")
    district: str = Field(..., description="상세 행정구역(구/동) 정보")
    placeName: str = Field(..., description="장소명")
    placeTypes: List[str] = Field(default_factory=list, description="장소 유형 카테고리 목록")

class TimeContextSchema(BaseModel):
    capturedAt: str = Field(..., description="촬영 시간 (ISO-8601 형식)")
    dayOfWeek: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = Field(..., description="요일")
    isWeekend: bool = Field(..., description="주말 여부")
    timeBucket: Literal["dawn", "morning", "afternoon", "evening", "night"] = Field(..., description="시간대 버킷")
    season: Literal["spring", "summer", "autumn", "winter"] = Field(..., description="계절 정보")

class BehaviorItemSchema(BaseModel):
    sourceImageId: str = Field(..., description="전처리 이미지 ID")
    location: LocationSchema = Field(..., description="위치 정보 객체")
    timeContext: TimeContextSchema = Field(..., description="시간 정보 객체")

class BehaviorAnalysisRequest(BaseModel):
    userId: UUID4 = Field(..., description="사용자 ID (UUID4 형식)")
    items: List[BehaviorItemSchema] = Field(..., description="전처리된 이미지 메타데이터 목록")

"""Existing photo metadata request plus a normalized summary output language."""

import re
from typing import Literal

from pydantic import UUID4, BaseModel, Field, field_validator


class LocationSchema(BaseModel):
    country: str = Field(..., description="국가 정보")
    city: str = Field(..., description="도시 정보")
    region: str = Field(..., description="지역(도/시) 정보")
    district: str = Field(..., description="상세 행정구역(구/동) 정보")
    placeName: str = Field(..., description="장소명")
    placeTypes: list[str] = Field(default_factory=list, description="장소 유형 카테고리 목록")

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
    language: str = Field(default="ko", description="호환용 BCP 47 언어 코드; 값과 관계없이 요약은 항상 한국어")
    items: list[BehaviorItemSchema] = Field(..., description="전처리된 이미지 메타데이터 목록")

    @field_validator("language")
    @classmethod
    def normalize_language(cls, value: str) -> str:
        """Validate legacy locale input and use Korean for all output."""
        value = value.strip().replace("_", "-")
        if len(value) > 35 or not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", value):
            raise ValueError("language must be a BCP 47 language code")
        return "ko"

def language_from_header(value: str) -> str:
    """Accept a legacy Accept-Language header and always select Korean."""
    return "ko"

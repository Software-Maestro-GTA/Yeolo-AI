# 방문 근거 기반 취향 분석 (2026-10-03)

사용자 사진 수집·선택은 Yeolo-FE의 책임이다. Yeolo-AI는 기존 `BehaviorAnalysisRequest`(userId, 사진 ID, 전처리 장소/시각)를 그대로 받는다. 사진 픽셀, 추가 위경도, 장소 ID를 요구하지 않는다.

## 실행 흐름

1. 빈 문자열/공백 `sourceImageId`는 식별할 수 없으므로 제외하며 각각 별도의 무효 항목으로 센다(빈 ID끼리 중복으로 합치지 않는다). 나머지 동일 `sourceImageId`를 제거한다. 동일 ID의 내용이 충돌하면 전체 레코드의 정렬된 JSON을 비교해 결정적으로 한 레코드를 선택한다. 입력 순서는 결과에 영향을 주지 않는다.
2. timezone이 있는 ISO 촬영 시각과 확인 가능한 장소명이 있는 항목만 사용한다. 빈 장소명, `unknown`, `unknown_place`, `알 수 없음` 등은 제외한다. 모두 무효면 LLM 호출 전에 HTTP 400을 반환한다.
3. 국가·도시·지역·행정구역·장소명의 NFKC, 공백 축약, casefold 결과와 **촬영 시각의 현지 날짜**로 장소/날짜를 구분한다. UTC 시각 순으로 정렬해 첫 사진부터 2시간 이내를 한 방문으로 묶는다. 첫 시각 기준이므로 연속 사진이 이어져도 방문이 무한히 확장되지 않는다. 다른 날짜는 별도 방문이다. 안정적인 placeId가 없으므로 이름/행정구역 변경과 동일 이름 장소의 구분에는 한계가 있다.
4. 방문 유형은 같은 방문 안에서 집합으로 합친다. 방문 수, 서로 다른 날짜/장소 수, 유형별 방문/날짜 수, 각 프로필 항목의 합집합 방문/날짜 수를 계산한다. 한 방문에 cafe와 food가 함께 있어도 gourmet 방문 근거는 1회다. 사진 수는 데이터 품질 정보이며 선호 강도의 근거가 아니다.
5. 구조화 방문 통계 JSON을 요약 LLM과 세 병렬 채점 LLM에 직접 전달한다. 요약은 보조 설명이다. 요약 오류/빈 응답이면 통계로 채점을 계속하고, provider content blocks 중 텍스트만 전달한다. 채점 오류는 기존 SSE error로 반환한다.
6. 구조화 취향 결과에 코드 기반 근거 보정을 적용한다. 기존 취향 스키마와 enum을 유지하고, `complete.data.analysisMetadata`를 추가한다.

요일·주말 여부와 시간대는 방문 대표 촬영 시각에서 다시 계산한다. 시간대의 현재 휴리스틱은 00–05시 dawn, 06–11시 morning, 12–17시 afternoon, 18–21시 evening, 22–23시 night이다. 방문의 첫 레코드를 대표로 사용한다. 계절은 국가별 반구/기후를 판단할 정보가 부족하므로 BE가 전달한 season을 그대로 사용한다. 도시 분포는 정규화 명칭으로 제공하지만, 개별 방문 시각/사진 ID/장소명 목록과 userId를 LLM에 보내지 않는다. 유형 통계는 상위 10개로 자르지 않고 모두 보존한다.

## 점수와 근거

`fieldVisitCounts`, `fieldDayCounts`는 프로필 경로별 유형 합집합으로 집계한다. `fieldMatchedPlaceTypes`와 항목 근거의 `matchedPlaceTypes`는 해당 항목의 조건을 실제 만족한 방문에서만 모은 유형이다. 계절 항목은 유형과 계절이 모두 맞아야 하므로 겨울 리조트 기록은 여름 휴양 근거에 들어가지 않는다. 관측 방문 3회 이상 **그리고** 서로 다른 날짜 2일 이상일 때만 반복 근거가 있다고 판단하고 LLM의 4–5점을 허용한다. 그보다 적은 표본 또는 근거 없는 항목은 정확히 3점으로 보정한다. 3점은 중립/미확정이며 비선호라는 뜻이 아니다. 반복 관측이 있어도 이 입력으로 부정적인 선호를 알 수 없으므로 1–2점은 3점으로 보정한다. confidence는 low/medium 두 단계의 미검증 휴리스틱이며 정확도 확률이 아니다.

근거 유형 매핑은 `app/services/behavior_evidence.py`의 `FIELD_TYPES`에 명시한다. 카페/베이커리는 카페·디저트 근거이고, bar/night_club은 밤문화 근거이다. 야간 사진만으로 밤문화를 추론하지 않는다. 일반 restaurant만으로 현지식·유명 맛집·파인다이닝을 추론하지 않는다. 유형 기반 근거도 서비스 이용 목적이나 선호를 확정하지 않으며, 유형 자체는 전처리의 품질에 의존한다.

동행, 소비, 일정 밀도는 메타데이터로 확정할 수 없다. 기존 enum을 유지하기 위해 각각 `solo`, `moderate`, `balanced`를 **호환용 기본값**으로 넣고, fieldEvidence와 fallbackFields/requiresUserConfirmation에 표시한다. 이 값은 추론된 사실이 아니다. 사진 자체 취향, 현지인 교류, 식단 제한, 익숙한 음식, 유명/숨은 명소 선호, 음식보다 관광 선호 등 직접 근거가 없는 점수도 중립값과 확인 대상으로 표시한다. 기후·날씨·성수기·비수기 등 미관측 환경 값은 제거한다. 여름 beach/resort_hotel 또는 겨울 ski_resort 방문이 반복될 때만 summer_resort/winter_sports를 허용한다.

전체 표본은 서로 다른 날짜 10일 이상 및 장소 15곳 이상이면 `dataSufficiency=sufficient`, 그 외에는 `provisional`이다. 이는 검증되지 않은 시작 기준이고 분석을 막는 최소 조건이 아니다. 개별 항목 근거와 전체 표본 충분성은 별도 판단이다.

## 응답 계약

```json
{
  "event": "complete",
  "data": {
    "tasteProfile": "기존 키/enum/점수 범위 유지",
    "analysisMetadata": {
      "statistics": "방문 기반 통계 및 dataSufficiency",
      "fieldEvidence": {
        "foodPreference.cafeDessert": {
          "evidence": {"visitCount": 3, "distinctDayCount": 2, "matchedPlaceTypes": ["cafe"]},
          "confidence": "medium",
          "insufficientEvidence": false
        }
      },
      "fallbackFields": ["companionType", "spendingTendency", "travelPaceDensity"],
      "requiresUserConfirmation": ["companionType", "spendingTendency", "travelPaceDensity"]
    }
  }
}
```

위 JSON은 필드 역할을 보여주는 설명용 예시이며 실제 전체 응답은 기존 tasteProfile 객체와 모든 항목의 근거를 포함한다. fallbackFields에는 중립 점수 또는 호환용 enum을 적용한 프로필 경로를, requiresUserConfirmation에는 확정 취향으로 사용하기 전 확인할 경로를 넣는다. 환경 경로는 `seasonalEnvironmentPreference.summer_resort`처럼 표시한다.

**BE/추천 소비자는 analysisMetadata를 보존하고 확인 대상 값을 확정 취향으로 사용하지 않아야 한다.** 기존 소비자가 tasteProfile만 사용하면 호환 기본값을 확정값으로 오해할 수 있다. FE/BE 및 추천 요청 스키마의 메타데이터 전달은 이번 변경 범위 밖이고, 사용자 확인값을 사진 추정보다 우선하도록 후속 연동이 필요하다.

## 검증 범위와 남은 한계

모킹된 테스트로 중복/연속 촬영 편향, 방문 창·현지 날짜·입력 순서, 결측 제외, 합집합 집계, JSON 직접 전달, 중립 보정, 요약 실패 복구, SSE 오류와 공개 스키마 호환성을 검증한다. 실제 LLM 정확도, 방문 묶기 2시간 기준, 3회/2일 근거 기준, 전체 표본 10일/15장소 기준은 사용자 평가로 보정해야 한다. 추가 사진의 선택 편향·생활 기록과 여행 기록의 혼재를 이 데이터만으로 제거하지 않는다. 사용자별 학습/누적 프로필 저장 기능은 추가하지 않는다.

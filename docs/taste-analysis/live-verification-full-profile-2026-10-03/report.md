# 전체 취향 분석 실제 LLM 검증

2026-10-03. 외부 LLM을 모킹하지 않고 로컬 FastAPI의 실제 취향 분석 API를 합성 데이터로 한 번 호출했다.

- 모델: gemini-3.6-flash
- LLM 요청: 1회, LangSmith 원격 기록으로 확인
- API 완료: 9.64초 / 원격 추적 확인 포함: 14.23초
- 토큰: 3,277
- SSE: progress → complete, 오류 없음
- 규칙·계약 검사: 51/51 통과
- 사진 49개 → 중복 3개·무효 2개 제외 → 유효 사진 44개 → 방문 32개
- 날짜 12일·장소 32곳. 카페 연속 촬영 12장은 같은 방문으로 묶였고 카페 근거는 6방문이었다.
- [LangSmith 전체 실행 기록](https://smith.langchain.com/o/b034a9ef-b137-4888-8542-f24dee39a274/projects/p/ec2264ca-6e24-4370-9724-55072ba70a7e/r/01a100ad-a186-7293-87dc-698aa8084dc8?poll=true)

## 전체 프로필의 최종 점수

근거가 부족한 항목의 3점은 중립·미확정이다. 4~5점은 반복 방문 근거를 충족하는 경우만 허용했다.

| 항목 | 최종 점수 | 근거 방문 | 근거 날짜 | 근거 강도 |
|---|---:|---:|---:|---|
| `travelPurpose.relaxation` | 4 | 6 | 3 | medium |
| `travelPurpose.sightseeing` | 4 | 3 | 3 | medium |
| `travelPurpose.culturalExperience` | 4 | 3 | 3 | medium |
| `travelPurpose.gourmet` | 5 | 10 | 6 | medium |
| `travelPurpose.natureExploration` | 4 | 3 | 3 | medium |
| `travelPurpose.activity` | 4 | 3 | 3 | medium |
| `travelPurpose.shopping` | 4 | 3 | 3 | medium |
| `travelPurpose.festivalEvent` | 3 | 0 | 0 | low |
| `travelPurpose.wellness` | 4 | 3 | 3 | medium |
| `travelPurpose.selfDevelopment` | 3 | 0 | 0 | low |
| `preferredLocationType.bigCity` | 3 | 0 | 0 | low |
| `preferredLocationType.smallTownAlley` | 3 | 0 | 0 | low |
| `preferredLocationType.natureHinterland` | 3 | 0 | 0 | low |
| `preferredLocationType.beachResort` | 4 | 3 | 3 | medium |
| `preferredLocationType.mountainPlateau` | 4 | 3 | 3 | medium |
| `preferredLocationType.historicalCity` | 3 | 0 | 0 | low |
| `preferredLocationType.themeParkResort` | 3 | 0 | 0 | low |
| `preferredLocationType.famousSpotPreferred` | 3 | 0 | 0 | low |
| `preferredLocationType.hiddenSpotPreferred` | 3 | 0 | 0 | low |
| `activityPreference.viewing` | 4 | 3 | 3 | medium |
| `activityPreference.experience` | 3 | 0 | 0 | low |
| `activityPreference.adventure` | 4 | 3 | 3 | medium |
| `activityPreference.photographyVideo` | 3 | 0 | 0 | low |
| `activityPreference.gourmetExploration` | 5 | 10 | 6 | medium |
| `activityPreference.nightlife` | 4 | 3 | 3 | medium |
| `activityPreference.shopping` | 4 | 3 | 3 | medium |
| `activityPreference.relaxation` | 4 | 6 | 3 | medium |
| `activityPreference.localInteraction` | 3 | 0 | 0 | low |
| `foodPreference.localFoodActive` | 3 | 0 | 0 | low |
| `foodPreference.famousRestaurantCentered` | 3 | 0 | 0 | low |
| `foodPreference.streetFood` | 3 | 0 | 0 | low |
| `foodPreference.cafeDessert` | 5 | 6 | 6 | medium |
| `foodPreference.fineDining` | 3 | 1 | 1 | low |
| `foodPreference.familiarFoodPreferred` | 3 | 0 | 0 | low |
| `foodPreference.dietaryRestriction` | 3 | 0 | 0 | low |
| `foodPreference.sightseeingOverFood` | 3 | 0 | 0 | low |

## 선택형 항목

- 일정 밀도: `balanced`
- 소비: `moderate`
- 동행: `solo`
- 위 세 값은 사진으로 확인한 취향이 아닌 호환 기본값이다. 내부 trace에서 사용자 확인 대상으로 기록됐음을 확인했다.
- 계절·환경: `summer_resort`, `winter_sports`, `spring_flower_autumn_foliage`
- 계절 후보 3개 모두 장소 유형과 계절이 맞는 3방문·3일 반복 근거로 선택됐다.

## 관측과 범위

미식 목적·미식 탐방·카페 디저트는 5점, 문화체험·밤문화·쇼핑·웰니스는 4점이었다. 1회 관측된 파인다이닝과 근거 없는 식단 제한은 3점으로 보정됐다. 점수 36개, enum 기본값 3개, 계절 목록 및 내부 근거 저장을 모두 확인했다.

이 결과는 실행과 보정 정책의 검증이다. 사용자 정답과 비교한 정확도 평가나 LLM 반복 안정성 검증은 아니다.

## 재현 및 원본

`uv run python docs/taste-analysis/live-verification-full-profile-2026-10-03/verify.py`

재실행하면 실제 LLM에 새 요청 1회가 발생하며 이 폴더의 결과 파일을 갱신한다.

- [합성 요청](request.json)
- [실제 SSE 이벤트](events.json)
- [전체 검사·프로필·내부 근거](result.json)

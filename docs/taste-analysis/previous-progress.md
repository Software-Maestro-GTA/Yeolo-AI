# Progress Board: 장소 영문명(placeEngName) 한글 반환 방지 및 프롬프트/Fallback 로직 고도화 (#35)

이 문서는 하나의 백로그 기능을 완성하기 위해 구현해야 하는 세부 사양을 기획하고, 이를 해결하기 위해 각 에이전트(Planner, Tester, Coder, Reviewer)가 수행한 작업 내역을 실시간으로 기록하는 단일 기능 진척도 시트입니다.

---

## 백로그 기본 정보 (Backlog Info)

| 구분            | 내용                                                                                                                                                                 |
| :-------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **이슈 ID**     | `[TSK-35]`                                                                                                                           |
| **기능 명칭**   | 장소 영문명(`placeEngName`) 한글 반환 방지 및 프롬프트/Fallback 로직 고도화                                                                                        |
| **진행 상태**   | `Completed` (`[완료]`)                                                                                                                  |
| **연관 명세서** | [API-AI-2.md](./.agents/Yeolo-SPEC/api-specs/API-AI-2.md)                                                                                                           |

---

## 세부 기획 및 구현 체크리스트 (Implementation Checklist)

Planner 에이전트가 기획/명세를 바탕으로 이 백로그를 구현하기 위해 필요한 소스코드 상의 변경 사항을 세분화하여 정의하는 영역입니다.

- [x] **Tester: placeEngName 영문 정제 및 프롬프트 검증 테스트 작성**
  - [x] `tests/test_google_maps_tools.py`: `english_query` 미제공 시 `placeEngName`에 한글이 fallback 되지 않고 빈 문자열(`""`)로 처리되는지 검증
  - [x] `tests/test_course_generation.py`: `COURSE_GENERATION_PROMPT`에 `placeEngName` 영문/로마자 필수 작성 지침 검증 추가
- [x] **Coder: 프롬프트 지침 강화 및 google_maps 영문 정제 로직 구현**
  - [x] `app/agent/prompts.py`: `COURSE_GENERATION_PROMPT` 규칙 3번에 `placeEngName`에 한글 기입 금지 및 공식 영문/로마자 표기 의무화 지침 추가
  - [x] `app/schemas/course.py`: `PlaceSchema.placeEngName` 필드 설명에 영문/로마자 명칭 명시
  - [x] `app/agent/tools/google_maps.py`: `search_place_detail` 및 `_get_fallback_place`에서 한글 `query`가 `placeEngName`으로 무분별하게 들어가지 않도록 영문 판별 및 정제 로직 추가
- [x] **Reviewer: 무결성 검증 및 승인**
  - [x] `uv run ruff check .` 정적 분석 및 린트 검사 통과 (PASS)
  - [x] `uv run pytest` 단위/통합 테스트 100% 통과 (PASS: 26/26)

---

## 에이전트별 수행 이력 (Agent Execution Log)

각 에이전트가 파이프라인 순서에 따라 이 기능을 완성하기 위해 실제 진행한 세부 액션 및 검증 결과 기록입니다.

### 1. Planner (기획 및 세부 사양 정의)

- **진행 상태**: [완료] (Completed)
- **수행 시각**: 2026-08-18 20:06
- **수행 상세**:
  - `placeEngName`에 한글이 들어가는 원인(프롬프트 지침 누락 및 google_maps Fallback의 `query` 기본값 할당) 분석.
  - 프롬프트 지침 강화, Pydantic 설명 보강, `google_maps.py`의 `placeEngName` 정제 로직 수립.

### 2. Tester (테스트 시나리오 및 코드 작성)

- **진행 상태**: [완료] (Completed)
- **수행 시각**: 2026-08-18 20:06
- **수행 상세**:
  - `tests/test_google_maps_tools.py`: Fallback 시 `placeEngName`에 한글 검색어 대신 빈 문자열이 반환되는지 검증 테스트 추가.
  - `tests/test_course_generation.py`: 프롬프트 요구조건에 `placeEngName` 영문 규정이 포함되었는지 테스트 추가.

### 3. Coder (비즈니스 로직 및 API 구현)

- **진행 상태**: [완료] (Completed)
- **수행 시각**: 2026-08-18 20:07
- **수행 상세**:
  - `app/agent/prompts.py`: `COURSE_GENERATION_PROMPT` 규칙 3번에 `placeEngName` 한글 기입 금지 및 공식 영문/로마자 명칭 작성 규칙 추가.
  - `app/schemas/course.py`: `PlaceSchema.placeEngName` 필드 description 업데이트.
  - `app/agent/tools/google_maps.py`: `_clean_english_name` 함수 구현을 통해 한글 포함 여부를 검사하여 `placeEngName`에 한글 검색어가 기본값으로 들어가는 문제 원천 차단.

### 4. Reviewer (코드 무결성 및 빌드 검증)

- **진행 상태**: [완료] (Completed)
- **수행 시각**: 2026-08-18 20:07
- **수행 상세**:
  - `uv run ruff check .`: 정적 분석 통과 (All checks passed).
  - `uv run pytest`: 26개 테스트 100% 통과 (26 passed).
  - `placeEngName` 영문 정제 및 프롬프트 규정 무결성 검증 완료 및 승인 종료.

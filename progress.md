# Progress Board: Google Maps 장소 다단계 검색(영문 fallback) 및 LLM 생성 데이터 보존 (#32)

이 문서는 하나의 백로그 기능을 완성하기 위해 구현해야 하는 세부 사양을 기획하고, 이를 해결하기 위해 각 에이전트(Planner, Tester, Coder, Reviewer)가 수행한 작업 내역을 실시간으로 기록하는 단일 기능 진척도 시트입니다.

---

## 백로그 기본 정보 (Backlog Info)

| 구분            | 내용                                                                                                                                                                 |
| :-------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **이슈 ID**     | `[TSK-32]`                                                                                                                           |
| **기능 명칭**   | Google Maps 장소 다단계 검색(영문 fallback) 지원 및 LLM 생성 데이터(위경도/사진/카테고리) 보존 처리                                                                                        |
| **진행 상태**   | `Completed` (`[완료]`)                                                                                                                  |
| **연관 명세서** | [API-AI-2.md](./.agents/Yeolo-SPEC/api-specs/API-AI-2.md)                                                                                                           |

---

## 세부 기획 및 구현 체크리스트 (Implementation Checklist)

Planner 에이전트가 기획/명세를 바탕으로 이 백로그를 구현하기 위해 필요한 소스코드 상의 변경 사항을 세분화하여 정의하는 영역입니다.

- [x] **Tester: Google Maps 다단계 검색 및 Fallback 보존 단위/통합 테스트 작성**
  - [x] `tests/test_google_maps_tools.py`: 한글 검색 실패 시 영문명(`english_query`) 2차 검색 성공 시나리오 테스트 추가
  - [x] `tests/test_google_maps_tools.py`: Google API 검색 완전 실패/에러 시 기존 `fallback_place` 데이터(위경도, 사진 등) 보존 테스트 추가
  - [x] `tests/test_course_generation.py`: `enrich_course_with_google_maps` 후처리 중 검색 실패 시 LLM 원본 장소 데이터 소실 방지 검증 테스트 추가
- [x] **Coder: Google Maps Place 도구 및 코스 서비스 로직 구현**
  - [x] `app/agent/tools/google_maps.py`: `search_place_detail`에 `english_query` 및 `fallback_place` 매개변수 추가, 한글 실패 시 영문 2차 다단계 검색 구현
  - [x] `app/agent/tools/google_maps.py`: `_get_fallback_place`에서 기존 `fallback_place`의 유효 위경도, 사진, 카테고리 보존 병합 처리
  - [x] `app/agent/tools/google_maps.py`: LangChain `@tool` 데코레이터 `search_place_detail_tool`에 `english_query` 지원 추가
  - [x] `app/services/course_service.py`: `enrich_course_with_google_maps` 호출 시 `english_query=p_eng_name`, `fallback_place=stop.place` 전달하여 LLM 생성 장소 데이터 보존 보장
- [x] **Reviewer: 무결성 검증 및 승인**
  - [x] `uv run ruff check .` 정적 분석 및 린트 검사 통과 (PASS)
  - [x] `uv run pytest` 단위/통합 테스트 100% 통과 (PASS: 26/26)

---

## 에이전트별 수행 이력 (Agent Execution Log)

각 에이전트가 파이프라인 순서에 따라 이 기능을 완성하기 위해 실제 진행한 세부 액션 및 검증 결과 기록입니다.

### 1. Planner (기획 및 세부 사양 정의)

- **진행 상태**: [완료] (Completed)
- **수행 시각**: 2026-08-18 19:43
- **수행 상세**:
  - 해외 장소 한글명 검색 실패 시 영문명(`placeEngName`) 활용 2차 검색(다단계 검색) 방안 수립.
  - 검색 완전 실패 시 Fallback 객체로 덮어쓰여 LLM 생성 데이터(위경도, 사진, 카테고리)가 소실되는 문제를 방지하기 위해 `fallback_place` 보존 및 병합 전략 수립.
  - 테스트 및 구현 체크리스트 도출 완료.

### 2. Tester (테스트 시나리오 및 코드 작성)

- **진행 상태**: [완료] (Completed)
- **수행 시각**: 2026-08-18 19:44
- **수행 상세**:
  - `tests/test_google_maps_tools.py`: 한글 1차 실패 후 영문 2차 검색(`english_query`) 성공 테스트 및 fallback_place 기존 데이터 보존 테스트 추가.
  - `tests/test_course_generation.py`: `enrich_course_with_google_maps` API 실패 시 LLM 원본 장소 데이터(위경도, 사진 등) 유지 검증 테스트 추가.

### 3. Coder (비즈니스 로직 및 API 구현)

- **진행 상태**: [완료] (Completed)
- **수행 시각**: 2026-08-18 19:44
- **수행 상세**:
  - `app/agent/tools/google_maps.py`: `_execute_places_text_search` 헬퍼 분리, `search_place_detail` 한글 1차 실패 시 영문 2차 검색 로직 및 `fallback_place` 유효 데이터 보존 병합 구현.
  - `app/agent/tools/google_maps.py`: LangChain `@tool` `search_place_detail_tool`에 `english_query` 지원 매개변수 추가.
  - `app/services/course_service.py`: `enrich_course_with_google_maps`에서 `p_eng_name` 및 `fallback_place=stop.place`를 전달하여 API 검색 실패 시에도 LLM 원본 장소 데이터가 0.0/빈값으로 유실되지 않도록 보장.

### 4. Reviewer (코드 무결성 및 빌드 검증)

- **진행 상태**: [완료] (Completed)
- **수행 시각**: 2026-08-18 19:44
- **수행 상세**:
  - `uv run ruff check .`: 정적 분석 및 린트 검사 100% 통과 (All checks passed).
  - `uv run pytest`: 전체 26개 테스트 100% 통과 (26 passed).
  - 다단계 검색(한글 1차 실패 시 영문 2차) 및 Fallback 데이터 보존 무결성 검증 완료. 파이프라인 승인 종료.

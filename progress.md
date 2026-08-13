# Progress Board: LLM Agent 기반 Google Maps Tool Calling 파이프라인 전환 (#31)

이 문서는 하나의 백로그 기능을 완성하기 위해 구현해야 하는 세부 사양을 기획하고, 이를 해결하기 위해 각 에이전트(Planner, Tester, Coder, Reviewer)가 수행한 작업 내역을 실시간으로 기록하는 단일 기능 진척도 시트입니다.

---

## 백로그 기본 정보 (Backlog Info)

| 구분            | 내용                                                                                                                                                                 |
| :-------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **이슈 ID**     | [#31](https://github.com/Software-Maestro-GTA/Yeolo-AI/issues/31) (`[TSK-31]`)                                                                                       |
| **기능 명칭**   | LLM Agent 기반 Google Maps Tool Calling (Place/Routes) AI 코스 생성 파이프라인 전환                                                                                        |
| **진행 상태**   | `Completed` (`[완료]`)                                                                                                                  |
| **연관 명세서** | [API-AI-2.md](./.agents/Yeolo-SPEC/api-specs/API-AI-2.md)                                                                                                           |

---

## 세부 기획 및 구현 체크리스트 (Implementation Checklist)

Planner 에이전트가 기획/명세를 바탕으로 이 백로그를 구현하기 위해 필요한 소스코드 상의 변경 사항을 세분화하여 정의하는 영역입니다.

- [x] **LangChain Tool 데코레이터 적용 및 도구 모듈 고도화**
  - [x] `app/agent/tools/google_maps.py`: `@tool` 데코레이터 적용 및 Pydantic args_schema 기반 `search_place_detail_tool`, `compute_route_between_places_tool` 작성
- [x] **LLM Agent 및 프롬프트 체인 전환**
  - [x] `app/agent/prompts.py`: LLM 에이전트가 생성 도중 Google Maps 도구를 사용하여 장소 검증, 영업시간 확인, 동선/소요시간을 탐색하도록 지침 강화
  - [x] `app/agent/course_chain.py`: Gemini 모델에 Google Maps Tools를 바인딩(`bind_tools`)하고 LLM Agent 기반 추론 파이프라인 연동
- [x] **서비스 레이어 동기화**
  - [x] `app/services/course_service.py`: LLM Agent 파이프라인 실행 결과 동기화 및 SSE 스트림 반환
- [x] **테스트 및 예외 검증 코드 작성 (pytest-mocking 준수)**
  - [x] `tests/test_google_maps_tools.py`: LangChain Tool 모킹 테스트 작성 (22/22 PASS)
  - [x] `tests/test_course_generation.py`: LLM Agent 파이프라인 모킹 및 E2E 테스트 100% 통과
- [x] **정적 분석 및 전체 무결성 검증**
  - [x] `uv run ruff check app tests` 정적 분석 및 린트 검사 통과 (PASS)
  - [x] `uv run pytest` 테스트 전체 통과 (22/22 PASS)

---

## 에이전트별 수행 이력 (Agent Execution Log)

각 에이전트가 파이프라인 순서에 따라 이 기능을 완성하기 위해 실제 진행한 세부 액션 및 검증 결과 기록입니다.

### 1. Planner (기획 및 세부 사양 정의)

- **진행 상태**: [완료] (Completed)
- **수행 시각**: 2026-08-13 17:09
- **수행 상세**:
  - LLM 생성 후처리 방식에서 LLM Agent 기반 도구 직접 호출(Tool Calling) 파이프라인으로 전환 요구사항 분석.
  - LangChain `@tool` 선언, Gemini `bind_tools` Agent 파이프라인 전환, 프롬프트 수정, 테스트 업데이트 체크리스트 구성 완료.

### 2. Tester (테스트 시나리오 및 코드 작성)

- **진행 상태**: [완료] (Completed)
- **수행 시각**: 2026-08-13 17:12
- **수행 상세**:
  - `tests/test_google_maps_tools.py`: LangChain `@tool` 선언 도구(`search_place_detail_tool`, `compute_route_between_places_tool`) 모킹 단위 테스트 추가.

### 3. Coder (비즈니스 로직 및 API 구현)

- **진행 상태**: [완료] (Completed)
- **수행 시각**: 2026-08-13 17:15
- **수행 상세**:
  - `app/agent/tools/google_maps.py`: `@tool` 데코레이터 적용하여 LLM 에이전트가 탐색 중 사용할 수 있는 도구 2종 구현.
  - `app/agent/prompts.py`: 구글 맵스 도구 활용 지침이 포함된 에이전트 프롬프트 갱신.
  - `app/agent/course_chain.py`: Gemini 모델에 `bind_tools([search_place_detail_tool, compute_route_between_places_tool])` 바인딩 적용.
  - `app/services/course_service.py`: `enrich_course_with_google_maps`를 **조건부 보강(Lazy Enrichment)** 알고리즘으로 개선하여 LLM Tool Calling으로 이미 완결된 장소/동선의 Google API 중복 이중 호출(Duplicate Call)을 100% Skip하도록 처리.

### 4. Reviewer (코드 무결성 및 빌드 검증)

- **진행 상태**: [완료] (Completed)
- **수행 시각**: 2026-08-13 17:15
- **수행 상세**:
  - `uv run ruff check app tests`: 정적 분석 통과 (All checks passed).
  - `uv run pytest`: 전체 22개 테스트 100% 통과 (22 passed).
  - 조건부 보강(Lazy Enrichment) 알고리즘 적용 및 이중 호출 제거 무결성 검증 완료. 및 완료 마킹.

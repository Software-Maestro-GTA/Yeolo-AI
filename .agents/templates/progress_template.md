# Progress Board: [기능 명칭 기입] (#이슈번호)

이 문서는 하나의 백로그 기능을 완성하기 위해 구현해야 하는 세부 사양을 기획하고, 이를 해결하기 위해 각 에이전트(Planner, Tester, Coder, Reviewer)가 수행한 작업 내역을 실시간으로 기록하는 단일 기능 진척도 시트입니다.

---

## 백로그 기본 정보 (Backlog Info)

| 구분            | 내용                                                                                                                                                                 |
| :-------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **이슈 ID**     | `#이슈번호`                                                                                                                                                          |
| **기능 명칭**   | [예: AI 기반 여행지 코스 추천 API 구현]                                                                                                                              |
| **연관 명세서** | [REQ-01.md](./Yeolo-SPEC/requirement-specs/REQ-01.md) ➡️ [FUN-01.md](./Yeolo-SPEC/functional-specs/FUN-01.md) ➡️ [API-BA-01.md](./Yeolo-SPEC/api-specs/API-BA-01.md) |

---

## 세부 기획 및 구현 체크리스트 (Implementation Checklist)

Planner 에이전트가 기획/명세를 바탕으로 이 백로그를 구현하기 위해 필요한 소스코드 상의 변경 사항을 세분화하여 정의하는 영역입니다.

- [ ] **데이터 검증 및 스키마 설계**
  - [ ] [예: app/schemas/recommendation.py 내에 TravelRecommendRequest 스키마 정의]
  - [ ] [예: app/schemas/recommendation.py 내에 TravelRecommendResponse 스키마 정의]
- [ ] **비즈니스 서비스 및 AI 에이전트 연동**
  - [ ] [예: app/services/recommendation.py 내 비즈니스 핵심 추천 로직 작성]
  - [ ] [예: app/agent/recommendation_chain.py 내 LangChain 프롬프트 및 비동기 ainvoke 체인 설정]
- [ ] **FastAPI API 엔드포인트 구현**
  - [ ] [예: app/api/recommendation.py에 추천 API GET/POST 라우터 연결 및 바인딩]
- [ ] **테스트 및 예외 복구**
  - [ ] [예: pytest-mock을 활용해 Gemini LLM API 호출을 모킹한 Happy Path 테스트 및 timeout 에러 케이스 테스트 검증]

---

## 에이전트별 수행 이력 (Agent Execution Log)

각 에이전트가 파이프라인 순서에 따라 이 기능을 완성하기 위해 실제 진행한 세부 액션 및 검증 결과 기록입니다.

### 1. Planner (기획 및 세부 사양 정의)

- **진행 상태**: [대기 / 진행중 / 완료]
- **수행 시각**: YYYY-MM-DD HH:MM
- **수행 상세**:
  - [예: REQ-01 및 API-BA-01을 분석하여 FUN-01의 기술 사양 매핑을 완료함]
  - [예: 구현에 필요한 파일 변경 목록 확정 및 세부 체크리스트 작성 완료]

### 2. Tester (테스트 시나리오 및 코드 작성)

- **진행 상태**: [대기 / 진행중 / 완료]
- **수행 시각**: YYYY-MM-DD HH:MM
- **수행 상세**:
  - [예: FUN-01의 추천 로직 검증을 위한 tests/test_recommendation.py 구현]
  - [예: ChatGoogleGenerativeAI ainvoke 모킹을 위해 pytest-mock의 mocker.patch를 활용한 비동기 API 모킹 테스트 구현]

### 3. Coder (비즈니스 로직 및 API 구현)

- **진행 상태**: [대기 / 진행중 / 완료]
- **수행 시각**: YYYY-MM-DD HH:MM
- **수행 상세**:
  - [예: app/schemas/recommendation.py 및 app/services/recommendation.py 코드 구현 작성]
  - [예: app/api/recommendation.py 라우터 등록 및 모듈 상단에 module-explain-formatter 주석 완료]
  - [예: 로컬 환경에서 pytest 테스트 스크립트 실행하여 작성한 테스트 통과 확인]

### 4. Reviewer (코드 무결성 및 빌드 검증)

- **진행 상태**: [대기 / 진행중 / 완료]
- **수행 시각**: YYYY-MM-DD HH:MM
- **수행 상세**:
  - [예: Ruff 정적 분석 및 포맷 스타일 통과 확인 (LINT: PASS)]
  - [예: pytest 전체 단위/통합 테스트 스크립트 최종 빌드 성공 (TEST: PASS)]
  - [예: 검증 최종 완료 후 progress.md 전체 상태를 완료(DONE)로 마킹 및 파이프라인 종료]

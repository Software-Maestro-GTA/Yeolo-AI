---
name: yeolo-spec-matcher
description: Map user-reported issues to specifications within Yeolo-SPEC.
---

# Yeolo Spec Matcher Rule

사용자 이슈 및 작업 대상에 부합하는 `Yeolo-SPEC` 내의 공식 요구사항, 기능 명세, 도메인, API 문서를 매핑하고 검증할 때 준수해야 하는 규칙입니다.

## 1. 명세 탐색 및 분석 순서

기획 및 설계를 진행할 때는 태스크를 시작할 때 반드시 아래 순서대로 `.agents/Yeolo-SPEC/` 하위 디렉토리를 탐색하고 기획을 구성해야 합니다.

1.  **요구사항 분석 (`requirement-specs/`)**:
    - 태스크와 매핑되는 요구사항 문서 `REQ-*.md`를 찾아 읽습니다.
    - 해당 기능의 **인수 기준(Acceptance Criteria)**을 추출하여 기획 상세 내용에 기록합니다.
2.  **기능 및 비즈니스 정책 분석 (`functional-specs/`)**:
    - 매핑되는 기능 명세서 `FUN-*.md`를 분석하여 사용자 흐름(Flow)과 에러/예외 처리 상황을 식별합니다.
3.  **데이터 도메인 분석 (`domain-specs/`)**:
    - 컴포넌트나 API 호출에서 주고받는 핵심 데이터 필드 정의(`DOM-*.md`)를 확인하고 데이터 타입이 유효한지 검증합니다.
4.  **API 규격 분석 (`api-specs/`)**:
    - API 연동이 수반되는 태스크일 경우, 사용할 API 규격서 `API-*.md`를 찾아 Request/Response JSON 형태 및 Status Code를 확인합니다.

## 2. 결과 작성 표준 (progress.md 매핑)

분석 결과를 `progress.md`에 다음과 같은 형식의 마크다운 리스트로 명시하여 후속 구현 및 검증 단계에서 즉각 확인할 수 있게 해야 합니다.

```markdown
- **관련 요구사항**: [REQ-1 (인덱스 링크)](./Yeolo-SPEC/requirement-specs/REQ-01.md)
- **비즈니스 로직**: [FUN-1 (인덱스 링크)](./Yeolo-SPEC/functional-specs/FUN-01.md)
- **API 명세**: [API-BA-1 (인덱스 링크)](./Yeolo-SPEC/api-specs/API-FB-01.md)
- **인수 기준 (Acceptance Criteria)**:
  - [ ] 사용자가 스타일 버튼 클릭 시 활성화 상태(Active)로 표시되어야 함
  - [ ] API 호출 실패 시 에러 모달이 표시되어야 함
```

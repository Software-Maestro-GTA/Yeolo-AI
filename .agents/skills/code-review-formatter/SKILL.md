---
name: code-review-formatter
description: Standardize code review and test validation report generation.
---

# Code Review Formatter Rule

최종 검증 및 승인을 담당하는 Reviewer 에이전트(`reviewer.md`)가 테스트 실행 결과를 판정하고 코드 품질에 대한 종합 보고서를 `log.md`에 기입할 때 사용하는 포맷 및 검토 기준 정의입니다.

## 1. 품질 검토 기준 (Review Criteria)

Reviewer는 승인(Approve) 판정을 내리기 전 아래 항목을 반드시 자가 진증해야 합니다.

1.  **동작 무결성 (Test Pass)**:
    - 테스트 명령어가 에러 없이 정상 실행(`exit 0`)되는지 검사합니다.
2.  **문서화 준수 여부 (Docstring)**:
    - 신규/수정 소스 파일 최상단에 `module-explain-formatter`에 규정된 파일 설명 헤더가 정확히 포함되어 있는지 검사합니다.
3.  **디자인 일관성 (Style consistency)**:
    - UI 컴포넌트가 디자인 명세서 및 글로벌 스타일 CSS 변수(HSL 테마 등)를 준수하여 구현되었는지 점검합니다.
4.  **린트 및 타입 에러 여부**:
    - TypeScript 경고나 ESLint 위반이 없는지 검사합니다.

## 2. 보고서 작성 규칙 (Report Output)

- 검증 종료 후 Reviewer는 `.agents/templates/review_report_template.md` 파일을 복사하여 `log.md`에 최종 승인 또는 반려 결과를 기록합니다.
- 반려(Reject) 시에는 구체적인 실패 원인 로그(Error Stack Trace)와 함께 Coder 또는 Tester가 인지해야 할 다음 조치 사항을 마크다운 목록으로 작성합니다.

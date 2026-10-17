# Planner

공통 규칙: [system.md](../system.md). 별도 기획 역할이 위임된 경우에 적용합니다.

1. 사용자 요청과 기존 변경사항을 확인하고 목적, 수정 대상, 검증 방법을 정합니다.
2. 비즈니스 기능이면 [yeolo-spec-matcher](../skills/yeolo-spec-matcher/SKILL.md)로 관련 명세만 읽습니다. 하네스나 문서 작업에 무관한 명세 탐색을 강제하지 않습니다.
3. 작업 보드가 필요하면 `bash .agents/hooks/init.sh`를 실행하고 [progress-manager](../skills/progress-manager/SKILL.md)를 따릅니다. 기존 기록은 보존합니다.
4. 새 동작과 기존 테스트가 이미 보장하는 동작을 구분해 테스트 필요성을 판단합니다. 사용자 요청 없는 커밋을 계획에 포함하지 않습니다.
5. 실제 위임하는 경우 담당 파일, 인수 조건, 검증 범위를 전달합니다. 별도 에이전트가 없으면 동일 에이전트가 다음 단계를 수행합니다.

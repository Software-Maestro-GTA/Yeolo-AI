# Yeolo-AI 개발 지침

Python 3.14+, uv, FastAPI, LangChain/LangGraph 프로젝트입니다.
프로덕션 코드는 `app/`, 테스트는 `tests/`, 개발 하네스는 `.agents/`에 있습니다.

- 작업 전 [하네스 공통 규칙](.agents/system.md)을 읽습니다. 역할별 지침과 검증 방법은 [.agents/AGENTS.md](.agents/AGENTS.md)를 참고합니다.
- 사용자 요청과 현재 변경사항을 먼저 확인하고, 요청 범위의 수정만 수행합니다. 커밋과 푸시는 사용자가 각각 요청했을 때만 수행합니다.
- 단순 수정은 한 에이전트가 분석·구현·검증합니다. 별도 역할은 실제 위임할 때만 적용하며, 수행하지 않은 에이전트 작업을 기록하지 않습니다.
- 관련 기능 명세는 `.agents/Yeolo-SPEC/`에서 읽습니다. 이 디렉토리는 별도 Git 서브모듈이므로 명시적 요청 없이 수정하지 않습니다.
- 전체 검증: `bash .agents/hooks/test.sh`. 개발 중 선택 검증: `bash .agents/hooks/test.sh -- tests/test_gemini_parameters.py`.
- 테스트는 실제 외부 API를 호출하지 않습니다. SDK 파라미터 변경은 생성자 mock만으로 끝내지 말고 실제 SDK 직렬화 후 요청을 `httpx.MockTransport`로 검증합니다.
- 검증 훅은 설치된 환경을 사용합니다. 의존성 준비는 별도로 `uv sync --locked`로 수행합니다. Ruff 통과는 정적 타입 검사 통과를 의미하지 않습니다.
- 전체 검증에는 환경 사전 점검과 하네스 문서·설정 검사가 포함됩니다. 검증 후 관련 파일을 수정하면 영향을 받는 검증을 다시 실행합니다.

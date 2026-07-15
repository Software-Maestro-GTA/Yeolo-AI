# Harness Engineering Project Map (AGENTS.md)

이 문서는 초개인화 여행 플랫폼 **여로(Yeolo)**의 AI 서버 프로젝트 아키텍처, 의존성, 다중 에이전트 협업 파이프라인 및 개발 규율을 정의한 **공통 지침서**입니다. 프로젝트에 참여하는 모든 에이전트와 개발자는 작업을 시작하기 전 본 문서를 숙독하고 반드시 준수해야 합니다.

---

## 1. 프로젝트 개요 및 목적

- **명칭**: 여로(Yeolo) AI 서버 애플리케이션
- **목적**: 사용자의 관심사 및 여행 성향을 분석하여 맞춤형 여행 코스 추천, 일정 보관, 생성 이력 조회 등의 모바일 앱 및 웹 환경을 원스톱 제공하는 초개인화 여행 플랫폼의 **AI 추천 핵심 백엔드 및 서비스**를 구축합니다.
- **하네스 엔지니어링 지향점**: AI 서버 패키지의 안정적인 비즈니스 명세 일치와 지속가능한 코드 품질을 위해 다중 에이전트 기반 자율 TDD 루프(Planner ➔ Tester ➔ Coder ➔ Reviewer)를 활용해 개발의 신뢰성과 생산성을 극대화합니다.

---

## 2. 프로젝트 폴더 구조 (Directory Structure)

```directory
.
├── .agents/                      # 하네스 오케스트레이션 및 에이전트 규칙 저장소
│   ├── agents/                   # 다중 에이전트(Planner, Tester, Coder, Reviewer) 역할 프롬프트 (.md)
│   ├── hooks/                    # 빌드 및 테스트 자동 검증 훅 스크립트 (run_test.sh)
│   ├── skills/                   # API 모킹, 커밋 메시지 규격, 프레임워크 가이드 등 개별 실행 규칙
│   ├── templates/                # progress.md, 모듈 주석, 검증 리포트 작성용 템플릿
│   ├── Yeolo-SPEC/               # 서브모듈로 등록된 공식 기획 스펙 저장소 (요구사항/기능/도메인/API/디자인)
│   ├── config.json               # 프로젝트 패키지 환경 및 TDD 최대 루프 제한 설정 파일
│   ├── system.md                 # 전역 에이전트 절대 제약 가이드라인 (System Rules)
│   ├── progress.md               # 현재 태스크의 TDD 루프 진척 현황판 및 작업 진척도 기록 파일
│   └── log.md                    # 현재 태스크 검증 도중 발생한 에러 로그 및 리뷰 결과 파일
│
├── app/                          # FastAPI 기반 AI 서버 비즈니스 로직
│   ├── agent/                    # LangChain 기반 에이전트 및 LLM 프롬프트/체인 흐름
│   ├── api/                      # FastAPI 라우터 및 엔드포인트 구현
│   ├── core/                     # 전역 설정 및 유틸리티
│   ├── schemas/                  # Pydantic 기반 Request/Response 데이터 검증 모델
│   ├── services/                 # 비즈니스 서비스 레이어 (외부 API 및 DB 연동)
│   └── main.py                   # FastAPI 애플리케이션 진입점 및 Uvicorn 실행 스크립트
│
└── tests/                        # pytest 기반 단위 및 통합 테스트 코드 저장소
```

---

## 3. 개발 환경 및 기술 스택 (Environments & Dependencies)

- **런타임 및 패키지 매니저**: Python 3.14+ / `uv` (가상환경 기반 패키지 제어)
- **웹 프레임워크**: FastAPI
- **AI 라이브러리**: LangChain (LangChain Google GenAI 등)
- **테스트 러너**: `pytest`
- **테스트 유틸리티**: `pytest-mock` (mocker 피스처), `httpx` (FastAPI TestClient 연동용)
- **린트 및 포맷터**: `ruff`
- **개발 기동**: `uv run uvicorn app.main:app --reload`
- **테스트 명령어**: `uv run pytest`
- **린트 명령어**: `uv run ruff check`

---

## 4. 다중 에이전트 협업 워크플로우 (Multi-Agent Workflow)

본 프로젝트는 4개 에이전트가 역할을 교대하며 GitHub Issue 티켓을 할당받아 하나의 작업을 완수해 나가는 자율 TDD 이중 루프(Double-Loop)로 진행됩니다.

### 4.1 에이전트별 역할 및 구동 파일

1.  **[Planner](./agents/planner.md)**:
    - `github-mcp-server`를 이용해 사용자가 등록한 GitHub Issue를 분석하고, `Yeolo-SPEC` 하위 명세를 매핑합니다.
    - `progress_template.md`를 바탕으로 `progress.md` 파일 초기화 및 인수 기준(Acceptance Criteria) 작성을 수행합니다.
2.  **[Tester](./agents/tester.md)**:
    - 기능 구현이 진행되기 전, `progress.md`에 설정된 인수 기준을 검증하는 실패하는 단위 테스트 코드(Red Phase) 및 외부 API/LLM 연동 모킹 핸들러를 먼저 작성합니다.
3.  **[Coder](./agents/coder.md)**:
    - 실패하는 테스트 코드를 통과시키는 비즈니스 로직과 API 스키마/엔드포인트를 구현합니다.
    - **테스트 코드를 임의 수정/완화하는 것은 엄격히 금지**됩니다. 파일 최상단에는 `module-explain-formatter` 주석을 추가합니다.
4.  **[Reviewer](./agents/reviewer.md)**:
    - 검증 훅인 `bash .agents/hooks/run_test.sh ai-server`를 구동하여 테스트 및 린트 결과(exit code)를 검증합니다.
    - 실패 시 에러 로그를 `log.md`에 기록하고 Coder/Tester로 롤백합니다.
    - 성공 시 `git-commit-formatter` 형식에 맞는 표준 커밋을 적용하고 `github-mcp-server`를 호출하여 관련 GitHub 이슈를 종결(Close) 처리합니다.

---

## 5. 필수로 준수해야 하는 공통 규칙 (Global Rules)

1.  **System.md의 절대 준수**:
    - [system.md](./system.md)에 기술된 제약 조건을 위반하지 않습니다. (테스트 없는 코딩 금지, uv 패키지 환경 보존 등)
2.  **API 및 LLM 모킹 의무화 (pytest-mocking)**:
    - 외부 LLM API 호출(Google Gemini 등)이나 외부 연동 서비스와의 차단을 보장하기 위해, 모든 비즈니스 로직 테스트 작성 시 [pytest-mocking/SKILL.md](./skills/pytest-mocking/SKILL.md)에 맞추어 `pytest-mock` 또는 `unittest.mock`을 사용해 API 호출을 모킹해야 합니다.
3.  **프레임워크 개발 지침 가동**:
    - AI 서버 구현 시 [python-guideline](./skills/python-guideline/SKILL.md)을 우선 참고하여 폴더 구조와 라우터 설계, Pydantic 스키마 및 LangChain 체인 호출 구조를 일치시킵니다.
4.  **문서 주석 규격화**:
    - 새로 만들거나 수정한 모든 소스 코드 최상단에는 [module-explain-formatter](./skills/module-explain-formatter/SKILL.md) 지침에 명시된 Python Docstring 헤더 주석을 필수로 포함합니다.
5.  **커밋 메시지 표준**:
    - Git 커밋 시 반드시 [git-commit-formatter](./skills/git-commit-formatter/SKILL.md)에 명시된 커밋 메시지 규격을 준수합니다.

---

## 6. 에러 상황 및 예외 처리 (Error Handling & Escalation)

- **TDD 최대 루프 초과 시 대처**:
  - `max_tdd_loops`(기본 5회)를 초과하여 최종 검증이 반복 실패하는 경우, 에이전트는 무의미한 루프를 멈추고 `log.md`에 최후 에러 내역을 남긴 채 **사람 개발자에게 보고(Escalation)**해야 합니다.
- **컴파일 및 정적 분석 중단**:
  - Python 정적 분석 경고나 ruff 린트 에러가 포착되는 즉시 실패(`exit != 0`)로 간주하고 `reviewer.md`가 반려 처리를 실행합니다.

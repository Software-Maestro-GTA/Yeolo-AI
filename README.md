# Yeolo-AI

제로터치 초개인화 여행 플랫폼 여로 (AI Server)

본 프로젝트는 FastAPI와 LangChain을 활용하여 구축된 여로(Yeolo)의 AI 서버입니다.

---

## 개발 환경 설정 및 빌드/실행 방법

### 1. 요구사항

- Python 3.14 이상
- [uv](https://astral.sh/uv) (Astral의 초고속 Python 패키지 인스톨러)

### 2. 의존성 패키지 설치

`uv`를 사용하여 필요한 의존성을 신속하게 설치하고 가상환경을 구성합니다.

```bash
# 의존성 설치 및 동기화
uv sync --locked
```

### 3. 로컬 서버 실행

FastAPI 서버를 로컬에서 실행하고 핫 리로드(Hot-reload)를 지원하도록 구동합니다.

#### 방법 A: Python 모듈로 직접 실행 (추천)

`app/main.py` 내부의 `__main__` 엔드포인트를 실행하여 서버를 가동합니다.

```bash
uv run python -m app.main
```

#### 방법 B: Uvicorn CLI 명령어로 직접 실행

```bash
uv run uvicorn app.main:app --reload
```

서버 실행이 완료되면 [http://127.0.0.1:8000](http://127.0.0.1:8000)에서 API 엔드포인트를 확인할 수 있습니다.

### 4. 개발 검증

```bash
# 전체 검증: 셸 문법, 환경, 하네스, Ruff, pytest
bash .agents/hooks/test.sh

# 개발 중 선택 검증 (전체 검증과 별도로 표시)
bash .agents/hooks/test.sh -- tests/test_gemini_parameters.py

# 필요할 때만 작업 보드 생성 (기존 progress.md와 log.md 보존)
bash .agents/hooks/init.sh
```

훅은 스크립트 위치를 기준으로 저장소 루트에서 실행합니다. 의존성을 자동으로 설치하거나
변경하지 않으므로 먼저 `uv sync --locked`로 환경을 준비하세요. 선택 옵션은 `--` 뒤에
전달하며, 검증 범위가 환경변수로 바뀌지 않도록 `PYTEST_ADDOPTS`는 사용하지 않습니다.
실행 명령과 종료 코드는 루트 `log.md`에 누적하고 상세 진단은 터미널에 출력합니다.
현재 정적 타입 검사기는 구성되어 있지 않습니다.

환경만 확인하려면 `bash .agents/hooks/preflight.sh`를 사용합니다. uv와 가상환경의
Python·Ruff·pytest를 확인한 뒤 Python 요구 버전과 잠금 파일·설치 패키지 일치 여부를
읽기 전용으로 검사합니다. 불일치 시 자동 수정하지 않으며 진단에 따라
`uv sync --locked --group dev`로 환경을 준비해야 합니다.

하네스 자체 검사는 스킬 필수 메타데이터, 문서의 인라인 파일 링크, 설정의 훅 경로를
검사합니다. 설정의 테스트·린트 명령은 전체 검증 훅과 동일한 명령만 허용하므로
존재하지 않는 실행 대상, 선택 실행, 수집 전용 실행, 자동 수정 옵션을 거부합니다.
코드 예제·과거 기록·명세 서브모듈·외부 URL·앵커는 링크 검사에서 제외합니다.
문서만 고친 경우 `uv run --no-sync --offline python scripts/check_harness.py`로 다시 확인할 수 있습니다.

모든 PR에서 actionlint로 워크플로와 표현식을, ShellCheck로 워크플로 내부 셸과
`.agents/hooks/*.sh`, `scripts/*.sh`를 먼저 검사합니다. 두 도구를 포함하는
`rhysd/actionlint:1.7.11` 컨테이너에서 `scripts/check_static.sh`를 실행하며 실패하면 중단합니다.
로컬에서는 actionlint와 ShellCheck를 설치한 뒤 `sh scripts/check_static.sh`로 같은 검사를
실행할 수 있습니다. 워크플로나 셸 파일을 수정했다면 이 검사도 수행하세요.

이후 전체 Python 검증을 실행하며, 배포 워크플로도 같은 검증을 재사용해 성공한 뒤에만
이미지를 빌드·푸시합니다. 병합 자체를 차단하려면 GitHub 브랜치 보호 설정에서
`Harness, lint and tests` 검사를 필수로 등록해야 합니다.
검증 후 관련 코드나 설정을 수정했다면 영향을 받는 검증을 다시 실행하세요.

에이전트 작업 규칙은 [AGENTS.md](AGENTS.md), 하네스 운영 방법은
[.agents/AGENTS.md](.agents/AGENTS.md)를 참고하세요.

---

## 프로젝트 폴더 구조

```text
yeolo-ai/
├── .venv/                  # uv 가상환경 디렉토리
├── app/                    # 전체 소스 코드 루트
│   ├── main.py             # FastAPI 애플리케이션 진입점
│   ├── api/                # API 라우팅 레이어
│   │   ├── agent.py        # AI 에이전트 호출 API
│   │   └── chat.py         # 일반 챗 API
│   ├── core/               # 공통 설정 및 유틸리티 (config, DB 등)
│   ├── agent/              # LangChain & LangGraph 에이전트 코어 레이어
│   │   ├── graph.py        # LangGraph 워크플로우 정의
│   │   ├── state.py        # 에이전트의 State(상태) 스키마 정의
│   │   ├── prompts.py      # 시스템 프롬프트 및 템플릿
│   │   └── tools/          # 에이전트가 사용하는 Custom Tools 모음
│   ├── schemas/            # Request / Response 데이터 모델 (Pydantic)
│   └── services/           # 외부 API 및 백엔드 비즈니스 로직
├── tests/                  # 테스트 코드 디렉토리
├── $.env                   # 환경변수 설정 샘플
├── pyproject.toml          # uv 프로젝트 의존성 설정 파일
└── uv.lock                 # 의존성 잠금 파일
```

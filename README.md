# Yeolo-AI

제로터치 초개인화 여행 플랫폼 여로 (AI Server)

본 프로젝트는 FastAPI와 LangChain을 활용하여 구축된 여로(Yeolo)의 AI 서버입니다.

---

## 🛠️ 개발 환경 설정 및 빌드/실행 방법

### 1. 요구사항

- Python 3.14 이상
- [uv](https://astral.sh/uv) (Astral의 초고속 Python 패키지 인스톨러)

### 2. 의존성 패키지 설치

`uv`를 사용하여 필요한 의존성을 신속하게 설치하고 가상환경을 구성합니다.

```bash
# 의존성 설치 및 동기화
uv sync
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

---

## 📂 프로젝트 폴더 구조

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

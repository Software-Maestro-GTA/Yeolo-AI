# Yeolo-AI

제로터치 초개인화 여행 플랫폼 **여로**의 AI 서버입니다. FastAPI와 LangChain/LangGraph로 취향 분석과 여행 코스 생성을 제공합니다.

## 개발 환경

- Python 3.14 이상
- [uv](https://astral.sh/uv)

## 설치 및 실행

```bash
uv sync --locked
```

[$.env]($.env)을 참고해 `.env`를 설정한 뒤 서버를 실행합니다.

```bash
uv run python -m app.main
```

- 서버: [http://127.0.0.1:8000](http://127.0.0.1:8000)
- API 문서: [Swagger UI](http://127.0.0.1:8000/docs)

코스의 추천 이유(`reason`)와 방문 팁(`memo`)은 각각 사실 근거를 검토합니다. 일반적인 방문 행동이나 조건부 확인 안내는 허용하고, 한쪽이 실패해도 승인된 다른 문구는 유지합니다. 작성·검토 실패 시 해당 필드는 빈 문자열일 수 있습니다.

## 테스트

```bash
# 전체 검증
bash .agents/hooks/test.sh

# 선택 검증
bash .agents/hooks/test.sh -- tests/test_gemini_parameters.py
```

## 프로젝트 구조

```text
app/
├── main.py      # FastAPI 진입점
├── api/         # API 라우터
├── agent/       # LLM 체인·프롬프트·LangGraph
├── core/        # 설정·로깅·모델 연동
├── schemas/     # 요청·응답 모델
└── services/    # 비즈니스 로직
tests/           # 테스트
.agents/         # 개발 지침·검증 훅
```

개발 규칙은 [AGENTS.md](AGENTS.md), 검증 도구 상세는 [하네스 운영 문서](.agents/AGENTS.md)를 참고하세요.

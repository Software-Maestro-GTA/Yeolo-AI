---
name: python-guideline
description: FastAPI, LangChain 및 비동기 Python 환경에서 준수해야 할 백엔드 구현 규칙
---

# Python & FastAPI Development Guideline

FastAPI와 LangChain 프레임워크를 기반으로 Yeolo AI 서버를 개발할 때, 일관된 스타일과 성능을 유지하기 위해 아래의 설계 규격을 엄격히 준수합니다.

## 1. 아키텍처 및 폴더 구조 규칙

- **`app/api/` (Presentation Layer)**:
  - FastAPI 라우터와 엔드포인트를 정의합니다.
  - 비즈니스 로직을 직접 작성하지 않고, 서비스 레이어(Services)의 함수를 호출하여 처리 결과를 반환합니다.
- **`app/schemas/` (Data Validation Layer)**:
  - Pydantic v2 기반의 데이터 검증 스키마를 정의합니다.
  - 요청 바디(`*Request`)와 응답 바디(`*Response`)를 각각 분리하여 작성합니다.
- **`app/services/` (Business Layer)**:
  - 실제 핵심 비즈니스 로직과 알고리즘이 작성되는 곳입니다.
  - AI 모델 인터페이스, 데이터베이스(DB) 처리 등 외부 연결 흐름을 제어합니다.
- **`app/agent/` (AI Agent Layer)**:
  - LangChain 프롬프트 템플릿, 에이전트 도구(Tools), 체인 구조를 정의합니다.

## 2. 비동기 프로그래밍 (`async` / `await`) 규칙

- FastAPI의 I/O 블로킹을 방지하기 위해 네트워크 통신(API 호출, DB 쿼리, LLM 응답 대기 등)은 반드시 **비동기 함수(`async def`)**로 정의하며, 호출 시 `await`를 사용합니다.
- 외부 API 호출에는 `httpx.AsyncClient`를 활용합니다.
- LangChain 라이브러리를 사용할 때, LLM 모델이나 체인 호출은 비동기 메서드인 `ainvoke`, `astream`, `abatch` 등을 우선적으로 호출합니다.
  - 예시:
    ```python
    # 올바른 예시
    response = await chat_model.ainvoke(messages)
    ```

## 3. Pydantic 및 타입 힌트 (Type Hinting)

- 모든 함수의 입력 파라미터와 출력 리턴 타입에는 정적 타입 힌트를 명시합니다.
- Pydantic 모델을 설계할 때 `Field`를 사용해 설명(`description`) 및 제약 사항(예: `ge`, `le`, `min_length` 등)을 추가하여 API 스펙과의 일관성을 유지합니다.
  - 예시:
    ```python
    from pydantic import BaseModel, Field

    class TravelRecommendRequest(BaseModel):
        destination: str = Field(..., min_length=2, description="여행 목적지 도시 이름")
        duration_days: int = Field(..., ge=1, le=30, description="여행 일수")
    ```

## 4. 에러 핸들링 (Error Handling)

- 예외 상황 발생 시 FastAPI의 `HTTPException`을 발생시켜 클라이언트에게 유의미한 HTTP 상태 코드와 에러 메시지를 반환합니다.
- 서비스 레이어에서 발생할 수 있는 내부 예외는 도메인 예외(Custom Exception)를 선언하여 발생시키며, 글로벌 익셉션 핸들러(`exception_handler`)에서 이를 포착하여 처리하도록 설정합니다.

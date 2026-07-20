---
name: pytest-mocking
description: pytest 및 pytest-mock을 활용한 외부 API 및 AI 모델 모킹 규칙
---

# pytest Mocking Rule

테스트 코드를 작성하거나 프로덕션 개발을 진행할 때 외부 백엔드 API, 외부 Open API(예: Google Gemini, 지도 API 등), 데이터베이스 연동 등을 테스트하기 위해 준수해야 하는 규칙입니다.

## 1. Mocking 원칙

- **외부 네트워크 호출 차단**: 테스트 실행 도중 실제 외부 서버로의 HTTP 요청이 발생하지 않도록 차단하는 것을 원칙으로 합니다.
- **테스트 격리**: 각 테스트 케이스는 독립적이어야 하며, 목 데이터 상태가 다른 테스트 케이스에 영향을 미치지 않도록 `pytest-mock`의 `mocker` 피스처를 사용합니다.

## 2. pytest-mock (`mocker`) 활용 규격

- `mocker`는 `unittest.mock.patch`를 pytest 피스처 스타일로 제공하여 테스트 종료 시 자동으로 모킹을 리셋해 줍니다.

### 2.1 LangChain & ChatGoogleGenerativeAI 모킹 예제

AI 에이전트나 추천 엔진 등에서 Google Gemini 모델 API를 연동하여 사용할 때, 실제 토큰 비용 및 네트워크 의존성을 제거하기 위해 모델의 `ainvoke` 또는 `invoke` 호출을 모킹합니다.

```python
import pytest
from unittest.mock import AsyncMock, MagicMock

@pytest.mark.asyncio
async def test_travel_agent_recommendation(mocker):
    # 1. ChatGoogleGenerativeAI.ainvoke 모킹 (비동기 함수이므로 AsyncMock 사용)
    mock_response = MagicMock()
    mock_response.content = "추천 여행지: 제주도 2박 3일 코스"
    
    mock_ainvoke = mocker.patch(
        "langchain_google_genai.ChatGoogleGenerativeAI.ainvoke",
        new_callable=AsyncMock
    )
    mock_ainvoke.return_value = mock_response

    # 2. 비즈니스 로직 실행
    from app.services.recommendation import get_travel_recommendation
    result = await get_travel_recommendation(destination="제주도", duration_days=3)

    # 3. 검증
    assert "제주도" in result
    mock_ainvoke.assert_called_once()
```

### 2.2 외부 HTTP API 연동 모킹 예제

`httpx.AsyncClient`를 사용하여 외부 REST API를 호출하는 경우, 해당 클라이언트의 `get` 또는 `post` 메서드를 모킹합니다.

```python
@pytest.mark.asyncio
async def test_external_weather_api(mocker):
    # AsyncClient.get 호출에 대한 모킹
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"weather": "Sunny", "temp": 22}
    
    mock_get = mocker.patch(
        "httpx.AsyncClient.get",
        new_callable=AsyncMock
    )
    mock_get.return_value = mock_response

    # 서비스 로직 호출 및 검증
    from app.services.weather import get_weather
    data = await get_weather("Seoul")
    
    assert data["temp"] == 22
    mock_get.assert_called_once_with("https://api.weather.com/v1/Seoul")
```

## 3. 예외 상황 및 에러 케이스 검증

- 에러 케이스를 검증하기 위해 모킹 함수가 예외를 발생시키거나 HTTP 에러 응답을 반환하도록 설정합니다.
- pytest의 `pytest.raises`를 활용하여 예외가 정상적으로 전파되거나 처리되는지 테스트합니다.

```python
import httpx

@pytest.mark.asyncio
async def test_weather_api_failure(mocker):
    # API 호출 시 Timeout 예외가 발생하는 시나리오 모킹
    mocker.patch(
        "httpx.AsyncClient.get",
        side_effect=httpx.TimeoutException("Connection timed out")
    )

    from app.services.weather import get_weather
    
    # 예외 처리 검증
    with pytest.raises(httpx.HTTPError):
        await get_weather("Seoul")
```

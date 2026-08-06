# Module Explain Template

## 클래스 및 함수 문서화 (Class & Function Docstring)

모듈 내 주요 비즈니스 클래스 및 함수에는 입력 파라미터, 반환 값 및 예외 처리 정보를 명시하는 Google Style Docstring 작성을 권장합니다.

```python
def recommend_courses(destination: str, duration: int) -> list[dict]:
    """여행 목적지와 일정을 기반으로 맞춤형 추천 코스 목록을 조회합니다.

    Args:
        destination (str): 여행 목적지 도시명.
        duration (int): 여행 일수 (1일 이상 30일 이하).

    Returns:
        list[dict]: 추천 코스 리스트.
    """
    ...
```

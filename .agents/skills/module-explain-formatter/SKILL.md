---
name: module-explain-formatter
description: Standardize docstrings and header comments for newly created or modified files.
---

# Module Explain Formatter Rule

코더 에이전트(`coder.md`)가 새로운 파이썬 파일(코드 모듈)을 생성하거나 기존 소스 코드를 수정할 때, 파일 최상단에 모듈의 설명과 기획 명세 매핑 정보를 기록하는 문서화 주석 표준입니다.

## 1. 파일 헤더 주석 템플릿 (File Header Docstring)

모든 파이썬 소스 코드 파일의 최상단(임포트 구문 이전)에는 아래 형식의 트리플 쿼트(`"""`) Docstring이 반드시 포함되어야 합니다.

### 파이썬 모듈 예시:

```python
"""
@file [파일명 (예: travel_service.py)]
@description [모듈의 핵심 목적 및 담당 기능 간략 설명]
@requirements [REQ-XX (연관된 요구사항 번호)]
@functional [FUN-XX (연관된 기능명세 번호, 없을 시 N/A)]
@api [API-BA-XX (연관된 API 명세서 번호, 없을 시 N/A)]
@author Antigravity Agent
"""

from fastapi import FastAPI
...
```

## 2. 클래스 및 함수 문서화 (Class & Function Docstring)

모듈 외부로 노출되는 주요 클래스 및 비즈니스 함수에는 입력 파라미터, 반환 값 및 예외 처리 정보를 명시하여 다른 에이전트나 개발자가 코드 구조를 즉시 파악하고 테스트를 설계할 수 있도록 돕습니다. (Google Style Docstring 권장)

### 예시:

```python
def recommend_courses(destination: str, duration: int) -> list[dict]:
    """여행 목적지와 일정을 기반으로 맞춤형 추천 코스 목록을 조회합니다.

    Args:
        destination (str): 여행 목적지 도시명.
        duration (int): 여행 일수 (1일 이상 30일 이하).

    Returns:
        list[dict]: 추천 코스 리스트. 각 코스는 도시명, 일차, 명소 정보를 포함함.

    Raises:
        ValueError: duration 값이 범위를 벗어날 경우 발생.
    """
    ...
```

# Module Explain Template

새로운 파일 작성 또는 기존 파일 수정 시, 소스 파일 최상단에 반드시 추가해야 하는 표준 문서화 주석입니다.

## Python 파일 (모듈) 헤더 주석 템플릿

모든 파이썬 소스 코드 파일(예: `main.py`, 서비스, 라우터, 스키마 등)의 최상단에는 아래 형식의 Docstring이 반드시 포함되어야 합니다.

```python
"""
@file [파일명 (예: main.py)]
@description [모듈의 핵심 목적 및 담당 기능 간략 설명]
@requirements [REQ-XX (연관된 요구사항 번호, 없을 시 N/A)]
@functional [FUN-XX (연관된 기능명세 번호, 없을 시 N/A)]
@api [API-BA-XX (연관된 API 명세서 번호, 없을 시 N/A)]
@author Antigravity Agent
"""
```

이 헤더를 제외한 파일 내부의 핵심 클래스 및 외부 노출 함수에도 비즈니스 요구사항 및 입출력 타입을 나타내는 적절한 Docstring을 추가해야 합니다.

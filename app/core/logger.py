"""
@file logger.py
@description AWS CloudWatch 및 컨테이너 배포 환경 통합 로깅 구성 모듈
@requirements REQ-11, REQ-7
@author Antigravity Agent
"""

import logging
import sys
from app.core.config import settings

def setup_logging():
    """
    애플리케이션 전역 로깅 바인딩 및 핸들러/포맷터 설정 함수.
    표준 출력(sys.stdout)으로 로그를 내보내 AWS CloudWatch Logs 및 컨테이너 로그 수집기와 연동합니다.
    """
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    log_format = "[%(asctime)s] [%(levelname)s] [%(name)s:%(lineno)d] - %(message)s"

    # 루트 로거 설정
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # 기존 핸들러 중복 방지를 위한 정리
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # StreamHandler (sys.stdout) 생성
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    formatter = logging.Formatter(log_format)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    # Uvicorn 로거 레벨 동기화
    for uvicorn_logger_name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uv_logger = logging.getLogger(uvicorn_logger_name)
        uv_logger.setLevel(log_level)

    init_logger = logging.getLogger(__name__)
    init_logger.info(f"Logging initialized with level: {settings.LOG_LEVEL.upper()}")

logger = logging.getLogger(__name__)


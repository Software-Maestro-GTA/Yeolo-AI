"""
@file config.py
@description 환경 변수 설정 및 프로젝트 글로벌 환경 변수 정의 모듈
@requirements REQ-11
@functional FUN-1
@api API-BA-6
@author Antigravity Agent
"""

import os
from dotenv import load_dotenv

load_dotenv()

class Settings:
    # 내부 통신 인증용 API Key
    INTERNAL_API_KEY: str = os.getenv("INTERNAL_API_KEY", "default_internal_secret_key")
    
    # Gemini API Key
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
    
    # 사용할 Gemini 모델명
    GEMINI_MODEL_NAME: str = os.getenv("GEMINI_MODEL_NAME", "gemini-3.5-flash")

settings = Settings()

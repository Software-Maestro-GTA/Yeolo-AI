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

    # Google Maps API Key (Place New & Routes API)
    GOOGLE_MAPS_API_KEY: str = os.getenv("GOOGLE_MAPS_API_KEY", "")

    # 로깅 레벨 (DEV: DEBUG, PROD: INFO)
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

settings = Settings()

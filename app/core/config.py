import os

from dotenv import load_dotenv

load_dotenv()


class Settings:
    # 내부 통신 인증용 API Key
    INTERNAL_API_KEY: str = os.getenv("INTERNAL_API_KEY", "default_internal_secret_key")

    # Gemini API Key
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

    # 사용할 Gemini 모델명
    GEMINI_MODEL_NAME: str = os.getenv("GEMINI_MODEL_NAME", "gemini-3.8-flash")

    # Server-only Calling credentials; Gemini routing is configured in ABTO.
    ABTO_CALLING_KEY: str = os.getenv("ABTO_CALLING_KEY", "")
    ABTO_GATEWAY_BASE_URL: str = os.getenv(
        "ABTO_GATEWAY_BASE_URL", "https://gateway.abto.app/v1"
    )

    # Google Maps API Key (Place New & Routes API)
    GOOGLE_MAPS_API_KEY: str = os.getenv("GOOGLE_MAPS_API_KEY", "")

    # Server-side quote conditions: one adult, one room, prices in KRW.
    MYREALTRIP_PARTNER_API_KEY: str = os.getenv("MYREALTRIP_PARTNER_API_KEY", "")
    MYREALTRIP_ORIGIN_AIRPORT: str = os.getenv("MYREALTRIP_ORIGIN_AIRPORT", "ICN")
    MYREALTRIP_ADULTS: int = int(os.getenv("MYREALTRIP_ADULTS", "1"))
    MYREALTRIP_QUOTE_TIMEOUT_SECONDS: float = float(
        os.getenv("MYREALTRIP_QUOTE_TIMEOUT_SECONDS", "8")
    )
    MYREALTRIP_MCP_URL: str = os.getenv(
        "MYREALTRIP_MCP_URL", "https://mcp-servers.myrealtrip.com/mcp"
    )
    MYREALTRIP_PARTNER_BASE_URL: str = os.getenv(
        "MYREALTRIP_PARTNER_BASE_URL", "https://partner-ext-api.myrealtrip.com"
    )

    # 로깅 레벨 (DEV: DEBUG, PROD: INFO)
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # Internal pipeline controls; public API schemas remain unchanged.
    COURSE_HISTORY_PATH: str = os.getenv(
        "COURSE_HISTORY_PATH", ".data/course_history.sqlite3"
    )
    COURSE_TIMEOUT_SECONDS: float = float(os.getenv("COURSE_TIMEOUT_SECONDS", "300"))
    COURSE_MAPS_CONCURRENCY: int = int(os.getenv("COURSE_MAPS_CONCURRENCY", "6"))
    TASTE_ANALYSIS_TIMEOUT_SECONDS: float = float(
        os.getenv("TASTE_ANALYSIS_TIMEOUT_SECONDS", "60")
    )


settings = Settings()

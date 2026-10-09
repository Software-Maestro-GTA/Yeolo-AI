"""Single structured-output chain for the complete taste profile."""

from langchain_google_genai import ChatGoogleGenerativeAI

from app.agent.prompts import TASTE_PROFILE_PROMPT
from app.core.config import settings
from app.schemas.taste_profile import TasteProfileAnalysisOutput

# Import-time test collection remains possible without provider credentials.
gemini_api_key = settings.GEMINI_API_KEY or "fake_gemini_api_key_for_testing"
llm = ChatGoogleGenerativeAI(
    model=settings.GEMINI_MODEL_NAME,
    google_api_key=gemini_api_key,
)
# Suppress LangChain's candidate_count=1 default; GenAI omits None on the wire.
taste_profile_chain = TASTE_PROFILE_PROMPT | llm.with_structured_output(
    TasteProfileAnalysisOutput
).bind(generation_config={'candidate_count': None})

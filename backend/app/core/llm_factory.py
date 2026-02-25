from typing import Optional
from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_ollama import ChatOllama
from app.core.config import settings


class LLMFactory:
    @staticmethod
    def get_model(provider: str, model_name: str, api_key: Optional[str] = None):
        """
        Return a LangChain chat model instance.

        api_key: resolved key from user_keys_store or frontend per-session override.
                 Falls back to the server .env value when None.
        """
        if provider == "ollama":
            # Ollama is local — no API key needed.
            return ChatOllama(
                model=model_name,
                base_url=settings.OLLAMA_BASE_URL.rstrip("/"),
            )

        if provider == "openai":
            return ChatOpenAI(
                model=model_name,
                api_key=api_key or settings.OPENAI_API_KEY,
            )

        if provider == "gemini":
            return ChatGoogleGenerativeAI(
                model=model_name,
                google_api_key=api_key or settings.GOOGLE_API_KEY,
            )

        if provider == "grok":
            return ChatOpenAI(
                model=model_name,
                api_key=api_key or settings.GROK_API_KEY,
                base_url=settings.GROK_BASE_URL,
            )

        raise ValueError(
            f"Unsupported provider: '{provider}'. "
            f"Valid options: ollama, openai, gemini, grok."
        )
from langchain_openai import ChatOpenAI
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_ollama import ChatOllama
from app.core.config import settings


class LLMFactory:
    @staticmethod
    def get_model(provider: str, model_name: str, api_key: str = None):
        """
        Return a LangChain chat model instance for the given provider.

        api_key: if supplied by the frontend settings panel, it overrides the
                 server-side .env value.  Ignored for Ollama (no key needed).
        """
        if provider == "ollama":
            return ChatOllama(
                model=model_name,
                base_url=settings.OLLAMA_BASE_URL.rstrip("/"),
            )

        if provider == "openai":
            return ChatOpenAI(model=model_name, api_key=api_key or settings.OPENAI_API_KEY)

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

        raise ValueError(f"Unsupported provider: {provider!r}")
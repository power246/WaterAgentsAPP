import logging
from typing import Optional
from app.core.config import settings
from app.core.llm_factory import LLMFactory

logger = logging.getLogger(__name__)


class LLMService:
    @staticmethod
    async def call_llm(
        prompt:     str,
        model_name: str           = None,
        provider:   str           = None,
        api_key:    Optional[str] = None,
    ) -> str:
        """
        Unified LLM invocation entry point.

        Parameters
        ----------
        prompt     : Full prompt string (system + user merged).
        model_name : Model ID, e.g. "llama3:8b" or "gpt-4o".
                     Defaults to settings.DEFAULT_LLM_MODEL.
        provider   : Provider ID ("ollama" | "openai" | "gemini" | "grok").
                     If None, auto-detected from model_name.
        api_key    : User-supplied API key from the frontend settings panel.
                     Falls back to the server-side .env value when None.

        Returns
        -------
        Raw response string. JSON parsing is the caller's responsibility.
        """
        target_model = model_name or settings.DEFAULT_LLM_MODEL

        # Auto-detect provider from model name when not explicitly supplied
        if not provider:
            name_lower = target_model.lower()
            if "gpt" in name_lower:
                provider = "openai"
            elif "gemini" in name_lower:
                provider = "gemini"
            elif "grok" in name_lower:
                provider = "grok"
            else:
                provider = "ollama"

        logger.info(f"[LLM] Calling {provider}/{target_model}")
        print(f"\n{'='*30} LLM INPUT ({provider}/{target_model}) {'='*30}")
        print(f"\033[34m{prompt}\033[0m")
        print("=" * 80 + "\n")

        try:
            llm = LLMFactory.get_model(provider, target_model, api_key=api_key)

            # Apply provider-specific JSON-mode binding
            if provider == "ollama":
                llm = llm.bind(format="json")
            elif provider == "openai":
                llm = llm.bind(response_format={"type": "json_object"})
            # Gemini and Grok handle JSON via prompt instructions only

            response = await llm.ainvoke(prompt)
            raw_text = response.content

            print(f"\n{'*'*30} LLM RAW OUTPUT {'*'*30}")
            print(f"\033[32m{raw_text}\033[0m")
            print("*" * 80 + "\n")

            return raw_text

        except Exception as e:
            logger.error(f"[LLM] {provider}/{target_model} call failed: {e}")
            return "{}"
import logging
from typing import Optional
from app.core.config import settings
from app.core.llm_factory import LLMFactory
from app.core.user_keys_store import resolve_llm_key

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

        Key resolution priority (for cloud providers):
          1. api_key arg      — explicit per-request key from frontend
          2. user_keys_store  — key saved by user in the Settings panel
          3. .env             — server-side environment variable (LLMFactory fallback)
        """
        target_model = model_name or settings.DEFAULT_LLM_MODEL

        # Auto-detect provider when not explicitly supplied
        if not provider:
            name_lower = target_model.lower()
            if "gpt"    in name_lower: provider = "openai"
            elif "gemini" in name_lower: provider = "gemini"
            elif "grok"   in name_lower: provider = "grok"
            else:                        provider = "ollama"

        # Resolve the best available API key using the priority chain
        resolved_key = await resolve_llm_key(provider, api_key)

        logger.info(f"[LLM] Calling {provider}/{target_model} "
                    f"(key source: {'frontend' if api_key else 'store/env' if resolved_key else 'env'})")

        print(f"\n{'='*30} LLM INPUT ({provider}/{target_model}) {'='*30}")
        print(f"\033[34m{prompt}\033[0m")
        print("=" * 80 + "\n")

        try:
            llm = LLMFactory.get_model(provider, target_model, api_key=resolved_key)

            # Apply provider-specific JSON-mode binding
            if provider == "ollama":
                llm = llm.bind(format="json")
            elif provider == "openai":
                llm = llm.bind(response_format={"type": "json_object"})

            response = await llm.ainvoke(prompt)
            raw_text = response.content

            print(f"\n{'*'*30} LLM RAW OUTPUT {'*'*30}")
            print(f"\033[32m{raw_text}\033[0m")
            print("*" * 80 + "\n")

            return raw_text

        except Exception as e:
            logger.error(f"[LLM] {provider}/{target_model} call failed: {e}")
            return "{}"
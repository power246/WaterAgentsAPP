from __future__ import annotations

from app.application.query_understanding import (
    ParserFinishReason,
    QueryParserInputV1,
    QueryParserResponse,
)
from app.application.query_understanding.parser_port import ParserDeltaHandler
from app.infrastructure.adapters._parsing import (
    invalid_response,
    require_list,
    require_mapping,
    require_string,
)
from app.infrastructure.http import ProviderHttpClient
from app.ports import PortConfigurationError, PortUnavailableError

PROVIDER = "openai"


class OpenAIResponsesParserAdapter:
    def __init__(
        self,
        http: ProviderHttpClient,
        *,
        api_key: str,
        base_url: str,
    ) -> None:
        if not api_key:
            raise PortConfigurationError(
                "OpenAI API key is not configured.",
                provider=PROVIDER,
                operation="configure",
            )
        self._http = http
        self._api_key = api_key
        self._endpoint = f"{base_url.rstrip('/')}/responses"

    async def generate(
        self,
        request: QueryParserInputV1,
        on_delta: ParserDeltaHandler,
    ) -> QueryParserResponse:
        if request.model.provider.casefold() != PROVIDER:
            raise PortConfigurationError(
                "The selected query parser provider is not configured.",
                provider=request.model.provider,
                operation="generate",
            )
        system, user = request.messages
        payload = await self._http.post_json(
            self._endpoint,
            {
                "model": request.model.model,
                "instructions": system.text,
                "input": user.text,
                "text": {"format": {"type": "json_object"}},
                "tools": [],
                "store": False,
            },
            provider=PROVIDER,
            operation="generate",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        root = require_mapping(payload, provider=PROVIDER, operation="generate")
        status = require_string(
            root.get("status"),
            provider=PROVIDER,
            operation="generate",
        )
        text = self._output_text(root)
        if text:
            await on_delta(text)
        if status == "completed":
            if not text:
                raise invalid_response(PROVIDER, "generate")
            finish_reason = ParserFinishReason.STOP
        elif status == "incomplete":
            details = require_mapping(
                root.get("incomplete_details"),
                provider=PROVIDER,
                operation="generate",
            )
            finish_reason = (
                ParserFinishReason.LENGTH
                if details.get("reason") == "max_output_tokens"
                else ParserFinishReason.CONTENT_FILTER
            )
        elif status in {"failed", "cancelled"}:
            raise PortUnavailableError(
                "OpenAI did not complete the parser response.",
                provider=PROVIDER,
                operation="generate",
            )
        else:
            raise invalid_response(PROVIDER, "generate")
        return QueryParserResponse(text=text, finish_reason=finish_reason)

    @staticmethod
    def _output_text(root: dict[str, object] | object) -> str:
        response = require_mapping(root, provider=PROVIDER, operation="generate")
        output = require_list(
            response.get("output", []),
            provider=PROVIDER,
            operation="generate",
        )
        chunks: list[str] = []
        for raw_item in output:
            item = require_mapping(raw_item, provider=PROVIDER, operation="generate")
            if item.get("type") != "message":
                continue
            content = require_list(
                item.get("content"),
                provider=PROVIDER,
                operation="generate",
            )
            for raw_part in content:
                part = require_mapping(
                    raw_part,
                    provider=PROVIDER,
                    operation="generate",
                )
                if part.get("type") == "output_text":
                    text = part.get("text")
                    if not isinstance(text, str) or not text:
                        raise invalid_response(PROVIDER, "generate")
                    chunks.append(text)
        return "".join(chunks)

from __future__ import annotations

import json

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
from app.infrastructure.http import ProviderHttpClient, ServerSentEvent
from app.ports import (
    InvalidPortResponseError,
    PortConfigurationError,
    PortUnavailableError,
)

PROVIDER = "openai"
_IGNORED_EVENT_TYPES = frozenset(
    {
        "response.queued",
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.output_item.done",
        "response.content_part.added",
        "response.content_part.done",
        "response.output_text.done",
        "response.output_text.annotation.added",
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_part.done",
        "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.done",
        "response.reasoning_text.delta",
        "response.reasoning_text.done",
    }
)


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
        chunks: list[str] = []
        saw_refusal = False
        async with self._http.post_sse(
            self._endpoint,
            {
                "model": request.model.model,
                "instructions": system.text,
                "input": user.text,
                "text": {"format": {"type": "json_object"}},
                "tools": [],
                "store": False,
                "stream": True,
            },
            provider=PROVIDER,
            operation="generate",
            headers={
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        ) as events:
            async for event in events:
                root, event_type = self._event_payload(event)
                if event_type == "response.output_text.delta":
                    delta = root.get("delta")
                    if not isinstance(delta, str) or not delta:
                        raise invalid_response(PROVIDER, "generate")
                    await on_delta(delta)
                    chunks.append(delta)
                    continue
                if event_type in {"response.refusal.delta", "response.refusal.done"}:
                    saw_refusal = True
                    continue
                if event_type == "error":
                    raise self._unavailable()
                if event_type in {
                    "response.completed",
                    "response.incomplete",
                    "response.failed",
                    "response.cancelled",
                }:
                    return self._terminal_response(
                        root,
                        event_type=event_type,
                        text="".join(chunks),
                        saw_refusal=saw_refusal,
                    )
                if event_type not in _IGNORED_EVENT_TYPES:
                    raise invalid_response(PROVIDER, "generate")
        raise invalid_response(PROVIDER, "generate")

    @staticmethod
    def _event_payload(
        event: ServerSentEvent,
    ) -> tuple[dict[str, object], str]:
        try:
            payload = json.loads(event.data)
        except json.JSONDecodeError as error:
            raise InvalidPortResponseError(
                "Provider returned an invalid event stream.",
                provider=PROVIDER,
                operation="generate",
            ) from error
        root = require_mapping(payload, provider=PROVIDER, operation="generate")
        event_type = require_string(
            root.get("type"),
            provider=PROVIDER,
            operation="generate",
        )
        if event.event not in {None, event_type}:
            raise invalid_response(PROVIDER, "generate")
        return dict(root), event_type

    @staticmethod
    def _terminal_response(
        root: dict[str, object],
        *,
        event_type: str,
        text: str,
        saw_refusal: bool,
    ) -> QueryParserResponse:
        response = require_mapping(
            root.get("response"),
            provider=PROVIDER,
            operation="generate",
        )
        status = require_string(
            response.get("status"),
            provider=PROVIDER,
            operation="generate",
        )
        expected_status = event_type.removeprefix("response.")
        if status != expected_status:
            raise invalid_response(PROVIDER, "generate")
        if status == "completed":
            if saw_refusal or OpenAIResponsesParserAdapter._has_refusal(response):
                finish_reason = ParserFinishReason.CONTENT_FILTER
            elif text:
                finish_reason = ParserFinishReason.STOP
            else:
                raise invalid_response(PROVIDER, "generate")
        elif status == "incomplete":
            raw_details = response.get("incomplete_details")
            if raw_details is not None and not isinstance(raw_details, dict):
                raise invalid_response(PROVIDER, "generate")
            reason = raw_details.get("reason") if raw_details else None
            finish_reason = (
                ParserFinishReason.LENGTH
                if reason in {"max_output_tokens", "max_tokens"}
                else ParserFinishReason.CONTENT_FILTER
            )
        elif status in {"failed", "cancelled"}:
            raise OpenAIResponsesParserAdapter._unavailable()
        else:
            raise invalid_response(PROVIDER, "generate")
        return QueryParserResponse(text=text, finish_reason=finish_reason)

    @staticmethod
    def _has_refusal(response: object) -> bool:
        root = require_mapping(
            response,
            provider=PROVIDER,
            operation="generate",
        )
        output = require_list(
            root.get("output", []),
            provider=PROVIDER,
            operation="generate",
        )
        for raw_item in output:
            item = require_mapping(
                raw_item,
                provider=PROVIDER,
                operation="generate",
            )
            if item.get("type") != "message":
                continue
            content = require_list(
                item.get("content", []),
                provider=PROVIDER,
                operation="generate",
            )
            for raw_part in content:
                part = require_mapping(
                    raw_part,
                    provider=PROVIDER,
                    operation="generate",
                )
                if part.get("type") == "refusal":
                    return True
        return False

    @staticmethod
    def _unavailable() -> PortUnavailableError:
        return PortUnavailableError(
            "OpenAI did not complete the parser response.",
            provider=PROVIDER,
            operation="generate",
        )

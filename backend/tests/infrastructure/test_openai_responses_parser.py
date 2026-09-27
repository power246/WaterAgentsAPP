import asyncio
import json

import httpx
import pytest

from app.application.query_understanding import (
    ParserFinishReason,
    ParserMessageRole,
    ParserPromptMessage,
    QueryParserInputV1,
)
from app.domain import ModelSelection
from app.infrastructure.adapters import OpenAIResponsesParserAdapter
from app.infrastructure.http import ProviderHttpClient
from app.ports import PortConfigurationError, PortUnavailableError


def run(coroutine):
    return asyncio.run(coroutine)


def parser_request(*, provider: str = "openai") -> QueryParserInputV1:
    return QueryParserInputV1(
        model=ModelSelection(provider=provider, model="gpt-parser"),
        messages=(
            ParserPromptMessage(
                role=ParserMessageRole.SYSTEM,
                text="Return one JSON object.",
            ),
            ParserPromptMessage(
                role=ParserMessageRole.USER,
                text='{"query":"river"}',
            ),
        ),
    )


def client(handler) -> ProviderHttpClient:
    return ProviderHttpClient(
        timeout_seconds=1,
        max_response_bytes=100_000,
        retry_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    )


def test_completed_response_uses_responses_json_mode_and_all_message_text() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {"type": "reasoning", "summary": []},
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": '{"location_mention"'},
                            {"type": "output_text", "text": ":null}"},
                        ],
                    },
                ],
            },
            request=request,
        )

    async def exercise() -> None:
        deltas: list[str] = []

        async def collect(text: str) -> None:
            deltas.append(text)

        async with client(handler) as http:
            response = await OpenAIResponsesParserAdapter(
                http,
                api_key="openai-secret",
                base_url="https://api.openai.test/v1",
            ).generate(parser_request(), collect)
        assert response.finish_reason is ParserFinishReason.STOP
        assert response.text == '{"location_mention":null}'
        assert deltas == [response.text]

    run(exercise())
    assert len(requests) == 1
    request = requests[0]
    payload = json.loads(request.content)
    assert str(request.url) == "https://api.openai.test/v1/responses"
    assert request.headers["authorization"] == "Bearer openai-secret"
    assert payload == {
        "model": "gpt-parser",
        "instructions": "Return one JSON object.",
        "input": '{"query":"river"}',
        "text": {"format": {"type": "json_object"}},
        "tools": [],
        "store": False,
    }


def test_incomplete_and_failed_responses_have_typed_finish_semantics() -> None:
    payloads = [
        {
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "{"}],
                }
            ],
        },
        {"status": "failed", "output": []},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payloads.pop(0), request=request)

    async def exercise() -> None:
        async with client(handler) as http:
            adapter = OpenAIResponsesParserAdapter(
                http,
                api_key="secret",
                base_url="https://api.openai.test/v1",
            )
            incomplete = await adapter.generate(parser_request(), lambda _: _noop())
            assert incomplete.finish_reason is ParserFinishReason.LENGTH
            with pytest.raises(PortUnavailableError):
                await adapter.generate(parser_request(), lambda _: _noop())

    run(exercise())


def test_provider_mismatch_starts_no_http_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={}, request=request)

    async def exercise() -> None:
        async with client(handler) as http:
            adapter = OpenAIResponsesParserAdapter(
                http,
                api_key="secret",
                base_url="https://api.openai.test/v1",
            )
            with pytest.raises(PortConfigurationError):
                await adapter.generate(
                    parser_request(provider="another-provider"),
                    lambda _: _noop(),
                )

    run(exercise())
    assert calls == 0


async def _noop() -> None:
    return None

import asyncio
import json
import logging

import httpx
import pytest

from app.infrastructure.adapters import OpenAIEmbeddingAdapter
from app.infrastructure.http import ProviderHttpClient
from app.ports import (
    EmbeddingCancelledError,
    InvalidPortResponseError,
    PortConfigurationError,
    PortHttpError,
    PortRateLimitError,
    PortTimeoutError,
)
from tests.infrastructure.embeddings.support import canonical_text, embedding_profile


def run(coroutine):
    return asyncio.run(coroutine)


def http_client(handler) -> ProviderHttpClient:
    return ProviderHttpClient(
        timeout_seconds=5,
        max_response_bytes=32_768,
        retry_delay_seconds=0,
        attempts=2,
        transport=httpx.MockTransport(handler),
    )


def response_body(model: str, vectors: list[list[object]]) -> dict[str, object]:
    return {
        "object": "list",
        "model": model,
        "data": [
            {"object": "embedding", "index": index, "embedding": vector}
            for index, vector in enumerate(vectors)
        ],
        "usage": {"prompt_tokens": 1, "total_tokens": 1},
    }


def test_batch_request_and_ordered_response_follow_the_port_contract() -> None:
    profile = embedding_profile(dimensions=3)
    texts = (canonical_text(), canonical_text(parameter_code="00010"))
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.method == "POST"
        assert request.url.path == "/v1/embeddings"
        assert request.headers["authorization"] == "Bearer unit-test-key"
        assert json.loads(request.content) == {
            "model": profile.model,
            "input": list(texts),
            "dimensions": 3,
            "encoding_format": "float",
        }
        return httpx.Response(
            200,
            json=response_body(profile.model, [[1, 0, 0], [0, 1, 0]]),
            request=request,
        )

    async def exercise() -> None:
        async with http_client(handler) as http:
            batch = await OpenAIEmbeddingAdapter(
                http,
                api_key="unit-test-key",
                base_url="https://provider.test/v1",
            ).embed(profile, texts, None, None)
        assert batch.profile == profile
        assert tuple(vector.values for vector in batch.vectors) == (
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
        )

    run(exercise())
    assert calls == 1


@pytest.mark.parametrize(
    "body",
    (
        {},
        response_body("wrong-model", [[1, 0, 0]]),
        response_body("text-embedding-fixture", []),
        {
            **response_body("text-embedding-fixture", [[1, 0, 0]]),
            "data": [
                {"object": "embedding", "index": 1, "embedding": [1, 0, 0]}
            ],
        },
        response_body("text-embedding-fixture", [[1, 0]]),
        response_body("text-embedding-fixture", [[0, 0, 0]]),
        response_body("text-embedding-fixture", [[float("nan"), 0, 0]]),
        response_body("text-embedding-fixture", [[float("inf"), 0, 0]]),
    ),
)
def test_missing_order_dimension_finite_and_norm_violations_fail_closed(
    body: dict[str, object],
) -> None:
    profile = embedding_profile(dimensions=3)

    async def exercise() -> None:
        async with http_client(
            lambda request: httpx.Response(
                200,
                content=json.dumps(body, allow_nan=True).encode("utf-8"),
                request=request,
            )
        ) as http:
            with pytest.raises(InvalidPortResponseError):
                await OpenAIEmbeddingAdapter(
                    http,
                    api_key="unit-test-key",
                    base_url="https://provider.test/v1",
                ).embed(profile, (canonical_text(),), None, None)

    run(exercise())


def test_zero_input_returns_without_network_and_noncanonical_input_is_rejected() -> None:
    profile = embedding_profile(dimensions=3)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("network must not be called")

    async def exercise() -> None:
        async with http_client(handler) as http:
            adapter = OpenAIEmbeddingAdapter(
                http,
                api_key="unit-test-key",
                base_url="https://provider.test/v1",
            )
            assert (await adapter.embed(profile, (), None, None)).vectors == ()
            with pytest.raises(PortConfigurationError):
                await adapter.embed(profile, ("raw transcript",), None, None)

    run(exercise())
    assert calls == 0


def test_duplicate_or_reordered_provider_indices_fail_closed() -> None:
    profile = embedding_profile(dimensions=3)
    texts = (canonical_text(), canonical_text(parameter_code="00010"))
    body = response_body(profile.model, [[1, 0, 0], [0, 1, 0]])
    body["data"][1]["index"] = 0

    async def exercise() -> None:
        async with http_client(
            lambda request: httpx.Response(200, json=body, request=request)
        ) as http:
            with pytest.raises(InvalidPortResponseError):
                await OpenAIEmbeddingAdapter(
                    http,
                    api_key="unit-test-key",
                    base_url="https://provider.test/v1",
                ).embed(profile, texts, None, None)

    run(exercise())


@pytest.mark.parametrize(
    ("status", "error_type"),
    ((429, PortRateLimitError), (500, PortHttpError)),
)
def test_http_failures_are_not_retried_by_the_adapter(
    status: int,
    error_type: type[Exception],
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, content=b'raw-provider-secret', request=request)

    async def exercise() -> None:
        async with http_client(handler) as http:
            with pytest.raises(error_type):
                await OpenAIEmbeddingAdapter(
                    http,
                    api_key="unit-test-key",
                    base_url="https://provider.test/v1",
                ).embed(
                    embedding_profile(dimensions=3),
                    (canonical_text(),),
                    None,
                    None,
                )

    run(exercise())
    assert calls == 1


def test_timeout_and_explicit_cancellation_are_bounded() -> None:
    entered = asyncio.Event()

    class Token:
        requested = False

        def is_requested(self) -> bool:
            return self.requested

    async def handler(request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def exercise_timeout() -> None:
        async with http_client(handler) as http:
            with pytest.raises(PortTimeoutError):
                await OpenAIEmbeddingAdapter(
                    http,
                    api_key="unit-test-key",
                    base_url="https://provider.test/v1",
                ).embed(
                    embedding_profile(dimensions=3),
                    (canonical_text(),),
                    asyncio.get_running_loop().time() + 0.01,
                    None,
                )

    async def exercise_cancel() -> None:
        token = Token()
        async with http_client(handler) as http:
            task = asyncio.create_task(
                OpenAIEmbeddingAdapter(
                    http,
                    api_key="unit-test-key",
                    base_url="https://provider.test/v1",
                ).embed(
                    embedding_profile(dimensions=3),
                    (canonical_text(),),
                    None,
                    token,
                )
            )
            await entered.wait()
            token.requested = True
            with pytest.raises(EmbeddingCancelledError):
                await task

    run(exercise_timeout())
    entered = asyncio.Event()
    run(exercise_cancel())


def test_error_and_log_text_exclude_secret_and_provider_body(caplog) -> None:
    api_key = "embedding-api-key-secret"
    provider_body = "raw-provider-body-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=provider_body, request=request)

    async def exercise() -> None:
        async with http_client(handler) as http:
            with pytest.raises(PortHttpError) as captured:
                await OpenAIEmbeddingAdapter(
                    http,
                    api_key=api_key,
                    base_url="https://provider.test/private",
                ).embed(
                    embedding_profile(dimensions=3),
                    (canonical_text(),),
                    None,
                    None,
                )
            rendered = f"{captured.value!s} {captured.value!r}"
            assert api_key not in rendered
            assert provider_body not in rendered
            assert canonical_text() not in rendered

    with caplog.at_level(logging.WARNING, logger="wateragents.providers"):
        run(exercise())
    assert api_key not in caplog.text
    assert provider_body not in caplog.text
    assert canonical_text() not in caplog.text

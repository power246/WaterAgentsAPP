import asyncio
import logging

import httpx
import pytest

from app.infrastructure.http import ProviderHttpClient
from app.ports import (
    InvalidPortResponseError,
    PortAuthenticationError,
    PortHttpError,
    PortRateLimitError,
    PortResponseLimitError,
    PortTimeoutError,
)


def run(coroutine):
    return asyncio.run(coroutine)


def client(handler, *, max_response_bytes: int = 1024) -> ProviderHttpClient:
    return ProviderHttpClient(
        timeout_seconds=1,
        max_response_bytes=max_response_bytes,
        retry_delay_seconds=0,
        transport=httpx.MockTransport(handler),
    )


def test_transient_get_is_retried_once_and_success_is_returned() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        assert request.method == "GET"
        calls += 1
        if calls == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    async def exercise() -> None:
        async with client(handler) as http:
            assert await http.get_json(
                "https://provider.test/data",
                provider="fixture",
                operation="metadata",
            ) == {"ok": True}

    run(exercise())
    assert calls == 2


@pytest.mark.parametrize(
    ("status", "error_type", "expected_calls"),
    [
        (401, PortAuthenticationError, 1),
        (429, PortRateLimitError, 2),
        (500, PortHttpError, 2),
    ],
)
def test_http_status_taxonomy_and_retry_count(
    status: int,
    error_type: type[Exception],
    expected_calls: int,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(status, request=request)

    async def exercise() -> None:
        async with client(handler) as http:
            with pytest.raises(error_type):
                await http.get_bytes(
                    "https://provider.test/data",
                    provider="fixture",
                    operation="metadata",
                )

    run(exercise())
    assert calls == expected_calls


def test_invalid_json_and_response_size_are_distinct() -> None:
    async def exercise_invalid() -> None:
        async with client(
            lambda request: httpx.Response(200, content=b"{", request=request)
        ) as http:
            with pytest.raises(InvalidPortResponseError):
                await http.get_json(
                    "https://provider.test/data",
                    provider="fixture",
                    operation="metadata",
                )

    async def exercise_large() -> None:
        async with client(
            lambda request: httpx.Response(200, content=b"12345", request=request),
            max_response_bytes=4,
        ) as http:
            with pytest.raises(PortResponseLimitError):
                await http.get_bytes(
                    "https://provider.test/data",
                    provider="fixture",
                    operation="metadata",
                )

    run(exercise_invalid())
    run(exercise_large())


def test_exhausted_deadline_starts_no_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, request=request)

    async def exercise() -> None:
        async with client(handler) as http:
            with pytest.raises(PortTimeoutError):
                await http.get_bytes(
                    "https://provider.test/data",
                    provider="fixture",
                    operation="metadata",
                    deadline=asyncio.get_running_loop().time() - 1,
                )

    run(exercise())
    assert calls == 0


def test_cancellation_propagates_without_a_retry() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return httpx.Response(200, request=request)

    async def exercise() -> None:
        async with client(handler) as http:
            task = asyncio.create_task(
                http.get_bytes(
                    "https://provider.test/data",
                    provider="fixture",
                    operation="metadata",
                )
            )
            await entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    run(exercise())
    assert calls == 1


def test_provider_errors_and_logs_do_not_expose_url_or_secret(caplog) -> None:
    secret = "AIza-secret-value"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request)

    async def exercise() -> None:
        async with client(handler) as http:
            with pytest.raises(PortHttpError) as captured:
                await http.get_bytes(
                    "https://provider.test/private/path",
                    provider="fixture",
                    operation="metadata",
                    params={"api_key": secret},
                )
            rendered = f"{captured.value!s} {captured.value!r}"
            assert secret not in rendered
            assert "private/path" not in rendered

    with caplog.at_level(logging.WARNING, logger="wateragents.providers"):
        run(exercise())
    assert secret not in caplog.text
    assert "private/path" not in caplog.text

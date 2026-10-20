import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from app.infrastructure.http import ProviderHttpClient
from app.infrastructure.observability.json_logging import JsonFormatter
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


def client(
    handler,
    *,
    max_response_bytes: int = 1024,
    retry_delay_seconds: float = 0,
) -> ProviderHttpClient:
    return ProviderHttpClient(
        timeout_seconds=1,
        max_response_bytes=max_response_bytes,
        retry_delay_seconds=retry_delay_seconds,
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


def test_get_429_honors_retry_after_seconds() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "1"}, request=request)
        return httpx.Response(200, content=b"ok", request=request)

    async def exercise() -> float:
        async with client(handler) as http:
            started = asyncio.get_running_loop().time()
            assert await http.get_bytes(
                "https://provider.test/data",
                provider="fixture",
                operation="metadata",
            ) == b"ok"
            return asyncio.get_running_loop().time() - started

    assert run(exercise()) >= 1.0
    assert calls == 2


def test_get_json_reports_rate_limit_even_when_internal_retry_succeeds() -> None:
    calls = 0
    reported: list[float | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    async def exercise() -> None:
        async with client(handler) as http:
            assert await http.get_json(
                "https://provider.test/data",
                provider="usgs_nldi",
                operation="lookup_position",
                on_rate_limit=reported.append,
            ) == {"ok": True}

    run(exercise())
    assert calls == 2
    assert reported == [0.0]


def test_rate_limit_callback_cooldown_controls_internal_retry() -> None:
    calls = 0
    started: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        started.append(asyncio.get_running_loop().time())
        if calls == 1:
            return httpx.Response(429, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    async def exercise() -> None:
        async with client(handler) as http:
            assert await http.get_json(
                "https://provider.test/data",
                provider="usgs_nldi",
                operation="lookup_position",
                on_rate_limit=lambda retry_after: 0.05,
            ) == {"ok": True}

    run(exercise())
    assert calls == 2
    assert started[1] - started[0] >= 0.04


def test_rate_limit_callback_precedes_deadline_conversion_and_logs_safe_headers(
    caplog,
) -> None:
    reported: list[float | None] = []
    calls = 0
    secret = "private-rate-header"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            429,
            headers={
                "Retry-After": "120",
                "X-RateLimit-Limit": "3600",
                "X-RateLimit-Remaining": "2888",
                "X-RateLimit-Reset": "1720000000",
                "X-Other-Header": secret,
            },
            request=request,
        )

    async def exercise() -> None:
        async with client(handler) as http:
            with pytest.raises(PortTimeoutError):
                await http.get_bytes(
                    "https://provider.test/data",
                    provider="usgs_nldi",
                    operation="lookup_position",
                    deadline=asyncio.get_running_loop().time() + 0.1,
                    on_rate_limit=reported.append,
                )

    with caplog.at_level(logging.INFO, logger="wateragents.providers"):
        run(exercise())
    assert calls == 1
    assert reported == [120.0]
    record = next(record for record in caplog.records
                  if getattr(record, "event_name", None) == "provider_response_headers_received")
    payload = json.loads(JsonFormatter().format(record))
    assert payload["provider_rate_limit_limit"] == 3600
    assert payload["provider_rate_limit_remaining"] == 2888
    assert payload["provider_rate_limit_reset"] == 1720000000
    assert payload["provider_retry_after_seconds"] == 120.0
    assert secret not in json.dumps(payload)


def test_nldi_rate_header_logging_ignores_invalid_or_unbounded_values(caplog) -> None:
    secret = "private-invalid-header"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "X-RateLimit-Limit": "9" * 5000,
                "X-RateLimit-Remaining": secret,
                "X-RateLimit-Reset": "-1",
            },
            content=b"ok",
            request=request,
        )

    async def exercise() -> None:
        async with client(handler) as http:
            assert await http.get_bytes(
                "https://provider.test/data",
                provider="usgs_nldi",
                operation="lookup_position",
            ) == b"ok"

    with caplog.at_level(logging.INFO, logger="wateragents.providers"):
        run(exercise())
    record = next(record for record in caplog.records
                  if getattr(record, "event_name", None) == "provider_response_headers_received")
    payload = json.loads(JsonFormatter().format(record))
    assert "provider_rate_limit_limit" not in payload
    assert "provider_rate_limit_remaining" not in payload
    assert "provider_rate_limit_reset" not in payload
    assert secret not in json.dumps(payload)


@pytest.mark.parametrize(
    "retry_after_kind",
    ["seconds", "http-date", "asctime"],
)
def test_get_429_skips_retry_when_retry_after_exceeds_deadline(
    retry_after_kind: str,
    caplog,
) -> None:
    calls = 0
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=2)
    retry_after = {
        "seconds": "120",
        "http-date": format_datetime(retry_at, usegmt=True),
        "asctime": retry_at.ctime(),
    }[retry_after_kind]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": retry_after}, request=request)

    async def exercise() -> None:
        async with client(handler) as http:
            with pytest.raises(PortTimeoutError):
                await http.get_bytes(
                    "https://provider.test/data",
                    provider="fixture",
                    operation="metadata",
                    deadline=asyncio.get_running_loop().time() + 0.5,
                )

    with caplog.at_level(logging.WARNING, logger="wateragents.providers"):
        run(exercise())
    assert calls == 1
    failure = next(record for record in caplog.records
                   if record.message == "provider_request_failed")
    assert failure.provider_status == 429
    assert failure.provider_retry_after_seconds >= 90


@pytest.mark.parametrize("retry_after", ["153", "9" * 5000])
def test_get_429_without_deadline_rejects_wait_beyond_client_cap(
    retry_after: str,
    caplog,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": retry_after}, request=request)

    async def exercise() -> None:
        async with client(handler) as http:
            with pytest.raises(PortRateLimitError):
                await asyncio.wait_for(
                    http.get_bytes(
                        "https://provider.test/data",
                        provider="fixture",
                        operation="metadata",
                    ),
                    timeout=0.5,
                )

    with caplog.at_level(logging.WARNING, logger="wateragents.providers"):
        run(exercise())
    assert calls == 1
    failure = next(record for record in caplog.records
                   if record.message == "provider_request_failed")
    assert failure.provider_status == 429
    if retry_after == "153":
        assert failure.provider_retry_after_seconds == 153
    else:
        assert not hasattr(failure, "provider_retry_after_seconds")


def test_get_429_invalid_retry_after_uses_configured_delay(caplog) -> None:
    calls = 0
    invalid_header = "private-invalid-retry-after"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": invalid_header}, request=request)
        return httpx.Response(200, content=b"ok", request=request)

    async def exercise() -> float:
        async with client(handler, retry_delay_seconds=0.1) as http:
            started = asyncio.get_running_loop().time()
            assert await http.get_bytes(
                "https://provider.test/data",
                provider="fixture",
                operation="metadata",
            ) == b"ok"
            return asyncio.get_running_loop().time() - started

    with caplog.at_level(logging.INFO, logger="wateragents.providers"):
        elapsed = run(exercise())
    assert calls == 2
    assert elapsed >= 0.05
    assert invalid_header not in caplog.text


def test_get_429_retry_after_wait_is_cancellable() -> None:
    entered = asyncio.Event()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        entered.set()
        return httpx.Response(429, headers={"Retry-After": "10"}, request=request)

    async def exercise() -> None:
        async with client(handler) as http:
            task = asyncio.create_task(
                http.get_bytes(
                    "https://provider.test/data",
                    provider="fixture",
                    operation="metadata",
                    deadline=asyncio.get_running_loop().time() + 15,
                )
            )
            await entered.wait()
            await asyncio.sleep(0.01)
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    run(exercise())
    assert calls == 1


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
    observed = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, request=request)

    async def exercise() -> None:
        def observe() -> None:
            nonlocal observed
            observed += 1

        async with client(handler) as http:
            with pytest.raises(PortTimeoutError):
                await http.get_bytes(
                    "https://provider.test/data",
                    provider="fixture",
                    operation="metadata",
                    deadline=asyncio.get_running_loop().time() - 1,
                    on_attempt=observe,
                )

    run(exercise())
    assert calls == 0
    assert observed == 0


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


def test_cancellation_checkpoint_prevents_retry_from_starting() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        asyncio.current_task().cancel()
        return httpx.Response(503, request=request)

    async def exercise() -> None:
        async with client(handler) as http:
            with pytest.raises(asyncio.CancelledError):
                await http.get_bytes(
                    "https://provider.test/data",
                    provider="fixture",
                    operation="metadata",
                )

    run(exercise())
    assert calls == 1


def test_attempt_observer_counts_every_real_retry() -> None:
    observed = 0

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    def observe() -> None:
        nonlocal observed
        observed += 1

    async def exercise() -> None:
        async with client(handler) as http:
            with pytest.raises(PortHttpError):
                await http.get_bytes(
                    "https://provider.test/data",
                    provider="fixture",
                    operation="metadata",
                    on_attempt=observe,
                )

    run(exercise())
    assert observed == 2


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


def test_sse_bad_request_logs_provider_error_fields_without_response_message(caplog) -> None:
    secret = "private-request-content"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {
                "type": "invalid_request_error",
                "code": "invalid_json_schema",
                "param": "text.format.schema",
                "message": secret,
            }},
            request=request,
        )

    async def exercise() -> None:
        async with client(handler) as http:
            with pytest.raises(PortHttpError):
                async with http.post_sse(
                    "https://provider.test/responses",
                    {"model": "fixture"},
                    provider="fixture",
                    operation="summary",
                ):
                    pass

    with caplog.at_level(logging.WARNING, logger="wateragents.providers"):
        run(exercise())
    record = next(record for record in caplog.records
                  if record.message == "provider_bad_request")
    assert record.provider_error_type == "invalid_request_error"
    assert record.provider_error_code == "invalid_json_schema"
    assert record.provider_error_param == "text.format.schema"
    assert secret not in caplog.text


def test_sse_stream_override_accepts_multiple_bounded_reasoning_frames() -> None:
    frames = [
        b"event: response.reasoning_text.delta\ndata: {\"type\":\"response.reasoning_text.delta\",\"delta\":\"private\"}\n\n",
        b"event: response.reasoning_text.done\ndata: {\"type\":\"response.reasoning_text.done\",\"text\":\"private\"}\n\n",
    ]
    raw = b"".join(frames)
    assert all(len(frame) < 128 for frame in frames)
    assert len(raw) > 128

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=raw,
            request=request,
        )

    async def exercise(max_stream_bytes: int | None) -> tuple[str | None, ...]:
        async with client(handler, max_response_bytes=128) as http:
            async with http.post_sse(
                "https://provider.test/responses",
                {"model": "fixture"},
                provider="fixture",
                operation="reasoning",
                max_stream_bytes=max_stream_bytes,
            ) as stream:
                return tuple([event.event async for event in stream])

    with pytest.raises(PortResponseLimitError):
        run(exercise(None))
    assert run(exercise(512)) == (
        "response.reasoning_text.delta",
        "response.reasoning_text.done",
    )


def test_sse_stream_override_still_rejects_oversized_single_frame() -> None:
    raw = (
        b"event: response.reasoning_text.done\n"
        + b"data: " + b"x" * 129 + b"\n\n"
    )
    assert len(raw) < 512

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=raw,
            request=request,
        )

    async def exercise() -> None:
        async with client(handler, max_response_bytes=128) as http:
            async with http.post_sse(
                "https://provider.test/responses",
                {"model": "fixture"},
                provider="fixture",
                operation="reasoning",
                max_stream_bytes=512,
            ) as stream:
                async for _ in stream:
                    pass

    with pytest.raises(PortResponseLimitError):
        run(exercise())


def test_sse_stream_override_is_hard_bounded_before_request() -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, request=request)

    async def exercise() -> None:
        async with client(handler) as http:
            with pytest.raises(ValueError):
                async with http.post_sse(
                    "https://provider.test/responses",
                    {"model": "fixture"},
                    provider="fixture",
                    operation="reasoning",
                    max_stream_bytes=67_108_865,
                ):
                    pass

    run(exercise())
    assert requests == 0

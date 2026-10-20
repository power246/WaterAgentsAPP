from __future__ import annotations

import asyncio
import codecs
import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from math import isfinite

import httpx

from app.ports import (
    InvalidPortResponseError,
    PortAuthenticationError,
    PortConfigurationError,
    PortError,
    PortHttpError,
    PortRateLimitError,
    PortResponseLimitError,
    PortTimeoutError,
    PortUnavailableError,
)

logger = logging.getLogger("wateragents.providers")

QueryValue = str | int | float | bool | tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ServerSentEvent:
    event: str | None
    data: str


class _SseFrameLimitError(Exception):
    pass


class _SseDecoder:
    def __init__(self, *, max_frame_bytes: int) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self._buffer = ""
        self._event: str | None = None
        self._data: list[str] = []
        self._max_frame_bytes = max_frame_bytes
        self._frame_bytes = 0

    def feed(self, chunk: bytes) -> tuple[ServerSentEvent, ...]:
        self._buffer += self._decoder.decode(chunk)
        events: list[ServerSentEvent] = []
        while True:
            line_end = self._line_end()
            if line_end is None:
                break
            index, separator_length = line_end
            line = self._buffer[:index]
            self._buffer = self._buffer[index + separator_length :]
            self._frame_bytes += len(line.encode("utf-8")) + separator_length
            if self._frame_bytes > self._max_frame_bytes:
                raise _SseFrameLimitError
            event = self._process_line(line)
            if event is not None:
                events.append(event)
            if not line:
                self._frame_bytes = 0
        if self._frame_bytes + len(self._buffer.encode("utf-8")) > self._max_frame_bytes:
            raise _SseFrameLimitError
        return tuple(events)

    def finish(self) -> None:
        self._buffer += self._decoder.decode(b"", final=True)
        if self._buffer or self._event is not None or self._data:
            raise ValueError("incomplete SSE frame")

    def _line_end(self) -> tuple[int, int] | None:
        for index, character in enumerate(self._buffer):
            if character == "\n":
                return index, 1
            if character == "\r":
                if index + 1 == len(self._buffer):
                    return None
                return index, 2 if self._buffer[index + 1] == "\n" else 1
        return None

    def _process_line(self, line: str) -> ServerSentEvent | None:
        if not line:
            if not self._data:
                self._event = None
                return None
            event = ServerSentEvent(
                event=self._event,
                data="\n".join(self._data),
            )
            self._event = None
            self._data = []
            return event
        if line.startswith(":"):
            return None
        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            self._event = value
        elif field == "data":
            self._data.append(value)
        return None


class ProviderHttpClient:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
        retry_delay_seconds: float,
        attempts: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        if not isfinite(retry_delay_seconds) or retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be finite and non-negative")
        if type(attempts) is not int or not 1 <= attempts <= 2:
            raise ValueError("provider attempts must be one or two")
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._retry_delay_seconds = retry_delay_seconds
        self._attempts = attempts
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "ProviderHttpClient":
        if self._client is not None:
            raise RuntimeError("provider HTTP client is already open")
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
            headers={
                "Accept": "application/json",
                "User-Agent": "WaterAgents/0.1 provider-adapters",
            },
        )
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def get_json(
        self,
        url: str,
        *,
        provider: str,
        operation: str,
        params: Mapping[str, QueryValue] | None = None,
        headers: Mapping[str, str] | None = None,
        deadline: float | None = None,
        on_attempt: Callable[[], None] | None = None,
        on_rate_limit: Callable[[float | None], float | None] | None = None,
        minimum_retry_delay_seconds: float = 0.0,
    ) -> object:
        content = await self.get_bytes(
            url,
            provider=provider,
            operation=operation,
            params=params,
            headers=headers,
            deadline=deadline,
            on_attempt=on_attempt,
            on_rate_limit=on_rate_limit,
            minimum_retry_delay_seconds=minimum_retry_delay_seconds,
        )
        try:
            return json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InvalidPortResponseError(
                "Provider returned invalid JSON.",
                provider=provider,
                operation=operation,
            ) from error

    async def post_json(
        self,
        url: str,
        body: Mapping[str, object],
        *,
        provider: str,
        operation: str,
        headers: Mapping[str, str] | None = None,
        deadline: float | None = None,
        on_attempt: Callable[[], None] | None = None,
    ) -> object:
        """Issue exactly one bounded JSON request.

        Retry ownership stays with the calling application workflow.  The request
        body and response body are deliberately excluded from failure logging.
        """

        client = self._client
        if client is None:
            raise RuntimeError("provider HTTP client is not open")
        await self._cancellation_checkpoint()
        self._remaining_timeout(deadline, provider, operation)
        if on_attempt is not None:
            on_attempt()
        try:
            content = await self._request_once(
                client,
                url,
                method="POST",
                body=body,
                provider=provider,
                operation=operation,
                params=None,
                headers=headers,
                deadline=deadline,
            )
        except PortError as error:
            self._log_failure(error, 1)
            raise
        try:
            return json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            port_error = InvalidPortResponseError(
                "Provider returned invalid JSON.",
                provider=provider,
                operation=operation,
            )
            self._log_failure(port_error, 1)
            raise port_error from error

    @asynccontextmanager
    async def post_sse(
        self,
        url: str,
        body: Mapping[str, object],
        *,
        provider: str,
        operation: str,
        headers: Mapping[str, str] | None = None,
        deadline: float | None = None,
        max_stream_bytes: int | None = None,
    ) -> AsyncIterator[AsyncIterator[ServerSentEvent]]:
        if max_stream_bytes is not None and (
            type(max_stream_bytes) is not int or not 0 < max_stream_bytes <= 67_108_864
        ):
            raise ValueError("max_stream_bytes must be between 1 and 67108864")
        client = self._client
        if client is None:
            raise RuntimeError("provider HTTP client is not open")
        try:
            timeout = self._remaining_timeout(deadline, provider, operation)
            async with asyncio.timeout(timeout):
                async with client.stream(
                    "POST",
                    url,
                    headers=headers,
                    json=body,
                    timeout=timeout,
                ) as response:
                    if response.status_code == 400:
                        await self._log_bad_request(response, provider, operation)
                    self._raise_for_status(
                        response.status_code,
                        provider=provider,
                        operation=operation,
                    )
                    content_type = response.headers.get("content-type", "")
                    media_type = content_type.partition(";")[0].strip().casefold()
                    if media_type != "text/event-stream":
                        raise InvalidPortResponseError(
                            "Provider returned an invalid event stream.",
                            provider=provider,
                            operation=operation,
                        )
                    yield self._iter_sse(
                        response,
                        provider=provider,
                        operation=operation,
                        max_stream_bytes=max_stream_bytes,
                    )
        except PortError as error:
            self._log_failure(error, 1)
            raise
        except (TimeoutError, httpx.TimeoutException) as error:
            port_error = PortTimeoutError(
                "Provider request timed out.",
                provider=provider,
                operation=operation,
            )
            self._log_failure(port_error, 1)
            raise port_error from error

        except (
            httpx.InvalidURL,
            httpx.UnsupportedProtocol,
        ) as error:
            port_error = PortConfigurationError(
                "Provider endpoint configuration is invalid.",
                provider=provider,
                operation=operation,
            )
            self._log_failure(port_error, 1)
            raise port_error from error
        except (
            httpx.ConnectError,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ) as error:
            port_error = PortUnavailableError(
                "Provider is unavailable.",
                provider=provider,
                operation=operation,
            )
            self._log_failure(port_error, 1)
            raise port_error from error

    @staticmethod
    async def _log_bad_request(
        response: httpx.Response,
        provider: str,
        operation: str,
    ) -> None:
        content = bytearray()
        async for chunk in response.aiter_bytes():
            content.extend(chunk)
            if len(content) > 8192:
                return
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict) or not isinstance(payload.get("error"), dict):
            return
        error = payload["error"]
        fields = {
            key: value
            for key, value in (
                ("provider_error_type", error.get("type")),
                ("provider_error_code", error.get("code")),
                ("provider_error_param", error.get("param")),
            )
            if isinstance(value, str) and 0 < len(value) <= 128
        }
        if fields:
            logger.warning(
                "provider_bad_request",
                extra={"provider": provider, "provider_operation": operation, **fields},
            )

    async def get_bytes(
        self,
        url: str,
        *,
        provider: str,
        operation: str,
        params: Mapping[str, QueryValue] | None = None,
        headers: Mapping[str, str] | None = None,
        deadline: float | None = None,
        on_attempt: Callable[[], None] | None = None,
        on_rate_limit: Callable[[float | None], float | None] | None = None,
        minimum_retry_delay_seconds: float = 0.0,
    ) -> bytes:
        return await self._request_bytes(
            url,
            provider=provider,
            operation=operation,
            params=params,
            headers=headers,
            deadline=deadline,
            on_attempt=on_attempt,
            on_rate_limit=on_rate_limit,
            minimum_retry_delay_seconds=minimum_retry_delay_seconds,
        )

    async def _iter_sse(
        self,
        response: httpx.Response,
        *,
        provider: str,
        operation: str,
        max_stream_bytes: int | None,
    ) -> AsyncIterator[ServerSentEvent]:
        decoder = _SseDecoder(max_frame_bytes=self._max_response_bytes)
        stream_limit = self._max_response_bytes if max_stream_bytes is None else max_stream_bytes
        received = 0
        try:
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > stream_limit:
                    raise PortResponseLimitError(
                        "Provider response exceeded the configured size limit.",
                        provider=provider,
                        operation=operation,
                    )
                for event in decoder.feed(chunk):
                    yield event
            decoder.finish()
        except _SseFrameLimitError:
            raise PortResponseLimitError(
                "Provider response exceeded the configured size limit.",
                provider=provider,
                operation=operation,
            ) from None
        except (UnicodeDecodeError, ValueError) as error:
            raise InvalidPortResponseError(
                "Provider returned an invalid event stream.",
                provider=provider,
                operation=operation,
            ) from error

    async def _request_bytes(
        self,
        url: str,
        *,
        provider: str,
        operation: str,
        params: Mapping[str, QueryValue] | None = None,
        headers: Mapping[str, str] | None = None,
        deadline: float | None = None,
        on_attempt: Callable[[], None] | None = None,
        on_rate_limit: Callable[[float | None], float | None] | None = None,
        minimum_retry_delay_seconds: float = 0.0,
    ) -> bytes:
        if not isfinite(minimum_retry_delay_seconds) or minimum_retry_delay_seconds < 0:
            raise ValueError("minimum_retry_delay_seconds must be finite and non-negative")
        client = self._client
        if client is None:
            raise RuntimeError("provider HTTP client is not open")
        for attempt in range(self._attempts):
            await self._cancellation_checkpoint()
            self._remaining_timeout(deadline, provider, operation)
            if on_attempt is not None:
                on_attempt()
            logger.info(
                "Provider GET attempt started.",
                extra={"event_name": "provider_get_attempt_started",
                       "provider": provider, "provider_operation": operation,
                       "attempt": attempt + 1},
            )
            try:
                return await self._request_once(
                    client,
                    url,
                    method="GET",
                    body=None,
                    provider=provider,
                    operation=operation,
                    params=params,
                    headers=headers,
                    deadline=deadline,
                )
            except PortError as error:
                cooldown_delay_seconds = 0.0
                if isinstance(error, PortRateLimitError) and on_rate_limit is not None:
                    cooldown_delay_seconds = on_rate_limit(error.retry_after_seconds) or 0.0
                if attempt + 1 == self._attempts or not self._is_retryable(error):
                    self._log_failure(error, attempt + 1)
                    raise
                retry_after_seconds = (
                    error.retry_after_seconds
                    if isinstance(error, PortRateLimitError)
                    else None
                )
                if (
                    isinstance(error, PortRateLimitError)
                    and deadline is None
                    and max(
                        self._retry_delay_seconds,
                        retry_after_seconds or 0.0,
                        minimum_retry_delay_seconds,
                        cooldown_delay_seconds,
                    )
                    > min(self._timeout_seconds, 30.0)
                ):
                    self._log_failure(error, attempt + 1)
                    raise
                try:
                    await self._wait_before_retry(
                        provider=provider,
                        operation=operation,
                        deadline=deadline,
                        retry_after_seconds=retry_after_seconds,
                        minimum_delay_seconds=max(
                            minimum_retry_delay_seconds, cooldown_delay_seconds
                        ),
                    )
                except PortTimeoutError:
                    if isinstance(error, PortRateLimitError):
                        self._log_failure(error, attempt + 1)
                    raise
        raise AssertionError("bounded provider request loop did not terminate")

    async def _request_once(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
        method: str,
        body: Mapping[str, object] | None,
        provider: str,
        operation: str,
        params: Mapping[str, QueryValue] | None,
        headers: Mapping[str, str] | None,
        deadline: float | None,
    ) -> bytes:
        timeout = self._remaining_timeout(deadline, provider, operation)
        started = asyncio.get_running_loop().time()
        try:
            async with asyncio.timeout(timeout):
                async with client.stream(
                    method,
                    url,
                    params=params or None,
                    headers=headers,
                    json=body,
                    timeout=timeout,
                ) as response:
                    retry_after_seconds = (
                        self._parse_retry_after(response.headers.get("retry-after"))
                        if method == "GET" and response.status_code == 429
                        else None
                    )
                    response_fields: dict[str, int | float] = {}
                    if provider == "usgs_nldi":
                        for header, field in (
                            ("x-ratelimit-limit", "provider_rate_limit_limit"),
                            ("x-ratelimit-remaining", "provider_rate_limit_remaining"),
                            ("x-ratelimit-reset", "provider_rate_limit_reset"),
                        ):
                            value = self._bounded_header_integer(response.headers.get(header))
                            if value is not None:
                                response_fields[field] = value
                        if retry_after_seconds is not None and isfinite(retry_after_seconds):
                            response_fields["provider_retry_after_seconds"] = retry_after_seconds
                    logger.info(
                        "Provider response headers received.",
                        extra={"event_name": "provider_response_headers_received",
                               "provider": provider, "provider_operation": operation,
                               "method": method, "status_code": response.status_code,
                               "duration_ms": (asyncio.get_running_loop().time() - started) * 1000,
                               **response_fields},
                    )
                    self._raise_for_status(
                        response.status_code,
                        provider=provider,
                        operation=operation,
                        retry_after_seconds=retry_after_seconds,
                    )
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > self._max_response_bytes:
                            raise PortResponseLimitError(
                                "Provider response exceeded the configured size limit.",
                                provider=provider,
                                operation=operation,
                            )
                    logger.info(
                        "Provider response body received.",
                        extra={"event_name": "provider_response_body_received",
                               "provider": provider, "provider_operation": operation,
                               "method": method, "response_bytes": len(content),
                               "duration_ms": (asyncio.get_running_loop().time() - started) * 1000},
                    )
                    return bytes(content)
        except PortError:
            raise
        except (TimeoutError, httpx.TimeoutException) as error:
            raise PortTimeoutError(
                "Provider request timed out.",
                provider=provider,
                operation=operation,
            ) from error
        except (
            httpx.InvalidURL,
            httpx.UnsupportedProtocol,
        ) as error:
            raise PortConfigurationError(
                "Provider endpoint configuration is invalid.",
                provider=provider,
                operation=operation,
            ) from error
        except (
            httpx.ConnectError,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ) as error:
            raise PortUnavailableError(
                "Provider is unavailable.",
                provider=provider,
                operation=operation,
            ) from error

    @staticmethod
    def _raise_for_status(
        status_code: int,
        *,
        provider: str,
        operation: str,
        retry_after_seconds: float | None = None,
    ) -> None:
        if 200 <= status_code < 300:
            return
        if status_code in {401, 403}:
            raise PortAuthenticationError(
                "Provider authentication failed.",
                provider=provider,
                operation=operation,
                status_code=status_code,
            )
        if status_code == 429:
            raise PortRateLimitError(
                "Provider rate limit was exceeded.",
                provider=provider,
                operation=operation,
                status_code=status_code,
                retry_after_seconds=retry_after_seconds,
            )
        raise PortHttpError(
            "Provider returned an HTTP error.",
            provider=provider,
            operation=operation,
            status_code=status_code,
        )

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        if value is None:
            return None
        value = value.strip()
        if value.isascii() and value.isdigit():
            try:
                return float(int(value))
            except (OverflowError, ValueError):
                return float("inf")
        try:
            retry_at = parsedate_to_datetime(value)
        except (IndexError, OverflowError, TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())

    @staticmethod
    def _bounded_header_integer(value: str | None) -> int | None:
        if value is None:
            return None
        value = value.strip()
        if not value or len(value) > 13 or not value.isascii() or not value.isdigit():
            return None
        return int(value)

    @staticmethod
    def _is_retryable(error: PortError) -> bool:
        if isinstance(error, (PortAuthenticationError, PortConfigurationError)):
            return False
        if isinstance(error, PortHttpError):
            return error.status_code is not None and error.status_code >= 500
        return isinstance(
            error,
            (PortRateLimitError, PortTimeoutError, PortUnavailableError),
        )

    def _remaining_timeout(
        self,
        deadline: float | None,
        provider: str,
        operation: str,
    ) -> float:
        if deadline is None:
            return self._timeout_seconds
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise PortTimeoutError(
                "Provider request deadline was exhausted.",
                provider=provider,
                operation=operation,
            )
        return min(self._timeout_seconds, remaining)

    async def _wait_before_retry(
        self,
        *,
        provider: str,
        operation: str,
        deadline: float | None,
        retry_after_seconds: float | None = None,
        minimum_delay_seconds: float = 0.0,
    ) -> None:
        await self._cancellation_checkpoint()
        delay_seconds = max(
            self._retry_delay_seconds, retry_after_seconds or 0.0, minimum_delay_seconds,
        )
        if deadline is not None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= delay_seconds:
                raise PortTimeoutError(
                    "Provider request deadline was exhausted before retry.",
                    provider=provider,
                    operation=operation,
                )
        await asyncio.sleep(delay_seconds)

    @staticmethod
    async def _cancellation_checkpoint() -> None:
        """Yield before every request/retry so cancellation cannot start new I/O."""

        await asyncio.sleep(0)

    @staticmethod
    def _log_failure(error: PortError, attempts: int) -> None:
        retry_after_seconds = (
            error.retry_after_seconds
            if isinstance(error, PortRateLimitError)
            else None
        )
        logger.warning(
            "provider_request_failed",
            extra={
                "provider": error.provider,
                "provider_operation": error.operation,
                "provider_status": error.status_code,
                "provider_error": type(error).__name__,
                "provider_attempts": attempts,
                **(
                    {"provider_retry_after_seconds": retry_after_seconds}
                    if retry_after_seconds is not None and isfinite(retry_after_seconds)
                    else {}
                ),
            },
        )

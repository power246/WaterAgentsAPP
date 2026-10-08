from __future__ import annotations

import asyncio
import codecs
import json
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
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


class _SseDecoder:
    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self._buffer = ""
        self._event: str | None = None
        self._data: list[str] = []

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
            event = self._process_line(line)
            if event is not None:
                events.append(event)
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
    ) -> object:
        content = await self.get_bytes(
            url,
            provider=provider,
            operation=operation,
            params=params,
            headers=headers,
            deadline=deadline,
            on_attempt=on_attempt,
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
    ) -> AsyncIterator[AsyncIterator[ServerSentEvent]]:
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
    ) -> bytes:
        return await self._request_bytes(
            url,
            provider=provider,
            operation=operation,
            params=params,
            headers=headers,
            deadline=deadline,
            on_attempt=on_attempt,
        )

    async def _iter_sse(
        self,
        response: httpx.Response,
        *,
        provider: str,
        operation: str,
    ) -> AsyncIterator[ServerSentEvent]:
        decoder = _SseDecoder()
        received = 0
        try:
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > self._max_response_bytes:
                    raise PortResponseLimitError(
                        "Provider response exceeded the configured size limit.",
                        provider=provider,
                        operation=operation,
                    )
                for event in decoder.feed(chunk):
                    yield event
            decoder.finish()
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
    ) -> bytes:
        client = self._client
        if client is None:
            raise RuntimeError("provider HTTP client is not open")
        for attempt in range(self._attempts):
            await self._cancellation_checkpoint()
            self._remaining_timeout(deadline, provider, operation)
            if on_attempt is not None:
                on_attempt()
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
                if attempt + 1 == self._attempts or not self._is_retryable(error):
                    self._log_failure(error, attempt + 1)
                    raise
                await self._wait_before_retry(
                    provider=provider,
                    operation=operation,
                    deadline=deadline,
                )
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
                    self._raise_for_status(
                        response.status_code,
                        provider=provider,
                        operation=operation,
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
            )
        raise PortHttpError(
            "Provider returned an HTTP error.",
            provider=provider,
            operation=operation,
            status_code=status_code,
        )

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
    ) -> None:
        await self._cancellation_checkpoint()
        if deadline is not None:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= self._retry_delay_seconds:
                raise PortTimeoutError(
                    "Provider request deadline was exhausted before retry.",
                    provider=provider,
                    operation=operation,
                )
        await asyncio.sleep(self._retry_delay_seconds)

    @staticmethod
    async def _cancellation_checkpoint() -> None:
        """Yield before every request/retry so cancellation cannot start new I/O."""

        await asyncio.sleep(0)

    @staticmethod
    def _log_failure(error: PortError, attempts: int) -> None:
        logger.warning(
            "provider_request_failed",
            extra={
                "provider": error.provider,
                "provider_operation": error.operation,
                "provider_status": error.status_code,
                "provider_error": type(error).__name__,
                "provider_attempts": attempts,
            },
        )

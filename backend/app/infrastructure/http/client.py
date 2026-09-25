from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
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

QueryValue = str | int | float | bool


class ProviderHttpClient:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        max_response_bytes: int,
        retry_delay_seconds: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        if not isfinite(retry_delay_seconds) or retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds must be finite and non-negative")
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._retry_delay_seconds = retry_delay_seconds
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
                "User-Agent": "WaterAgents/0.1 metadata-adapters",
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
    ) -> object:
        content = await self.get_bytes(
            url,
            provider=provider,
            operation=operation,
            params=params,
            headers=headers,
            deadline=deadline,
        )
        try:
            return json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InvalidPortResponseError(
                "Provider returned invalid JSON.",
                provider=provider,
                operation=operation,
            ) from error

    async def get_bytes(
        self,
        url: str,
        *,
        provider: str,
        operation: str,
        params: Mapping[str, QueryValue] | None = None,
        headers: Mapping[str, str] | None = None,
        deadline: float | None = None,
    ) -> bytes:
        client = self._client
        if client is None:
            raise RuntimeError("provider HTTP client is not open")

        for attempt in range(2):
            try:
                return await self._request_once(
                    client,
                    url,
                    provider=provider,
                    operation=operation,
                    params=params,
                    headers=headers,
                    deadline=deadline,
                )
            except PortError as error:
                if attempt or not self._is_retryable(error):
                    self._log_failure(error, attempt + 1)
                    raise
                await self._wait_before_retry(
                    provider=provider,
                    operation=operation,
                    deadline=deadline,
                )
        raise AssertionError("bounded provider retry loop did not terminate")

    async def _request_once(
        self,
        client: httpx.AsyncClient,
        url: str,
        *,
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
                    "GET",
                    url,
                    params=params or None,
                    headers=headers,
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

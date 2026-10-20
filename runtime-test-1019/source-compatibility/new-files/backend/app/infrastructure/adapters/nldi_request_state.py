from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from math import isfinite
from typing import TypeVar, cast

_Result = TypeVar("_Result")


class NldiRequestState:
    def __init__(
        self,
        *,
        cache_ttl_seconds: float = 0.0,
        cache_max_entries: int = 512,
    ) -> None:
        if not isfinite(cache_ttl_seconds) or cache_ttl_seconds < 0:
            raise ValueError("cache_ttl_seconds must be finite and nonnegative")
        if isinstance(cache_max_entries, bool) or cache_max_entries < 1:
            raise ValueError("cache_max_entries must be positive")
        self.request_lock = asyncio.Lock()
        self.next_request_time = 0.0
        self.rate_limited_until = 0.0
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache_max_entries = cache_max_entries
        self._cache: OrderedDict[
            tuple[object, ...], tuple[float, object]
        ] = OrderedDict()
        self._key_locks: dict[
            tuple[object, ...], tuple[asyncio.Lock, int]
        ] = {}

    def record_rate_limit(self, retry_after_seconds: float | None) -> float:
        delay = (
            retry_after_seconds
            if retry_after_seconds is not None
            and isfinite(retry_after_seconds)
            and retry_after_seconds >= 0
            else 30.0
        )
        now = asyncio.get_running_loop().time()
        self.rate_limited_until = max(
            self.rate_limited_until,
            now + delay,
        )
        return self.rate_limited_until - now

    async def cached(
        self,
        key: tuple[object, ...],
        load: Callable[[], Awaitable[_Result]],
    ) -> _Result:
        if self._cache_ttl_seconds == 0:
            return await load()
        hit, value = self._lookup(key)
        if hit:
            return cast(_Result, value)

        lock, users = self._key_locks.get(key, (asyncio.Lock(), 0))
        self._key_locks[key] = (lock, users + 1)
        try:
            async with lock:
                hit, value = self._lookup(key)
                if hit:
                    return cast(_Result, value)
                result = await load()
                self._cache[key] = (
                    asyncio.get_running_loop().time() + self._cache_ttl_seconds,
                    result,
                )
                self._cache.move_to_end(key)
                while len(self._cache) > self._cache_max_entries:
                    self._cache.popitem(last=False)
                return result
        finally:
            current_lock, users = self._key_locks[key]
            if users == 1:
                del self._key_locks[key]
            else:
                self._key_locks[key] = (current_lock, users - 1)

    def _lookup(self, key: tuple[object, ...]) -> tuple[bool, object | None]:
        entry = self._cache.get(key)
        if entry is None:
            return False, None
        expires_at, value = entry
        if expires_at <= asyncio.get_running_loop().time():
            del self._cache[key]
            return False, None
        self._cache.move_to_end(key)
        return True, value

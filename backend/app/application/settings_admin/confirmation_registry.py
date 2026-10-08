"""Bounded, single-process confirmation claims shared by settings and Admin actions."""

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from hmac import compare_digest
from secrets import token_urlsafe
from typing import AsyncIterator, Generic, TypeVar

from app.application.settings_admin.errors import (
    AdminConfirmationInvalidError, AdminPreviewExpiredError,
)
from app.domain.common import normalize_utc_datetime
from app.domain.settings_admin import AdminConfirmation


T = TypeVar("T")


@dataclass(frozen=True, slots=True, repr=False)
class _Claim(Generic[T]):
    action: str
    phrase: str
    expires_at: datetime
    payload: T


class ConfirmationRegistry(Generic[T]):
    def __init__(self, *, capacity: int = 128, ttl_seconds: int = 600) -> None:
        if capacity < 1 or ttl_seconds < 1:
            raise ValueError("confirmation registry limits must be positive")
        self._capacity = capacity
        self._ttl = timedelta(seconds=ttl_seconds)
        self._lock = asyncio.Lock()
        self._claims: OrderedDict[bytes, _Claim[T]] = OrderedDict()

    async def issue(
        self, *, action: str, phrase: str, payload: T,
        now: datetime,
    ) -> AdminConfirmation:
        issued_at = normalize_utc_datetime(now)
        token = token_urlsafe(32)
        digest = sha256(token.encode("utf-8")).digest()
        expiry = issued_at + self._ttl
        async with self._lock:
            self._evict(issued_at)
            if len(self._claims) >= self._capacity:
                self._claims.popitem(last=False)
            self._claims[digest] = _Claim(action, phrase, expiry, payload)
        return AdminConfirmation(token=token, expires_at=expiry, required_phrase=phrase)

    @asynccontextmanager
    async def claim(
        self, *, token: str, action: str, phrase: str,
        now: datetime,
    ) -> AsyncIterator[tuple[T, bytes]]:
        digest = sha256(token.encode("utf-8")).digest()
        async with self._lock:
            claim = self._claims.get(digest)
            if claim is None:
                raise AdminConfirmationInvalidError()
            if normalize_utc_datetime(now) >= claim.expires_at:
                del self._claims[digest]
                raise AdminPreviewExpiredError()
            if not compare_digest(claim.action, action) or not compare_digest(claim.phrase, phrase):
                raise AdminConfirmationInvalidError()
            yield claim.payload, digest
            del self._claims[digest]

    def _evict(self, now: datetime) -> None:
        for digest, claim in tuple(self._claims.items()):
            if claim.expires_at <= now:
                del self._claims[digest]

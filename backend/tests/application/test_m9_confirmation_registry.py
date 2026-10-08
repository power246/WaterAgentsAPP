"""Confirmation tokens expire, bind one action, and are consumed once."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.application.settings_admin.confirmation_registry import ConfirmationRegistry
from app.application.settings_admin.errors import (
    AdminConfirmationInvalidError, AdminPreviewExpiredError,
)


def test_action_phrase_expiry_and_single_use() -> None:
    async def exercise():
        now = datetime(2026, 10, 8, tzinfo=UTC)
        registry = ConfirmationRegistry[str](ttl_seconds=2, capacity=2)
        confirmation = await registry.issue(
            action="unit_policy",
            phrase="APPLY UNIT POLICY 2", payload="bound facts", now=now,
        )
        with pytest.raises(AdminConfirmationInvalidError):
            async with registry.claim(
                token=confirmation.token, action="history_cleanup",
                phrase=confirmation.required_phrase, now=now,
            ):
                pass
        with pytest.raises(AdminConfirmationInvalidError):
            async with registry.claim(
                token=confirmation.token, action="unit_policy",
                phrase="wrong", now=now,
            ):
                pass
        async with registry.claim(
            token=confirmation.token, action="unit_policy",
            phrase=confirmation.required_phrase, now=now,
        ) as (facts, digest):
            assert facts == "bound facts" and len(digest) == 32
        with pytest.raises(AdminConfirmationInvalidError):
            async with registry.claim(
                token=confirmation.token, action="unit_policy",
                phrase=confirmation.required_phrase, now=now,
            ):
                pass
        expired = await registry.issue(
            action="unit_policy",
            phrase="APPLY UNIT POLICY 3", payload="later", now=now,
        )
        with pytest.raises(AdminPreviewExpiredError):
            async with registry.claim(
                token=expired.token, action="unit_policy",
                phrase=expired.required_phrase, now=now + timedelta(seconds=2),
            ):
                pass

    asyncio.run(exercise())

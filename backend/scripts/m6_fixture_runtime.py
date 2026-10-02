from __future__ import annotations

from contextlib import asynccontextmanager

from app.infrastructure.runtime import (
    OpenAIResponsesSizeEstimator,
    WaterbodyAdapterRegistration,
)
from app.infrastructure.runtime.m6_test import build_m6_test_runtime


class _UnscriptedFixtureWaterbodyModelPort:
    async def stream(self, request):
        del request
        raise AssertionError("smoke app received an unscripted M6 model call")
        yield  # pragma: no cover


@asynccontextmanager
async def build_fixture_m6_runtime(settings, **kwargs):
    """Compose M6 for fixture catalogs without relaxing production startup."""

    async with build_m6_test_runtime(
        settings,
        registrations=(
            WaterbodyAdapterRegistration(
                provider="fixture",
                port=_UnscriptedFixtureWaterbodyModelPort(),
                context_window_tokens=128_000,
                max_output_tokens=32_768,
                size_estimator=OpenAIResponsesSizeEstimator(),
            ),
        ),
        **kwargs,
    ) as runtime:
        yield runtime

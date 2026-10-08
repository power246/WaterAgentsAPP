from __future__ import annotations

from contextlib import asynccontextmanager

from app.application.transcript_memory import (
    RAGSearchUnavailableError,
    TranscriptEvidenceProducer,
)


class _BuildingSearchSession:
    async def search(self, intent, *, operation_id, deadline=None):
        del intent, operation_id, deadline
        raise RAGSearchUnavailableError("index_building")

    def clear(self) -> None:
        pass


class _BuildingSearchService:
    def bind(self, scope):
        del scope
        return _BuildingSearchSession()


class FixtureM8Runtime:
    def __init__(self) -> None:
        self.producer = TranscriptEvidenceProducer(_BuildingSearchService())

    async def start(self) -> None:
        pass

    def notify_run_terminal(self, run_id) -> None:
        del run_id

    async def shutdown(self) -> None:
        pass


@asynccontextmanager
async def build_fixture_m8_runtime(*args, **kwargs):
    """Compose an offline M8 runtime without relaxing production startup."""

    del args, kwargs
    yield FixtureM8Runtime()

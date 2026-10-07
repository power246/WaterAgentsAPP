from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from app.domain import RAGIndexGeneration, RAGIndexJob, RAGSourceCutoff, UtcDatetime


class RAGDiscoveryStore(Protocol):
    async def latest_candidate_cutoff(self) -> RAGSourceCutoff: ...

    async def discover_jobs(
        self,
        generation_id: str,
        *,
        through: RAGSourceCutoff | None,
        limit: int,
        discovered_at: UtcDatetime,
    ) -> tuple[RAGIndexJob, ...]: ...


@dataclass(frozen=True, slots=True)
class RAGDiscoveryResult:
    jobs: tuple[RAGIndexJob, ...]
    exhausted: bool


class RAGDiscoveryService:
    """The only application entry point for durable candidate discovery."""

    def __init__(self, store: RAGDiscoveryStore, *, batch_size: int) -> None:
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError("discovery batch_size must be a positive integer")
        self._store = store
        self._batch_size = batch_size

    async def latest_cutoff(self) -> RAGSourceCutoff:
        return await self._store.latest_candidate_cutoff()

    async def reconcile(
        self,
        generation: RAGIndexGeneration,
        *,
        discovered_at: UtcDatetime,
        max_batches: int = 1,
    ) -> RAGDiscoveryResult:
        if type(max_batches) is not int or max_batches < 1:
            raise ValueError("max_batches must be a positive integer")
        through = (
            RAGSourceCutoff(
                created_at=generation.source_cutoff_created_at,
                item_id=generation.source_cutoff_item_id,
            )
            if generation.status.value == "building"
            else None
        )
        discovered: list[RAGIndexJob] = []
        exhausted = False
        for _ in range(max_batches):
            batch = await self._store.discover_jobs(
                generation.generation_id,
                through=through,
                limit=self._batch_size,
                discovered_at=discovered_at,
            )
            discovered.extend(batch)
            if len(batch) < self._batch_size:
                exhausted = True
                break
        return RAGDiscoveryResult(jobs=tuple(discovered), exhausted=exhausted)


class RAGReconciliationSignal:
    """A lossy wake hint; correctness remains in periodic durable reconciliation."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def notify_run_terminal(self) -> None:
        self._event.set()

    async def wait(self, timeout_seconds: float) -> bool:
        if timeout_seconds <= 0:
            raise ValueError("reconciliation timeout must be positive")
        try:
            async with asyncio.timeout(timeout_seconds):
                await self._event.wait()
        except TimeoutError:
            return False
        self._event.clear()
        return True


class RAGReconciliationRunner:
    """Runs the same bounded reconciliation after wake hints and periodic timeouts."""

    def __init__(
        self,
        signal: RAGReconciliationSignal,
        *,
        interval_seconds: float,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("reconciliation interval must be positive")
        self._signal = signal
        self._interval_seconds = interval_seconds
        self._stopping = False

    def request_stop(self) -> None:
        self._stopping = True
        self._signal.notify_run_terminal()

    async def run(self, reconcile: Callable[[], Awaitable[object]]) -> None:
        while not self._stopping:
            await reconcile()
            if self._stopping:
                return
            await self._signal.wait(self._interval_seconds)

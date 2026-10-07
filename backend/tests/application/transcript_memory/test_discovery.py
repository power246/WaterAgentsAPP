import asyncio
from datetime import UTC, datetime
from uuid import UUID

from app.application.transcript_memory import (
    RAGDiscoveryService,
    RAGReconciliationSignal,
    RAGReconciliationRunner,
)
from app.domain import (
    EmbeddingProfile,
    RAGIndexGeneration,
    RAGIndexGenerationStatus,
    RAGIndexJob,
    RAGIndexJobStatus,
    RAGSourceCutoff,
)


NOW = datetime(2026, 10, 8, 8, tzinfo=UTC)
GENERATION_ID = "85000000-0000-4000-8000-000000000001"
SOURCE_ID = "85000000-0000-4000-8000-000000000002"


class _DiscoveryStore:
    def __init__(self) -> None:
        self.calls: list[RAGSourceCutoff | None] = []
        self.jobs: dict[str, RAGIndexJob] = {}

    async def latest_candidate_cutoff(self) -> RAGSourceCutoff:
        return RAGSourceCutoff(created_at=NOW, item_id=UUID(SOURCE_ID))

    async def discover_jobs(self, generation_id, *, through, limit, discovered_at):
        self.calls.append(through)
        job = self.jobs.setdefault(
            generation_id,
            RAGIndexJob(
                job_id="85000000-0000-4000-8000-000000000003",
                generation_id=generation_id,
                source_run_id="85000000-0000-4000-8000-000000000004",
                source_session_id="85000000-0000-4000-8000-000000000005",
                source_result_item_id=SOURCE_ID,
                status=RAGIndexJobStatus.PENDING,
                attempts=0,
                created_at=discovered_at,
                updated_at=discovered_at,
            ),
        )
        return (job,) if len(self.calls) == 1 else ()


def _generation(status: RAGIndexGenerationStatus) -> RAGIndexGeneration:
    profile = EmbeddingProfile(
        provider="test",
        model="embed",
        output_dimensions=3,
        max_input_tokens=512,
        semantic_min_similarity=0.75,
    )
    return RAGIndexGeneration(
        generation_id=GENERATION_ID,
        profile=profile,
        status=status,
        source_cutoff_created_at=NOW,
        source_cutoff_item_id=SOURCE_ID,
        created_at=NOW,
        activated_at=NOW if status is RAGIndexGenerationStatus.ACTIVE else None,
    )


def test_building_discovery_uses_cutoff_and_repeat_reconciliation_is_idempotent() -> None:
    async def exercise() -> None:
        store = _DiscoveryStore()
        service = RAGDiscoveryService(store, batch_size=1)

        first = await service.reconcile(
            _generation(RAGIndexGenerationStatus.BUILDING),
            discovered_at=NOW,
            max_batches=2,
        )
        restarted = RAGDiscoveryService(store, batch_size=1)
        second = await restarted.reconcile(
            _generation(RAGIndexGenerationStatus.BUILDING),
            discovered_at=NOW,
        )

        assert first.exhausted
        assert len(store.jobs) == 1
        assert second.jobs == ()
        assert all(call is not None for call in store.calls)

    asyncio.run(exercise())


def test_active_reconciliation_does_not_depend_on_wake_delivery() -> None:
    async def exercise() -> None:
        store = _DiscoveryStore()
        service = RAGDiscoveryService(store, batch_size=4)
        signal = RAGReconciliationSignal()

        # No signal is sent. Periodic reconciliation still reaches durable discovery.
        result = await service.reconcile(
            _generation(RAGIndexGenerationStatus.ACTIVE),
            discovered_at=NOW,
        )
        signal.notify_run_terminal()

        assert result.exhausted
        assert len(result.jobs) == 1
        assert store.calls == [None]
        assert await signal.wait(1)

    asyncio.run(exercise())


def test_reconciliation_runner_repeats_after_lost_wake_and_stops_cleanly() -> None:
    async def exercise() -> None:
        signal = RAGReconciliationSignal()
        runner = RAGReconciliationRunner(signal, interval_seconds=0.001)
        reached_second_cycle = asyncio.Event()
        calls = 0

        async def reconcile() -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                reached_second_cycle.set()

        running = asyncio.create_task(runner.run(reconcile))
        async with asyncio.timeout(1):
            await reached_second_cycle.wait()
        runner.request_stop()
        await running

        assert calls == 2

    asyncio.run(exercise())

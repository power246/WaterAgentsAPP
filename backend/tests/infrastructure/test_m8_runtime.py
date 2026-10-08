import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.application.analysis_runs.models import RunStatus
from app.application.transcript_memory import (
    GenerationStartupReadiness,
    GenerationStartupState,
    RAGReconciliationSignal,
)
from app.domain import (
    ModelCatalogSnapshot,
    RAGIndexGeneration,
    RAGIndexGenerationStatus,
    RAGJobCounts,
)
from app.infrastructure.config import AppSettings, SettingsConfigurationError
from app.infrastructure.observability.json_logging import JsonFormatter
from app.infrastructure.observability.rag_metrics import RAGRuntimeMetrics
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.infrastructure.runtime import M8Runtime, build_m8_runtime
from tests.m4_support import (
    MODEL_CATALOG,
    FakeM8Runtime,
    create_request_bytes,
    create_test_app,
)
from tests.infrastructure.embeddings.fake import DeterministicEmbeddingPort


class FixedClock:
    def now(self):
        return datetime.now(UTC)


def test_m8_builder_rejects_a_missing_desired_embedding_profile() -> None:
    async def exercise() -> None:
        with pytest.raises(SettingsConfigurationError, match="embedding profile"):
            async with build_m8_runtime(
                AppSettings(_env_file=None),
                engine=object(),  # type: ignore[arg-type]
                transcript_reader=object(),
                transcript_registry=object(),  # type: ignore[arg-type]
                model_catalog=ModelCatalogSnapshot(capabilities=()),
                clock=FixedClock(),
                foundation_validated=True,
                embedding_port=DeterministicEmbeddingPort(),
            ):
                raise AssertionError("invalid M8 startup reached the yield boundary")

    asyncio.run(exercise())


def test_m8_builder_rejects_an_unvalidated_foundation_before_database_access() -> None:
    async def exercise() -> None:
        with pytest.raises(SettingsConfigurationError, match="foundation"):
            async with build_m8_runtime(
                AppSettings(_env_file=None),
                engine=object(),  # type: ignore[arg-type]
                transcript_reader=object(),
                transcript_registry=object(),  # type: ignore[arg-type]
                model_catalog=MODEL_CATALOG.snapshot(),
                clock=FixedClock(),
                foundation_validated=False,
                embedding_port=DeterministicEmbeddingPort(),
            ):
                raise AssertionError("invalid M8 startup reached the yield boundary")

    asyncio.run(exercise())


def test_rag_metrics_use_only_bounded_safe_labels(caplog) -> None:
    metrics = RAGRuntimeMetrics()
    secret = "raw-query api_key=private vector=[1,2,3]"
    with caplog.at_level(logging.INFO, logger="wateragents.rag"):
        metrics.observe_embedding(
            stage=secret,
            outcome=secret,
            latency_seconds=0.1,
        )
        metrics.observe_search(
            strategy=secret,
            outcome=secret,
            latency_seconds=0.2,
            result_count=999,
        )
        metrics.observe_search_candidates(
            strategy=secret,
            candidate_count=999,
        )
        metrics.observe_hydration_invalid(secret)
        metrics.observe_generation(
            status=secret,
            age_seconds=1,
            counts=RAGJobCounts(pending=1, retry_wait=2, failed=3),
            lag_seconds=4,
            active_profile_digest=secret,
        )

    encoded = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    assert secret not in encoded
    assert "semantic_case_query" in encoded
    assert "rag_generation_health" in encoded
    assert '"candidate_count":50' in encoded
    assert "rag_hydration_invalid" in encoded
    assert metrics.snapshot()["search"] == (
        ("semantic_case_query", "invalid", 1),
    )


def test_m8_settings_reject_unknown_and_out_of_range_retention() -> None:
    with pytest.raises(Exception, match="rag_retired_generation_retention_seconds"):
        AppSettings(
            _env_file=None,
            rag_retired_generation_retention_seconds=-1,
        )
    with pytest.raises(Exception, match="extra_forbidden"):
        AppSettings(_env_file=None, rag_worker_hidden_switch=True)


def test_terminal_commit_wakes_m8_and_shutdown_stops_it_before_coordinator() -> None:
    store = InMemoryTranscriptStore()
    events: list[str] = []

    class TrackedM8Runtime(FakeM8Runtime):
        def __init__(self) -> None:
            super().__init__()
            self.shutdown_calls = 0

        def notify_run_terminal(self, run_id) -> None:
            assert store.write_batches
            batch = store.write_batches[-1]
            assert batch.run_id == run_id
            assert any(
                mutation.snapshot.status
                in {
                    RunStatus.COMPLETED,
                    RunStatus.COMPLETED_WITH_PERSISTENCE_ERROR,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                }
                for mutation in batch.run_mutations
            )
            super().notify_run_terminal(run_id)

        async def shutdown(self) -> None:
            self.shutdown_calls += 1
            if not self.stopped:
                events.append("m8_shutdown")
            await super().shutdown()

    runtime = TrackedM8Runtime()

    @asynccontextmanager
    async def tracked_builder(*args, **kwargs):
        del args, kwargs
        yield runtime

    application = create_test_app(
        AppSettings(_env_file=None),
        transcript_store=store,
        m8_runtime_builder=tracked_builder,
        shutdown_grace_period=0,
    )

    async def exercise() -> None:
        async with application.router.lifespan_context(application):
            coordinator = application.state.run_coordinator
            shutdown = coordinator.shutdown

            async def tracked_shutdown() -> None:
                events.append("coordinator_shutdown")
                await shutdown()

            coordinator.shutdown = tracked_shutdown
            created = await coordinator.create_run(create_request_bytes("M8 wake"))
            await coordinator._runs[created.run_id].workflow_quiesced.wait()
            async with asyncio.timeout(1):
                while runtime.wakes == 0:
                    await asyncio.sleep(0)
            assert runtime.started
            assert runtime.wakes == 1

    asyncio.run(exercise())

    assert events == ["m8_shutdown", "coordinator_shutdown"]
    assert runtime.stopped
    assert runtime.shutdown_calls == 1


def test_shutdown_gate_prevents_activation_after_worker_returns() -> None:
    profile = MODEL_CATALOG.snapshot().embedding_profile
    generation = RAGIndexGeneration(
        generation_id=str(UUID(int=1)),
        profile=profile,
        status=RAGIndexGenerationStatus.BUILDING,
        source_cutoff_created_at=datetime.now(UTC),
        source_cutoff_item_id=str(UUID(int=2)),
        created_at=datetime.now(UTC),
    )

    class Backfill:
        def __init__(self) -> None:
            self.activations = 0

        async def ensure_building_generation(self, desired_profile) -> None:
            assert desired_profile == profile

        async def reconcile(self, *, discovered_at):
            del discovered_at
            return SimpleNamespace(building=SimpleNamespace(exhausted=True))

        async def activate_when_complete(self, target, *, activated_at) -> None:
            del target, activated_at
            self.activations += 1

    class Repository:
        async def list_generations(self):
            return (generation,)

        async def counts(self, generation_id):
            assert generation_id == generation.generation_id
            return RAGJobCounts()

        async def oldest_incomplete_job_at(self, generation_id):
            assert generation_id == generation.generation_id
            return None

    class Worker:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run_available(self, target) -> int:
            assert target == generation
            self.started.set()
            await self.release.wait()
            return 0

        def request_stop(self) -> None:
            self.release.set()

        async def drain(self, timeout_seconds) -> bool:
            del timeout_seconds
            return True

    class Runner:
        def request_stop(self) -> None:
            pass

    async def exercise() -> None:
        backfill = Backfill()
        worker = Worker()
        runtime = M8Runtime(
            producer=FakeM8Runtime().producer,
            worker=worker,  # type: ignore[arg-type]
            backfill=backfill,  # type: ignore[arg-type]
            discovery=object(),  # type: ignore[arg-type]
            repository=Repository(),  # type: ignore[arg-type]
            operations=object(),  # type: ignore[arg-type]
            signal=RAGReconciliationSignal(),
            runner=Runner(),  # type: ignore[arg-type]
            metrics=RAGRuntimeMetrics(),
            startup_state=GenerationStartupState(
                readiness=GenerationStartupReadiness.INDEX_BUILDING,
                active_generation=None,
                building_generation=generation,
            ),
            desired_profile=profile,
            clock=FixedClock(),
            shutdown_grace_seconds=1,
        )
        reconciling = asyncio.create_task(runtime._reconcile())
        await worker.started.wait()
        await runtime.shutdown()
        await reconciling

        assert backfill.activations == 0

    asyncio.run(exercise())

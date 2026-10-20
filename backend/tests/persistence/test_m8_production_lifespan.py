import asyncio
from contextlib import asynccontextmanager

from app.domain import RAGIndexGenerationStatus
from app.infrastructure.runtime import build_m8_runtime
from scripts.test_database import temporary_migrated_schema
from tests.infrastructure.embeddings.fake import DeterministicEmbeddingPort
from tests.m4_support import create_test_app


def test_production_lifespan_builds_one_worker_and_injects_its_m5_producer() -> None:
    captured = []

    @asynccontextmanager
    async def deterministic_m8_builder(settings, **kwargs):
        async with build_m8_runtime(
            settings,
            embedding_port=DeterministicEmbeddingPort(),
            lease_owner_factory=lambda: "m8-production-lifespan-test",
            **kwargs,
        ) as runtime:
            captured.append(runtime)
            yield runtime

    async def exercise(settings) -> None:
        application = create_test_app(
            settings.model_copy(
                update={
                    "memory_enabled": True,
                    "rag_reconciliation_interval_seconds": 0.01,
                    "rag_worker_shutdown_grace_seconds": 1,
                }
            ),
            m8_runtime_builder=deterministic_m8_builder,
        )
        paths_before = set(application.openapi()["paths"])
        async with application.router.lifespan_context(application):
            runtime = application.state.m8_runtime
            assert captured == [runtime]
            assert runtime.startup_state.readiness.value == "index_building"
            assert application.state.m5_runtime.transcript_producer is runtime.producer
            assert runtime._task is not None
            assert runtime._task.get_name() == "rag-index-worker"
            assert not runtime._task.done()
            for _ in range(100):
                active = await runtime.repository.read_active_generation()
                if active is not None:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("first generation was not activated")
            assert active.status is RAGIndexGenerationStatus.ACTIVE
            assert runtime.metrics.snapshot()["generation"]
            assert set(application.openapi()["paths"]) == paths_before
        assert runtime._task is not None and runtime._task.done()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))

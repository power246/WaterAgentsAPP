import asyncio
import os
import threading

import pytest
from sqlalchemy import text

from app.application.analysis_runs.models import AgentRole
from app.application.guidelines.worker import GuidelinePreparationWorker
from app.domain.guidelines import GuidelinePolicy
from app.infrastructure.persistence.database import create_database_engine
from app.infrastructure.persistence.guideline_repository import PostgresGuidelineRepository
from app.infrastructure.persistence.store import PostgresTranscriptStore
from scripts.test_database import temporary_migrated_schema
from tests.application.analysis_runs._m7_projection_support import HoldingWorkflow, coordinator_for
from tests.application.analysis_runs._topology_scheduler_support import topology
from tests.application.guidelines.test_retrieval import REGISTRY, PROFILE, pin
from tests.application.guidelines.test_worker import Embedding
from tests.m4_support import analysis_context, create_request_bytes


@pytest.mark.skipif(not os.environ.get("WATERAGENTS_TEST_DB_NAME"), reason="dedicated PostgreSQL required")
@pytest.mark.parametrize("interruption", [None, "run", "encoding_task"])
def test_topology_encoding_allows_other_run_pins_and_writes_before_release(interruption):
    async def exercise(settings):
        engine = create_database_engine(settings)
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(PostgresTranscriptStore(engine, operation_timeout_seconds=5), workflow)
        repository = PostgresGuidelineRepository(settings, engine=engine)
        entered, release = asyncio.Event(), threading.Event()
        encoding = None
        loop = asyncio.get_running_loop()
        main_thread = threading.get_ident()
        try:
            async def embedding_factory():
                return Embedding()
            worker = GuidelinePreparationWorker(repository, REGISTRY, PROFILE, GuidelinePolicy(), embedding_factory)
            await worker.reconcile_generation()
            first = await coordinator.create_run(create_request_bytes("First concurrent analysis"))
            await workflow.calls.get()
            parser = await coordinator.prepare_agent_session(first.run_id, agent_role=AgentRole.QUERY_PARSER)
            await coordinator.mark_agent_session_running(first.run_id, parser.session_id)
            context = await coordinator.reserve_transcript_item(first.run_id, session_id=parser.session_id)
            await coordinator.finalize_transcript_item(first.run_id, context.item_id, "query_understood", 1,
                                                       analysis_context("First concurrent analysis"))
            await coordinator.complete_agent_session(first.run_id, parser.session_id)
            reserved = await coordinator.reserve_transcript_item(first.run_id)
            original = coordinator._registry.encode

            def blocked_encode(content_type, *args, **kwargs):
                if content_type == "topology_ready":
                    assert threading.get_ident() != main_thread
                    loop.call_soon_threadsafe(entered.set)
                    if not release.wait(5):
                        raise TimeoutError("test topology encoder was not released")
                return original(content_type, *args, **kwargs)

            coordinator._registry.encode = blocked_encode
            encoding = asyncio.create_task(coordinator.finalize_transcript_item(
                first.run_id, reserved.item_id, "topology_ready", 1, topology(("100", "300"))
            ))
            await asyncio.wait_for(entered.wait(), 2)
            second = await coordinator.create_run(create_request_bytes("Second concurrent analysis"))
            await workflow.calls.get()
            pins = await asyncio.gather(pin(repository, run_id=first.run_id), pin(repository, run_id=second.run_id))
            assert all(value.pin.status == "ready" for value in pins)
            async with engine.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM analysis_runs")) == 2
            assert not release.is_set() and not encoding.done()
            if interruption == "run":
                await coordinator.cancel_run(first.run_id)
            elif interruption == "encoding_task":
                encoding.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await encoding
            release.set()
            if interruption != "encoding_task":
                await encoding
            contents = coordinator._runs[first.run_id].transcript_buffer.items
            assert any(item.content.content_type == "topology_ready" for item in contents) == (interruption is None)
        finally:
            release.set()
            if encoding is not None:
                await asyncio.gather(encoding, return_exceptions=True)
            workflow.release.set()
            await coordinator.shutdown()
            await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))

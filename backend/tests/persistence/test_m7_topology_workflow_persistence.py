import asyncio
from collections.abc import Iterator

import pytest

from app.application.analysis_runs.models import AgentRole, RunStatus
from app.application.analysis_runs.topology_result_models import DependencyStatus
from app.application.analysis_runs.persistence import PersistenceState, PersistenceWriteBatch
from app.application.transcripts.read_models import ReadView
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.persistence.database import create_database_engine
from app.infrastructure.persistence.store import PostgresTranscriptStore
from scripts.test_database import temporary_migrated_schema
from tests.application.analysis_runs._m7_projection_support import (
    HoldingWorkflow,
    complete_m7_run,
    coordinator_for,
)
from tests.m4_support import create_request_bytes, create_test_app
from tests.smoke.test_m7_topology_workflow_smoke import (
    TARGET,
    complete_workflow_runtime,
    run_to_quiescence,
)


@pytest.fixture(scope="module")
def postgres_settings() -> Iterator[AppSettings]:
    with temporary_migrated_schema() as settings:
        yield settings


class FailTopologyWriteOnce(PostgresTranscriptStore):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.failed = False
        self.topology_attempts = 0

    async def write_batch(self, batch: PersistenceWriteBatch) -> None:
        if any(
            item.content_type == "topology_run_result"
            for item in batch.transcript.items
        ):
            self.topology_attempts += 1
            if not self.failed:
                self.failed = True
                raise OSError("injected topology result write failure")
        await super().write_batch(batch)


async def _wait_persistence(coordinator, run_id) -> None:
    async with asyncio.timeout(5):
        while True:
            runtime = coordinator._runs[run_id]
            async with runtime.condition:
                if coordinator._transcript_writer.info_locked(runtime).state is (
                    PersistenceState.UP_TO_DATE
                ):
                    return
            await asyncio.sleep(0)


def test_m7_results_reload_with_reference_closure_retry_history_and_startup_repair(
    postgres_settings: AppSettings,
) -> None:
    async def exercise() -> None:
        settings = postgres_settings.model_copy(update={
            "transcript_inline_max_bytes": 256,
            "transcript_preview_bytes": 40,
        })
        engine = create_database_engine(settings)
        store = FailTopologyWriteOnce(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(
            store,
            workflow,
            inline_content_bytes=256,
            preview_bytes=40,
        )
        try:
            completed = await complete_m7_run(coordinator, workflow)
            await _wait_persistence(coordinator, completed.run_id)
            assert store.failed is True
            assert store.topology_attempts == 2
        finally:
            await coordinator.shutdown()
            await engine.dispose()

        interrupted_engine = create_database_engine(settings)
        interrupted_store = PostgresTranscriptStore(
            interrupted_engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        interrupted_workflow = HoldingWorkflow()
        interrupted_coordinator = coordinator_for(
            interrupted_store,
            interrupted_workflow,
        )
        try:
            interrupted = await interrupted_coordinator.create_run(
                create_request_bytes("M7 interrupted")
            )
            await interrupted_workflow.calls.get()
            session = await interrupted_coordinator.prepare_agent_session(
                interrupted.run_id,
                agent_role=AgentRole.QUERY_PARSER,
            )
            await interrupted_coordinator.mark_agent_session_running(
                interrupted.run_id,
                session.session_id,
            )
            await _wait_persistence(interrupted_coordinator, interrupted.run_id)
        finally:
            await interrupted_coordinator.shutdown()
            await interrupted_engine.dispose()

        restarted = create_test_app(settings, shutdown_grace_period=0)
        async with restarted.router.lifespan_context(restarted):
            reader = restarted.state.transcript_reader
            detail = await reader.get_run(completed.run_id, ReadView.HISTORY)
            history = await reader.list_runs(limit=200, cursor=None)
            summary_transcript = await reader.get_session_transcript(
                completed.run_id,
                completed.summary_session_id,
                ReadView.HISTORY,
            )
            topology_item = next(
                item
                for item in detail.items
                if item.content_type == "topology_run_result"
            )
            assert topology_item.content.kind == "reference"
            content = await reader.get_content(
                completed.run_id,
                topology_item.item_id,
                ReadView.HISTORY,
            )
            assert '"target_comid":"300"' in content.text
            assert detail.run.result_ref.item_id == topology_item.item_id
            assert detail.run.result_summary.summary_status.value == "completed"
            assert {
                item.content_type for item in detail.referenced_items
            } >= {"inference_result", "run_summary_result"}
            assert [
                item.content_type for item in summary_transcript.items[-2:]
            ] == ["run_summary_result", "lifecycle_event"]
            listed = next(
                item for item in history.items if item.run_id == completed.run_id
            )
            assert listed.result_ref == detail.run.result_ref
            repaired = await restarted.state.transcript_store.read_run(
                interrupted.run_id
            )
            assert repaired.run.status is RunStatus.FAILED

    asyncio.run(asyncio.wait_for(exercise(), timeout=30))


def test_production_m7_workflow_round_trips_through_postgres_restart(
    postgres_settings: AppSettings,
) -> None:
    async def exercise() -> None:
        settings = postgres_settings.model_copy(
            update={
                "transcript_inline_max_bytes": 256,
                "transcript_preview_bytes": 40,
            }
        )
        engine = create_database_engine(settings)
        store = PostgresTranscriptStore(
            engine,
            operation_timeout_seconds=settings.db_operation_timeout_seconds,
        )
        try:
            async with complete_workflow_runtime(
                (("100", "200", TARGET),),
                {
                    "100": DependencyStatus.USABLE,
                    "200": DependencyStatus.USABLE,
                    TARGET: DependencyStatus.USABLE,
                },
                settings=settings,
                transcript_store=store,
            ) as runtime:
                created, snapshot = await run_to_quiescence(runtime)
                await _wait_persistence(runtime.coordinator, created.run_id)
                assert snapshot.status is RunStatus.COMPLETED
                assert snapshot.result_ref is not None
                assert snapshot.summary.result_ref is not None

            restarted = create_test_app(settings, shutdown_grace_period=0)
            async with restarted.router.lifespan_context(restarted):
                detail = await restarted.state.transcript_reader.get_run(
                    created.run_id,
                    ReadView.HISTORY,
                )
                assert detail.run.status is RunStatus.COMPLETED
                assert detail.run.result_ref == snapshot.result_ref
                assert detail.run.result_summary.summary_status.value == "completed"
                content_types = {item.content_type for item in detail.items}
                for session in detail.sessions:
                    transcript = (
                        await restarted.state.transcript_reader.get_session_transcript(
                            created.run_id,
                            session.session_id,
                            ReadView.HISTORY,
                        )
                    )
                    content_types.update(
                        item.content_type for item in transcript.items
                    )
                assert content_types >= {
                    "waterbody_dependency_manifest",
                    "run_summary_result",
                    "topology_run_result",
                }
                assert {
                    item.content_type for item in detail.referenced_items
                } >= {"inference_result", "run_summary_result"}
        finally:
            await engine.dispose()

    asyncio.run(asyncio.wait_for(exercise(), timeout=30))

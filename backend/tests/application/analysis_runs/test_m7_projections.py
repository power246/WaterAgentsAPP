import asyncio
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.application.analysis_runs.models import RunCompletedEvent, RunStatus
from app.application.analysis_runs.persistence import PersistenceState
from app.application.transcripts.read_models import ReadView
from app.application.transcripts.reader import TranscriptReader
from app.application.transcripts.registry import create_production_registry
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from tests.application.analysis_runs._m7_projection_support import (
    HoldingWorkflow,
    complete_m7_run,
    coordinator_for,
)


def run(exercise) -> None:
    asyncio.run(asyncio.wait_for(exercise, timeout=10))


def test_summary_and_topology_results_share_their_terminal_writer_groups() -> None:
    async def exercise() -> None:
        store = InMemoryTranscriptStore()
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(store, workflow)
        try:
            completed = await complete_m7_run(coordinator, workflow)
            runtime = coordinator._runs[completed.run_id]
            summary = runtime.agent_sessions[completed.summary_session_id].summary
            summary_mutation = next(
                mutation
                for mutation in runtime.state_mutations
                if getattr(mutation, "summary", None) == summary
            )
            run_mutation = next(
                mutation
                for mutation in runtime.state_mutations
                if getattr(mutation, "snapshot", None) == runtime.snapshot
            )
            summary_batch = next(
                batch
                for batch in store.write_batches
                if summary_mutation.mutation_id in batch.mutation_ids
            )
            summary_group = next(
                group
                for group in summary_batch.groups
                if summary_mutation.mutation_id in group.mutation_ids
            )
            summary_types = {
                item.content_type
                for item in summary_batch.transcript.items
                if item.item_id in summary_group.item_ids
            }
            assert summary_types == {
                "run_summary_agent_message",
                "run_summary_result",
                "lifecycle_event",
            }

            run_batch = next(
                batch
                for batch in store.write_batches
                if run_mutation.mutation_id in batch.mutation_ids
            )
            run_group = next(
                group
                for group in run_batch.groups
                if run_mutation.mutation_id in group.mutation_ids
            )
            run_types = {
                item.content_type
                for item in run_batch.transcript.items
                if item.item_id in run_group.item_ids
            }
            assert run_types == {"topology_run_result", "lifecycle_event"}
            assert completed.ready_statuses == ("ready", "ready")
            terminal_event = next(
                event for event in runtime.events if isinstance(event, RunCompletedEvent)
            )
            corrupt = terminal_event.model_dump(mode="python")
            corrupt["payload"]["result_ref"]["run_id"] = uuid4()
            with pytest.raises(
                ValidationError,
                match="run result reference must remain in the run",
            ):
                RunCompletedEvent.model_validate(corrupt)
            assert runtime.snapshot.status is RunStatus.COMPLETED
            assert coordinator._transcript_writer.info_locked(runtime).state is (
                PersistenceState.UP_TO_DATE
            )
        finally:
            await coordinator.shutdown()

    run(exercise())


def test_run_snapshot_projects_partial_nodes_safe_summary_failure_and_result_ref() -> None:
    async def exercise() -> None:
        store = InMemoryTranscriptStore()
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(store, workflow)
        try:
            completed = await complete_m7_run(
                coordinator,
                workflow,
                partial=True,
                summary_failed=True,
            )
            stream = await coordinator.subscribe_run(completed.run_id)
            snapshot = await anext(stream)
            await stream.aclose()
            assert snapshot.status is RunStatus.COMPLETED
            assert snapshot.analysis_phase.value == "terminal"
            assert snapshot.result_ref is not None
            assert snapshot.summary.status.value == "failed"
            assert snapshot.summary.error.code == "run_summary_failed"
            assert snapshot.graph_outcome.coverage.value == "partial"
            assert snapshot.graph_outcome.usable_nodes == 1
            assert snapshot.graph_outcome.invalid_nodes == 1
            assert [item.dependency_status.value for item in snapshot.waterbodies] == [
                "invalid",
                "usable",
            ]
            assert all(item.scheduling_status.value == "terminal" for item in snapshot.waterbodies)
            assert "plain_language_summary" not in snapshot.model_dump_json()
            reader = TranscriptReader(
                coordinator,
                store,
                create_production_registry(),
                error_id_factory=lambda: completed.run_id,
                inline_content_bytes=64 * 1024,
                preview_bytes=1024,
            )
            detail = await reader.get_run(completed.run_id, ReadView.HISTORY)
            assert detail.run.result_summary.summary_error.code == "run_summary_failed"
        finally:
            await coordinator.shutdown()

    run(exercise())


def test_history_and_detail_project_only_the_persisted_topology_result() -> None:
    async def exercise() -> None:
        store = InMemoryTranscriptStore()
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(store, workflow)
        try:
            completed = await complete_m7_run(coordinator, workflow)
            reader = TranscriptReader(
                coordinator,
                store,
                create_production_registry(),
                error_id_factory=lambda: completed.run_id,
                inline_content_bytes=64 * 1024,
                preview_bytes=1024,
            )
            detail = await reader.get_run(completed.run_id, ReadView.HISTORY)
            history = await reader.list_runs(limit=10, cursor=None)
            assert detail.run.result_ref is not None
            assert detail.run.result_summary.target_comid == "300"
            assert detail.run.result_summary.summary_status.value == "completed"
            assert detail.issues == ()
            listed = next(item for item in history.items if item.run_id == completed.run_id)
            assert listed.result_ref == detail.run.result_ref
            assert listed.result_summary == detail.run.result_summary
            stored = await store.read_run(completed.run_id)
            terminal = next(
                item
                for item in detail.items
                if item.content_type == "lifecycle_event"
                and item.item_id
                == stored.run.terminal_item_id
            )
            assert terminal.content.kind == "inline"
        finally:
            await coordinator.shutdown()

    run(exercise())

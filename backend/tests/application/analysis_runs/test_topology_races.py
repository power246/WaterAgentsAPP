import asyncio
from datetime import UTC, datetime

import pytest

from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    RunStatus,
)
from app.application.analysis_runs.coordinator import (
    InvalidAgentSessionTransitionError,
    InvalidRunTransitionError,
)
from app.application.run_summary.models import RunSummaryResult
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from tests.application.analysis_runs._m7_projection_support import (
    HoldingWorkflow,
    coordinator_for,
    prepare_m7_run,
    summary_message_for,
)
from tests.m4_support import WATERBODY_MODEL, create_request_bytes


def _summary_result(run_id, session_id) -> RunSummaryResult:
    return RunSummaryResult(
        run_id=run_id,
        session_id=session_id,
        input_digest="sha256:summary-race",
        model=WATERBODY_MODEL,
        language="en",
        plain_language_summary="The committed target result remains available.",
        generated_at=datetime(2026, 10, 3, 10, tzinfo=UTC),
    )


def test_cancel_accepted_while_final_commit_waits_writes_no_topology_result() -> None:
    async def exercise() -> None:
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(InMemoryTranscriptStore(), workflow)
        try:
            prepared = await prepare_m7_run(coordinator, workflow)
            entered = asyncio.Event()
            release = asyncio.Event()
            original = coordinator._transcript_writer.wait_for_required

            async def gated(*args, **kwargs):
                entered.set()
                await release.wait()
                return await original(*args, **kwargs)

            coordinator._transcript_writer.wait_for_required = gated
            committing = asyncio.create_task(coordinator.complete_run(
                prepared.run_id,
                prepared.topology_result,
            ))
            await asyncio.wait_for(entered.wait(), timeout=1)
            cancelled = await coordinator.cancel_run(prepared.run_id)
            release.set()
            final = await asyncio.wait_for(committing, timeout=1)

            assert cancelled.status is final.status is RunStatus.CANCELLED
            runtime = coordinator._runs[prepared.run_id]
            assert runtime.result_ref is None
            assert all(
                item.content.content_type != "topology_run_result"
                for item in runtime.transcript_buffer.items
            )
        finally:
            workflow.release.set()
            await coordinator.shutdown()

    asyncio.run(exercise())


def test_summary_completion_and_cancel_have_one_terminal_winner() -> None:
    async def cancel_first() -> None:
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(InMemoryTranscriptStore(), workflow)
        try:
            created = await coordinator.create_run(create_request_bytes("summary race"))
            await workflow.calls.get()
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.RUN_SUMMARIZER,
            )
            await coordinator.mark_agent_session_running(
                created.run_id,
                session.session_id,
            )
            await coordinator.cancel_run(created.run_id)
            result = _summary_result(created.run_id, session.session_id)
            terminal = await coordinator.complete_run_summary_agent_session(
                created.run_id,
                session.session_id,
                result,
                message=summary_message_for(result),
            )

            assert terminal.status is AgentSessionStatus.CANCELLED
            assert all(
                item.content.content_type != "run_summary_result"
                for item in coordinator._runs[created.run_id].transcript_buffer.items
            )
            with pytest.raises(InvalidRunTransitionError):
                await coordinator.prepare_agent_session(
                    created.run_id,
                    agent_role=AgentRole.WATERBODY,
                    target_comid="100",
                )
        finally:
            workflow.release.set()
            await coordinator.shutdown()

    async def completion_first() -> None:
        workflow = HoldingWorkflow()
        coordinator = coordinator_for(InMemoryTranscriptStore(), workflow)
        try:
            created = await coordinator.create_run(create_request_bytes("summary race"))
            await workflow.calls.get()
            session = await coordinator.prepare_agent_session(
                created.run_id,
                agent_role=AgentRole.RUN_SUMMARIZER,
            )
            await coordinator.mark_agent_session_running(
                created.run_id,
                session.session_id,
            )
            result = _summary_result(created.run_id, session.session_id)
            terminal = await coordinator.complete_run_summary_agent_session(
                created.run_id,
                session.session_id,
                result,
                message=summary_message_for(result),
            )
            await coordinator.cancel_run(created.run_id)

            assert terminal.status is AgentSessionStatus.COMPLETED
            assert terminal.result_ref is not None
            assert (
                await coordinator.get_session(created.run_id, session.session_id)
            ).status is AgentSessionStatus.COMPLETED
            with pytest.raises(InvalidAgentSessionTransitionError):
                await coordinator.complete_run_summary_agent_session(
                    created.run_id,
                    session.session_id,
                    result,
                    message=summary_message_for(result),
                )
            assert sum(
                item.content.content_type == "run_summary_result"
                for item in coordinator._runs[created.run_id].transcript_buffer.items
            ) == 1
        finally:
            workflow.release.set()
            await coordinator.shutdown()

    asyncio.run(cancel_first())
    asyncio.run(completion_first())

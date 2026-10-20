import asyncio

import pytest

from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    RunStatus,
)
from app.application.analysis_runs.persistence import PersistenceState
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from tests.application.analysis_runs._m7_projection_support import coordinator_for
from tests.m4_support import create_request_bytes


class _CancellationResistantWorkflow:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.release = asyncio.Event()
        self.completed_session_id = None
        self.in_flight_session_id = None

    async def run(self, context) -> None:
        parser = await context.operations.prepare_agent_session(
            context.run.run_id,
            agent_role=AgentRole.QUERY_PARSER,
        )
        await context.operations.mark_agent_session_running(
            context.run.run_id,
            parser.session_id,
        )
        await context.operations.complete_agent_session(
            context.run.run_id,
            parser.session_id,
        )
        waterbody = await context.operations.prepare_agent_session(
            context.run.run_id,
            agent_role=AgentRole.WATERBODY,
            target_comid="100",
        )
        await context.operations.mark_agent_session_running(
            context.run.run_id,
            waterbody.session_id,
        )
        self.completed_session_id = parser.session_id
        self.in_flight_session_id = waterbody.session_id
        self.started.set()
        await context.cancellation.wait()
        self.cancel_seen.set()
        await self.release.wait()


def test_cancel_acceptance_precedes_quiescence_stream_close_and_cleanup() -> None:
    async def exercise() -> None:
        workflow = _CancellationResistantWorkflow()
        cleaned = []
        coordinator = coordinator_for(
            InMemoryTranscriptStore(),
            workflow,
            live_state_grace_period=0,
        )

        async def cleanup(run_id) -> None:
            cleaned.append(run_id)

        coordinator.register_run_cleanup_hook(cleanup)
        try:
            created = await coordinator.create_run(create_request_bytes("quiescence"))
            await asyncio.wait_for(workflow.started.wait(), timeout=1)
            runtime = coordinator._runs[created.run_id]
            cancelled = await coordinator.cancel_run(created.run_id)
            await asyncio.wait_for(workflow.cancel_seen.wait(), timeout=1)

            assert cancelled.status is RunStatus.CANCELLED
            assert runtime.workflow_quiesced.is_set() is False
            assert cleaned == []
            completed = await coordinator.get_session(
                created.run_id,
                workflow.completed_session_id,
            )
            in_flight = await coordinator.get_session(
                created.run_id,
                workflow.in_flight_session_id,
            )
            assert completed.status is AgentSessionStatus.COMPLETED
            assert in_flight.status is AgentSessionStatus.CANCELLED

            stream = await coordinator.subscribe_run(created.run_id)
            assert (await anext(stream)).status is RunStatus.CANCELLED
            waiting_for_end = asyncio.create_task(anext(stream))
            await asyncio.sleep(0)
            assert waiting_for_end.done() is False

            workflow.release.set()
            await asyncio.wait_for(runtime.workflow_quiesced.wait(), timeout=1)
            await asyncio.wait_for(coordinator._transcript_writer.drain(runtime), timeout=1)
            final = await asyncio.wait_for(waiting_for_end, timeout=1)
            assert final.status is RunStatus.CANCELLED
            assert final.persistence.state is PersistenceState.UP_TO_DATE
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(anext(stream), timeout=1)
            async with asyncio.timeout(1):
                while not cleaned:
                    await asyncio.sleep(0)
            assert cleaned == [created.run_id]
        finally:
            workflow.release.set()
            await coordinator.shutdown()

    asyncio.run(asyncio.wait_for(exercise(), timeout=5))

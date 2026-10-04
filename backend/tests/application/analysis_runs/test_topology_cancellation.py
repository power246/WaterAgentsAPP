import asyncio
from types import SimpleNamespace

import pytest

from app.application.analysis_runs.topology_scheduler import (
    TopologyScheduler,
    WaterbodyWorkflowLimiter,
)
from app.application.analysis_runs.workflow import CancellationToken, WorkflowStopped
from app.application.run_summary.runner import RunSummaryRunStatus
from app.application.run_summary.stage import RunSummaryStage
from tests.application.analysis_runs._topology_scheduler_support import (
    RUN_ID,
    RecordingOutcomePort,
    outcome,
    topology,
)
from tests.application.run_summary.test_runner import (
    _StageOperations,
    _StageRunnerFactory,
)
from tests.application.run_summary.support import MODEL, NOW


class _NoCallRunner:
    def __init__(self, cancellation=None) -> None:
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancellation = cancellation

    async def run(self, request):
        self.calls.append(request.node.comid)
        self.started.set()
        if self.cancellation is not None:
            self.cancellation.set()
            return outcome(request.node.comid)
        await self.release.wait()
        return outcome(request.node.comid)

    async def settle_failure(self, request, error):
        raise AssertionError("cancelled scheduler must not settle a failure") from error


class _BuilderThatCancels:
    def __init__(self, stop_requested) -> None:
        self.stop_requested = stop_requested

    async def build(self, **kwargs):
        self.stop_requested.set()
        return SimpleNamespace()


class _CountingRunnerFactory(_StageRunnerFactory):
    def __init__(self) -> None:
        self.calls = 0

    def build(self, *, run_id, session_id):
        self.calls += 1
        return super().build(run_id=run_id, session_id=session_id)


def test_cancel_before_dequeue_starts_no_node_or_publish() -> None:
    async def exercise() -> None:
        stop_requested = asyncio.Event()
        stop_requested.set()
        runner = _NoCallRunner()
        published = RecordingOutcomePort()
        scheduler = TopologyScheduler(
            run_id=RUN_ID,
            topology=topology(("100", "300")),
            runner=runner,
            outcome_port=published,
            limiter=WaterbodyWorkflowLimiter(1),
            cancellation=CancellationToken(stop_requested),
        )

        with pytest.raises(WorkflowStopped):
            await scheduler.run()
        assert runner.calls == []
        assert published.outcomes == []

    asyncio.run(exercise())


def test_cancel_after_node_completion_suppresses_publish_and_downstream() -> None:
    async def exercise() -> None:
        stop_requested = asyncio.Event()
        runner = _NoCallRunner(stop_requested)
        published = RecordingOutcomePort()
        scheduler = TopologyScheduler(
            run_id=RUN_ID,
            topology=topology(("100", "300")),
            runner=runner,
            outcome_port=published,
            limiter=WaterbodyWorkflowLimiter(1),
            cancellation=CancellationToken(stop_requested),
        )

        with pytest.raises(WorkflowStopped):
            await scheduler.run()
        assert runner.calls == ["100"]
        assert published.outcomes == []

    asyncio.run(exercise())


def test_cancel_wakes_limiter_waiters_without_starting_another_node() -> None:
    async def exercise() -> None:
        stop_requested = asyncio.Event()
        runner = _NoCallRunner()
        scheduler = TopologyScheduler(
            run_id=RUN_ID,
            topology=topology(("100", "300"), ("200", "300")),
            runner=runner,
            outcome_port=RecordingOutcomePort(),
            limiter=WaterbodyWorkflowLimiter(1),
            cancellation=CancellationToken(stop_requested),
        )
        task = asyncio.create_task(scheduler.run())
        await asyncio.wait_for(runner.started.wait(), timeout=1)
        stop_requested.set()

        with pytest.raises(WorkflowStopped):
            await asyncio.wait_for(task, timeout=1)
        assert len(runner.calls) == 1

    asyncio.run(exercise())


def test_summary_cancel_checkpoints_prevent_session_or_model_creation() -> None:
    async def exercise(cancel_before_session: bool) -> None:
        stop_requested = asyncio.Event()
        if cancel_before_session:
            stop_requested.set()
        operations = _StageOperations()
        factory = _CountingRunnerFactory()
        stage = RunSummaryStage(
            operations=operations,
            input_builder=_BuilderThatCancels(stop_requested),
            runner_factory=factory,
        )
        graph = topology(("100", "300"))
        aggregation = SimpleNamespace(
            outcomes=(outcome("100"), outcome("300")),
        )

        result = await stage.run(
            run_id=RUN_ID,
            query="Summarize.",
            topology=graph,
            topology_ref=SimpleNamespace(run_id=RUN_ID),
            aggregation=aggregation,
            model=MODEL,
            model_fit=SimpleNamespace(model=MODEL),
            deadline=NOW,
            cancellation=CancellationToken(stop_requested),
        )

        assert result.status is RunSummaryRunStatus.CANCELLED
        assert factory.calls == 0
        if cancel_before_session:
            assert operations.prepared == []
        else:
            assert len(operations.prepared) == 1

    asyncio.run(exercise(True))
    asyncio.run(exercise(False))

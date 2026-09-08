import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from app.api.schemas.analysis_sessions import CreateAnalysisSessionRequest
from app.application.analysis_sessions.coordinator import (
    InvalidSessionTransitionError,
    ServiceShuttingDownError,
    SessionCoordinator,
)
from app.application.analysis_sessions.models import SessionStatus
from app.application.analysis_sessions.workflow import WorkflowContext, WorkflowFailed
from app.infrastructure.config import AppSettings
from app.main import create_app

CONFIGURATION = CreateAnalysisSessionRequest(query="query").to_configuration_snapshot(
    "America/Chicago"
)
SECRET = "Authorization: Bearer private-token; query=private-location"


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 9, 8, 10, tzinfo=timezone.utc)


@dataclass
class InFlightCall:
    context: WorkflowContext
    release: asyncio.Event = field(default_factory=asyncio.Event)
    returning: asyncio.Event = field(default_factory=asyncio.Event)
    error: Exception | None = None


class GatedWorkflow:
    def __init__(self) -> None:
        self.calls: asyncio.Queue[InFlightCall] = asyncio.Queue()
        self.downstream: list[UUID] = []

    async def in_flight(self, context: WorkflowContext) -> None:
        call = InFlightCall(context)
        self.calls.put_nowait(call)
        await call.release.wait()
        call.returning.set()
        if call.error is not None:
            raise call.error

    async def run(self, context: WorkflowContext) -> None:
        context.cancellation.checkpoint()
        await self.in_flight(context)
        context.cancellation.checkpoint()
        self.downstream.append(context.session.session_id)


class LateReturningWorkflow(GatedWorkflow):
    async def run(self, context: WorkflowContext) -> None:
        await self.in_flight(context)


class CooperativeWorkflow:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def run(self, context: WorkflowContext) -> None:
        self.started.set()
        await context.cancellation.wait()
        self.stopped.set()
        context.cancellation.checkpoint()


@asynccontextmanager
async def application_runtime(workflow=None, *, shutdown_grace_period=0):
    application = create_app(
        clock=FixedClock(), workflow=workflow, shutdown_grace_period=shutdown_grace_period
    )
    async with application.router.lifespan_context(application):
        coordinator = application.state.session_coordinator
        try:
            yield coordinator
        finally:
            owned = [runtime.workflow_task for runtime in coordinator._sessions.values()]
    assert all(task.done() for task in owned)
    assert not set(owned).intersection(asyncio.all_tasks())
    assert all(runtime.workflow_quiesced.is_set() for runtime in coordinator._sessions.values())


def run(exercise) -> None:
    asyncio.run(asyncio.wait_for(exercise(), timeout=5))


async def wait_quiesced(coordinator, session_id):
    await coordinator._sessions[session_id].workflow_quiesced.wait()


async def events_for(coordinator, session_id):
    return [event async for event in await coordinator.subscribe(session_id)]


def records_for(caplog, event_name):
    return [record for record in caplog.records if getattr(record, "event_name", None) == event_name]


@pytest.mark.parametrize("eager", [False, True])
def test_default_workflow_is_owned_before_pending_response_and_completes(eager) -> None:
    async def exercise() -> None:
        loop = asyncio.get_running_loop()
        original_factory = loop.get_task_factory()
        if eager:
            if not hasattr(asyncio, "eager_task_factory"):
                pytest.skip("Eager task factories require Python 3.12+")
            loop.set_task_factory(asyncio.eager_task_factory)
        try:
            async with application_runtime() as coordinator:
                created = await coordinator.create_session("query", CONFIGURATION)
                runtime = coordinator._sessions[created.session_id]
                assert created.status is SessionStatus.PENDING
                assert runtime.workflow_task is not None
                assert not runtime.workflow_task.done()
                assert not runtime.workflow_quiesced.is_set()
                await wait_quiesced(coordinator, created.session_id)
                events = await events_for(coordinator, created.session_id)
                completed = await coordinator.get_snapshot(created.session_id)
                assert completed.status is SessionStatus.COMPLETED
                assert created.status is SessionStatus.PENDING
                assert [event.type.value for event in events] == ["session_created", "session_completed"]
                assert [event.sequence for event in events] == [1, 2]
                assert set(events[-1].payload.model_dump()) == {"status", "completed_at"}
                assert runtime.workflow_task.done()
        finally:
            loop.set_task_factory(original_factory)

    run(exercise)


@pytest.mark.parametrize("failure_type", [None, WorkflowFailed, RuntimeError])
def test_controlled_outcomes_use_running_context_and_safe_unique_terminal(failure_type, caplog) -> None:
    caplog.set_level(logging.INFO, logger="wateragents.analysis_sessions")

    async def exercise() -> None:
        workflow = GatedWorkflow()
        async with application_runtime(workflow) as coordinator:
            created = await coordinator.create_session(SECRET, CONFIGURATION)
            call = await workflow.calls.get()
            assert call.context.session is await coordinator.get_snapshot(created.session_id)
            assert call.context.session.status is SessionStatus.RUNNING
            assert call.context.session.configuration is created.configuration
            assert call.context.query == SECRET
            assert SECRET not in repr(call.context)
            if failure_type is not None:
                call.error = failure_type(SECRET)
            call.release.set()
            await wait_quiesced(coordinator, created.session_id)
            snapshot = await coordinator.get_snapshot(created.session_id)
            events = await events_for(coordinator, created.session_id)
            assert snapshot.status is (SessionStatus.FAILED if failure_type else SessionStatus.COMPLETED)
            assert len(events) == 2
            assert events[-1].payload.status is snapshot.status
            assert len(records_for(caplog, "workflow_quiesced")) == 1
            assert not records_for(caplog, "workflow_task_error")
            if failure_type is not None:
                error = events[-1].payload.error
                assert error is coordinator._sessions[created.session_id].last_error
                assert error.error_id != created.session_id
                assert error.code == "workflow_failed"
                assert error.message == "The analysis workflow failed."
                assert error.failed_stage == "analysis_workflow"
                assert error.retryable is False
                failures = records_for(caplog, "session_failed")
                assert len(failures) == 1
                assert failures[0].exception_type == failure_type.__name__
                assert failures[0].correlation.error_id == error.error_id
                assert failures[0].correlation.session_id == created.session_id
                assert workflow.downstream == []
            else:
                assert workflow.downstream == [created.session_id]
            assert SECRET not in "".join(event.model_dump_json() for event in events)
            assert SECRET not in repr([record.__dict__ for record in caplog.records])
            with pytest.raises(InvalidSessionTransitionError) as conflict:
                await coordinator.cancel_session(created.session_id)
            assert conflict.value.session is snapshot
            assert await events_for(coordinator, created.session_id) == events

    run(exercise)


def test_pending_cancel_does_not_enter_workflow(caplog) -> None:
    caplog.set_level(logging.INFO, logger="wateragents.analysis_sessions")

    async def exercise() -> None:
        workflow = GatedWorkflow()
        async with application_runtime(workflow) as coordinator:
            created = await coordinator.create_session("query", CONFIGURATION)
            cancelled = await coordinator.cancel_session(created.session_id)
            runtime = coordinator._sessions[created.session_id]
            assert cancelled.status is SessionStatus.CANCELLED
            assert runtime.cancel_requested.is_set()
            assert runtime.stop_requested.is_set()
            assert not runtime.workflow_quiesced.is_set()
            assert await coordinator.cancel_session(created.session_id) is cancelled
            await wait_quiesced(coordinator, created.session_id)
            assert workflow.calls.empty()
            assert not runtime.workflow_task.cancelled()
            assert [event.type.value for event in await events_for(coordinator, created.session_id)] == [
                "session_created", "session_cancelled"
            ]
            assert len(records_for(caplog, "cancel_accepted")) == 1
            assert len(records_for(caplog, "workflow_quiesced")) == 1

    run(exercise)


@pytest.mark.parametrize("late_failure", [False, True])
@pytest.mark.parametrize("workflow_type", [GatedWorkflow, LateReturningWorkflow])
def test_cancel_returns_before_quiescence_and_suppresses_late_output(workflow_type, late_failure, caplog) -> None:
    caplog.set_level(logging.INFO, logger="wateragents.analysis_sessions")

    async def exercise() -> None:
        workflow = workflow_type()
        async with application_runtime(workflow) as coordinator:
            created = await coordinator.create_session("query", CONFIGURATION)
            call = await workflow.calls.get()
            runtime = coordinator._sessions[created.session_id]
            cancelled = await coordinator.cancel_session(created.session_id)
            assert call.context.cancellation.is_requested()
            assert not runtime.workflow_task.done()
            assert runtime.workflow_task.cancelling() == 0
            assert not runtime.workflow_quiesced.is_set()
            assert await coordinator.cancel_session(created.session_id) is cancelled
            events = await events_for(coordinator, created.session_id)
            assert len(events) == 2
            assert events[-1].payload.status is SessionStatus.CANCELLED
            if late_failure:
                call.error = RuntimeError(SECRET)
            call.release.set()
            await wait_quiesced(coordinator, created.session_id)
            assert not runtime.workflow_task.cancelled()
            assert await coordinator.get_snapshot(created.session_id) is cancelled
            assert await events_for(coordinator, created.session_id) == events
            assert workflow.downstream == []
            assert runtime.last_error is None
            assert len(records_for(caplog, "workflow_output_suppressed")) == 1
            assert len(records_for(caplog, "workflow_quiesced")) == 1
            assert len(records_for(caplog, "cancel_accepted")) == 1
            assert not records_for(caplog, "session_failed")
            assert SECRET not in repr([record.__dict__ for record in caplog.records])

    run(exercise)


@pytest.mark.parametrize("cancel_first", [False, True])
@pytest.mark.parametrize("failure", [False, True])
def test_real_workflow_and_cancel_race_commit_one_terminal(cancel_first, failure) -> None:
    async def exercise() -> None:
        workflow = GatedWorkflow()
        async with application_runtime(workflow) as coordinator:
            created = await coordinator.create_session("query", CONFIGURATION)
            call = await workflow.calls.get()
            if failure:
                call.error = WorkflowFailed("controlled failure")
            runtime = coordinator._sessions[created.session_id]
            cancel_started = asyncio.Event()

            async def cancel():
                cancel_started.set()
                return await coordinator.cancel_session(created.session_id)

            async with runtime.condition:
                if cancel_first:
                    task = asyncio.create_task(cancel())
                    await cancel_started.wait()
                call.release.set()
                await call.returning.wait()
                if not cancel_first:
                    task = asyncio.create_task(cancel())
                    await cancel_started.wait()
            result = (await asyncio.gather(task, return_exceptions=True))[0]
            await wait_quiesced(coordinator, created.session_id)
            snapshot = await coordinator.get_snapshot(created.session_id)
            events = await events_for(coordinator, created.session_id)
            if cancel_first:
                assert result is snapshot
                assert snapshot.status is SessionStatus.CANCELLED
            else:
                assert isinstance(result, InvalidSessionTransitionError)
                assert result.session is snapshot
                assert snapshot.status is (SessionStatus.FAILED if failure else SessionStatus.COMPLETED)
            assert len(events) == 2
            assert [event.sequence for event in events] == [1, 2]
            assert events[-1].payload.status is snapshot.status

    run(exercise)


def test_concurrent_workflows_have_independent_cancellation_and_completion() -> None:
    async def exercise() -> None:
        workflow = GatedWorkflow()
        async with application_runtime(workflow) as coordinator:
            first = await coordinator.create_session("first", CONFIGURATION)
            second = await coordinator.create_session("second", CONFIGURATION)
            calls = [await workflow.calls.get(), await workflow.calls.get()]
            by_id = {call.context.session.session_id: call for call in calls}
            await coordinator.cancel_session(first.session_id)
            assert by_id[first.session_id].context.cancellation.is_requested()
            assert not by_id[second.session_id].context.cancellation.is_requested()
            by_id[second.session_id].release.set()
            await wait_quiesced(coordinator, second.session_id)
            assert not coordinator._sessions[first.session_id].workflow_quiesced.is_set()
            assert (await coordinator.get_snapshot(second.session_id)).status is SessionStatus.COMPLETED
            by_id[first.session_id].release.set()
            await wait_quiesced(coordinator, first.session_id)
            assert workflow.downstream == [second.session_id]
            for session in (first, second):
                events = await events_for(coordinator, session.session_id)
                assert all(event.session_id == session.session_id for event in events)
                assert [event.sequence for event in events] == [1, 2]

    run(exercise)


@pytest.mark.parametrize("already_running", [False, True])
def test_shutdown_signals_cooperative_workflow_without_fabricating_cancel(already_running, caplog) -> None:
    caplog.set_level(logging.INFO, logger="wateragents.analysis_sessions")

    async def exercise() -> None:
        workflow = CooperativeWorkflow()
        async with application_runtime(workflow, shutdown_grace_period=1) as coordinator:
            created = await coordinator.create_session("query", CONFIGURATION)
            if already_running:
                await workflow.started.wait()
            before = await coordinator.get_snapshot(created.session_id)
            runtime = coordinator._sessions[created.session_id]
            await coordinator.shutdown()
            assert await coordinator.get_snapshot(created.session_id) is before
            assert runtime.stop_requested.is_set()
            assert not runtime.cancel_requested.is_set()
            assert runtime.workflow_task.done()
            assert not runtime.workflow_task.cancelled()
            assert workflow.stopped.is_set() is already_running
            assert len(runtime.events) == 1
            records = records_for(caplog, "service_shutdown")
            assert len(records) == 1
            assert records[0].original_status is before.status
            assert records[0].outcome == "quiesced"
            assert records[0].correlation.session_id == created.session_id
            assert not records_for(caplog, "cancel_accepted")
            with pytest.raises(ServiceShuttingDownError):
                await coordinator.create_session("too late", CONFIGURATION)
            await coordinator.shutdown()
            assert len(records_for(caplog, "service_shutdown")) == 1

    run(exercise)


def test_lifespan_shutdown_cancels_blocked_tasks_and_awaits_their_cleanup(caplog) -> None:
    caplog.set_level(logging.INFO, logger="wateragents.analysis_sessions")

    async def exercise() -> None:
        cleaning = asyncio.Event()
        allow_cleanup = asyncio.Event()
        cleaned = asyncio.Event()

        class CleaningWorkflow(GatedWorkflow):
            async def run(self, context: WorkflowContext) -> None:
                try:
                    await super().run(context)
                finally:
                    cleaning.set()
                    await allow_cleanup.wait()
                    cleaned.set()

        workflow = CleaningWorkflow()
        application = create_app(clock=FixedClock(), workflow=workflow, shutdown_grace_period=0)
        lifespan = application.router.lifespan_context(application)
        await lifespan.__aenter__()
        coordinator = application.state.session_coordinator
        try:
            created = await coordinator.create_session("query", CONFIGURATION)
            call = await workflow.calls.get()
            runtime = coordinator._sessions[created.session_id]
            before = await coordinator.get_snapshot(created.session_id)
            exiting = asyncio.create_task(lifespan.__aexit__(None, None, None))
            await cleaning.wait()
            assert call.context.cancellation.is_requested()
            assert not exiting.done()
            assert not runtime.workflow_quiesced.is_set()
            allow_cleanup.set()
            await exiting
            assert cleaned.is_set()
            assert runtime.workflow_task.cancelled()
            assert runtime.workflow_quiesced.is_set()
            assert await coordinator.get_snapshot(created.session_id) is before
            assert len(runtime.events) == 1
            assert not runtime.cancel_requested.is_set()
            records = records_for(caplog, "service_shutdown")
            assert len(records) == 1
            assert records[0].original_status is SessionStatus.RUNNING
            assert records[0].outcome == "task_cancelled"
            assert len(records_for(caplog, "workflow_quiesced")) == 1
        finally:
            allow_cleanup.set()
            await coordinator.shutdown()

    run(exercise)


def test_shutdown_handles_multiple_tasks_including_cancelled_but_not_quiesced(caplog) -> None:
    caplog.set_level(logging.INFO, logger="wateragents.analysis_sessions")

    async def exercise() -> None:
        workflow = GatedWorkflow()
        async with application_runtime(workflow) as coordinator:
            sessions = [await coordinator.create_session(str(index), CONFIGURATION) for index in range(3)]
            calls = [await workflow.calls.get() for _ in sessions]
            calls[0].release.set()
            await wait_quiesced(coordinator, calls[0].context.session.session_id)
            await coordinator.cancel_session(calls[1].context.session.session_id)
            await coordinator.shutdown()
            assert len(records_for(caplog, "service_shutdown")) == 2
            assert {record.original_status for record in records_for(caplog, "service_shutdown")} == {
                SessionStatus.RUNNING, SessionStatus.CANCELLED
            }
            assert all(runtime.workflow_task.done() for runtime in coordinator._sessions.values())
            assert len(coordinator._sessions) == 3

    run(exercise)


def test_shutdown_suppresses_in_flight_return_that_ignores_the_token() -> None:
    async def exercise() -> None:
        workflow = LateReturningWorkflow()
        async with application_runtime(workflow, shutdown_grace_period=1) as coordinator:
            created = await coordinator.create_session("query", CONFIGURATION)
            call = await workflow.calls.get()
            snapshot = await coordinator.get_snapshot(created.session_id)
            closing = asyncio.create_task(coordinator.shutdown())
            await call.context.cancellation.wait()
            call.release.set()
            await closing
            assert await coordinator.get_snapshot(created.session_id) is snapshot
            assert len(coordinator._sessions[created.session_id].events) == 1
            assert not coordinator._sessions[created.session_id].workflow_task.cancelled()

    run(exercise)


def test_lifespan_creates_fresh_runtime_on_each_start_without_import_time_tasks() -> None:
    async def exercise() -> None:
        settings = AppSettings()
        workflow = CooperativeWorkflow()
        application = create_app(settings, clock=FixedClock(), workflow=workflow)
        assert application.state.settings is settings
        assert not hasattr(application.state, "session_coordinator")
        async with application.router.lifespan_context(application):
            first = application.state.session_coordinator
            await first.create_session("query", CONFIGURATION)
            await workflow.started.wait()
        async with application.router.lifespan_context(application):
            second = application.state.session_coordinator
            assert second is not first
            assert second._sessions == {}
            assert len(first._sessions) == 1
            assert all(runtime.workflow_task.done() for runtime in first._sessions.values())
        assert set(application.openapi()["paths"]) == {"/health"}

    run(exercise)


def test_task_factory_failure_does_not_publish_unowned_session() -> None:
    async def exercise() -> None:
        async with application_runtime() as coordinator:
            loop = asyncio.get_running_loop()
            original_factory = loop.get_task_factory()

            def failing_factory(loop, coroutine, **kwargs):
                raise RuntimeError("Task factory unavailable")

            loop.set_task_factory(failing_factory)
            try:
                with pytest.raises(RuntimeError, match="Task factory unavailable"):
                    await coordinator.create_session("query", CONFIGURATION)
                assert coordinator._sessions == {}
            finally:
                loop.set_task_factory(original_factory)

    run(exercise)


def test_done_callback_consumes_unexpected_wrapper_failure_and_marks_quiesced(caplog) -> None:
    caplog.set_level(logging.INFO, logger="wateragents.analysis_sessions")

    async def exercise() -> None:
        observed = []
        unhandled = []
        loop = asyncio.get_running_loop()
        original_factory = loop.get_task_factory()
        original_handler = loop.get_exception_handler()

        class ObservedTask(asyncio.Task):
            def exception(self):
                observed.append(self)
                return super().exception()

        loop.set_task_factory(lambda loop, coroutine, **kwargs: ObservedTask(coroutine, loop=loop, **kwargs))
        loop.set_exception_handler(lambda loop, context: unhandled.append(context))
        identifiers = iter([uuid4()])
        coordinator = SessionCoordinator(
            FixedClock(), workflow=GatedWorkflow(), id_factory=lambda: next(identifiers)
        )
        try:
            created = await coordinator.create_session("query", CONFIGURATION)
            call = await coordinator._workflow.calls.get()
            call.error = RuntimeError(SECRET)
            call.release.set()
            await wait_quiesced(coordinator, created.session_id)
            runtime = coordinator._sessions[created.session_id]
            callback_done = asyncio.Event()
            runtime.workflow_task.add_done_callback(lambda task: callback_done.set())
            await callback_done.wait()
            assert runtime.workflow_task in observed
            assert unhandled == []
            assert len(records_for(caplog, "workflow_task_error")) == 1
            assert len(records_for(caplog, "workflow_quiesced")) == 1
            assert SECRET not in repr([record.__dict__ for record in caplog.records])
        finally:
            await coordinator.shutdown()
            loop.set_task_factory(original_factory)
            loop.set_exception_handler(original_handler)

    run(exercise)


@pytest.mark.parametrize("grace", [-1, float("inf"), float("nan")])
def test_shutdown_grace_period_rejects_unbounded_or_negative_values(grace) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        SessionCoordinator(FixedClock(), shutdown_grace_period=grace)


@pytest.mark.parametrize("create_first", [False, True])
def test_creation_and_shutdown_race_never_leaves_an_unowned_task(create_first) -> None:
    async def exercise() -> None:
        async with application_runtime(CooperativeWorkflow()) as coordinator:
            creating = asyncio.Event()
            closing = asyncio.Event()

            async def create():
                creating.set()
                return await coordinator.create_session("query", CONFIGURATION)

            async def shutdown():
                closing.set()
                await coordinator.shutdown()

            async with coordinator._sessions_lock:
                if create_first:
                    creation = asyncio.create_task(create())
                    await creating.wait()
                shutdown_task = asyncio.create_task(shutdown())
                await closing.wait()
                if not create_first:
                    creation = asyncio.create_task(create())
                    await creating.wait()
            result, _ = await asyncio.gather(creation, shutdown_task, return_exceptions=True)
            if create_first:
                assert result.status is SessionStatus.PENDING
                runtime = coordinator._sessions[result.session_id]
                assert runtime.workflow_task.done()
                assert runtime.workflow_quiesced.is_set()
                assert not runtime.cancel_requested.is_set()
                assert len(runtime.events) == 1
            else:
                assert isinstance(result, ServiceShuttingDownError)
                assert coordinator._sessions == {}

    run(exercise)


def test_task_cancelled_before_first_step_still_reports_quiescence(caplog) -> None:
    caplog.set_level(logging.INFO, logger="wateragents.analysis_sessions")

    async def exercise() -> None:
        workflow = GatedWorkflow()
        async with application_runtime(workflow) as coordinator:
            created = await coordinator.create_session("query", CONFIGURATION)
            runtime = coordinator._sessions[created.session_id]
            runtime.workflow_task.cancel()
            await wait_quiesced(coordinator, created.session_id)
            assert runtime.workflow_task.cancelled()
            assert workflow.calls.empty()
            assert len(records_for(caplog, "workflow_quiesced")) == 1
            assert len(runtime.events) == 1

    run(exercise)

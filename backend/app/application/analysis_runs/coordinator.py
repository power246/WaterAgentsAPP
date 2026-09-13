import asyncio
import logging
from collections.abc import AsyncGenerator, Callable, Mapping
from datetime import datetime
from math import isfinite
from typing import TypeVar
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    AnalysisConfigurationSnapshot,
    AnalysisRunSnapshot,
    CorrelationContext,
    OperationIdentity,
    PublicError,
    RunCancelledEvent,
    RunCancelledPayload,
    RunCompletedEvent,
    RunCompletedPayload,
    RunCreatedEvent,
    RunCreatedPayload,
    RunFailedEvent,
    RunFailedPayload,
    RunLifecycleEvent,
    RunStateMutation,
    RunStatus,
    SessionCancelledEvent,
    SessionCancelledPayload,
    SessionCompletedEvent,
    SessionCompletedPayload,
    SessionCreatedEvent,
    SessionCreatedPayload,
    SessionFailedEvent,
    SessionFailedPayload,
    SessionLifecycleEvent,
    SessionStateMutation,
    SessionSummary,
    TranscriptItemIdentity,
)
from app.application.analysis_runs.runtime import AgentSessionRuntime, RunRuntime
from app.application.analysis_runs.workflow import (
    AnalysisWorkflow,
    CancellationToken,
    DefaultFakeWorkflow,
    WorkflowContext,
    WorkflowStopped,
)
from app.application.correlation import request_id_context
from app.domain.common import NonEmptyString, normalize_utc_datetime
from app.ports.clock import ClockPort

_UUID = TypeAdapter(UUID)
_QUERY = TypeAdapter(NonEmptyString)
_ROLE = TypeAdapter(AgentRole)
_COMID = TypeAdapter(NonEmptyString)
_RUN_TRANSITIONS = {
    RunStatus.PENDING: {
        RunStatus.RUNNING,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    },
    RunStatus.RUNNING: {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.COMPLETED_WITH_PERSISTENCE_ERROR,
    },
}
_SESSION_TRANSITIONS = {
    AgentSessionStatus.PENDING: {
        AgentSessionStatus.RUNNING,
        AgentSessionStatus.FAILED,
        AgentSessionStatus.CANCELLED,
    },
    AgentSessionStatus.RUNNING: {
        AgentSessionStatus.COMPLETED,
        AgentSessionStatus.FAILED,
        AgentSessionStatus.CANCELLED,
    },
}
StatusT = TypeVar("StatusT", RunStatus, AgentSessionStatus)


def _resolve_transition_target(
    current: StatusT,
    requested: StatusT,
    *,
    transitions: Mapping[StatusT, set[StatusT]],
    cancelled: StatusT,
    cancellation_requested: bool,
) -> StatusT | None:
    if current == requested == cancelled:
        return current
    if current not in transitions:
        return None
    target = cancelled if cancellation_requested else requested
    return target if target in transitions[current] else None


class RunNotFoundError(LookupError):
    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id
        super().__init__("The run was not found.")


class AgentSessionNotFoundError(LookupError):
    def __init__(self, run_id: UUID, session_id: UUID) -> None:
        self.run_id = run_id
        self.session_id = session_id
        super().__init__("The Agent session was not found in the run.")


class InvalidRunTransitionError(Exception):
    def __init__(self, run: AnalysisRunSnapshot, target_status: RunStatus) -> None:
        self.run = run
        self.target_status = target_status
        super().__init__("The run cannot enter the requested state.")


class InvalidAgentSessionTransitionError(Exception):
    def __init__(
        self,
        session: SessionSummary,
        target_status: AgentSessionStatus,
    ) -> None:
        self.session = session
        self.target_status = target_status
        super().__init__("The Agent session cannot enter the requested state.")


class ServiceShuttingDownError(Exception):
    pass


class InvalidEventCursorError(ValueError):
    def __init__(self) -> None:
        super().__init__("The event cursor must be a non-negative decimal integer.")


class EventCursorAheadError(Exception):
    def __init__(self, run: AnalysisRunSnapshot, last_sequence: int) -> None:
        self.run = run
        self.last_sequence = last_sequence
        super().__init__("The event cursor is ahead of the latest run event.")


class RunCoordinator:
    def __init__(
        self,
        clock: ClockPort,
        *,
        id_factory: Callable[[], UUID] = uuid4,
        record_id_factory: Callable[[], UUID] = uuid4,
        logger: logging.Logger | None = None,
        workflow: AnalysisWorkflow | None = None,
        shutdown_grace_period: float = 1.0,
        keepalive_interval: float = 15.0,
    ) -> None:
        if not isfinite(shutdown_grace_period) or shutdown_grace_period < 0:
            raise ValueError("shutdown_grace_period must be finite and non-negative")
        if (
            isinstance(keepalive_interval, bool)
            or not isfinite(keepalive_interval)
            or keepalive_interval <= 0
        ):
            raise ValueError("keepalive_interval must be finite and positive")
        self._clock = clock
        self._id_factory = id_factory
        self._record_id_factory = record_id_factory
        self._logger = (
            logger
            if logger is not None
            else logging.getLogger("wateragents.analysis_runs")
        )
        self._runs: dict[UUID, RunRuntime] = {}
        self._runs_lock = asyncio.Lock()
        self._workflow = workflow if workflow is not None else DefaultFakeWorkflow()
        self._shutdown_grace_period = shutdown_grace_period
        self._shutdown_lock = asyncio.Lock()
        self._closing = False
        self._keepalive_interval = keepalive_interval

    async def create_run(
        self,
        query: str,
        configuration: AnalysisConfigurationSnapshot,
    ) -> AnalysisRunSnapshot:
        query = _QUERY.validate_python(query)
        configuration = AnalysisConfigurationSnapshot.model_validate(configuration)
        run_id = self._new_id()
        timestamp = self._clock.now()
        snapshot = AnalysisRunSnapshot(
            run_id=run_id,
            status=RunStatus.PENDING,
            created_at=timestamp,
            updated_at=timestamp,
            configuration=configuration,
        )
        runtime = RunRuntime(snapshot=snapshot, query=query)
        request_id = request_id_context.get()
        self._reserve_transcript_item_locked(
            runtime,
            session_id=None,
            request_id=request_id,
            operation_id=None,
            created_at=timestamp,
        )
        created_item = self._reserve_transcript_item_locked(
            runtime,
            session_id=None,
            request_id=request_id,
            operation_id=None,
            created_at=timestamp,
        )
        event = RunCreatedEvent(
            run_id=run_id,
            occurred_at=snapshot.created_at,
            payload=RunCreatedPayload(
                created_at=snapshot.created_at,
                configuration=configuration,
            ),
        )
        runtime.events.append(event)
        runtime.state_mutations.append(
            RunStateMutation(
                mutation_id=self._new_record_id(),
                previous_status=None,
                snapshot=snapshot,
                transcript_item=created_item,
            )
        )
        async with self._runs_lock:
            if self._closing:
                raise ServiceShuttingDownError()
            if run_id in self._runs:
                raise ValueError("The generated run ID already exists.")
            coroutine = self._run_workflow(runtime)
            try:
                task = asyncio.create_task(coroutine, name=f"analysis-run-{run_id}")
            except BaseException:
                coroutine.close()
                raise
            runtime.workflow_task = task
            task.add_done_callback(
                lambda completed: self._workflow_done(runtime, completed)
            )
            self._runs[run_id] = runtime
        self._log_run_transition(None, snapshot, event)
        runtime.workflow_start.set()
        return snapshot

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            async with self._runs_lock:
                self._closing = True
                active = [
                    (runtime, runtime.snapshot.status, runtime.workflow_task)
                    for runtime in self._runs.values()
                    if runtime.workflow_task is not None
                    and not runtime.workflow_task.done()
                ]
                for runtime, _, _ in active:
                    runtime.stop_requested.set()
                    for session in runtime.agent_sessions.values():
                        session.stop_requested.set()
            if not active:
                return
            _, pending = await asyncio.wait(
                [task for _, _, task in active],
                timeout=self._shutdown_grace_period,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(
                *(task for _, _, task in active),
                return_exceptions=True,
            )
            for runtime, original_status, task in active:
                self._log_activity(
                    "service_shutdown",
                    runtime,
                    original_status=original_status,
                    outcome="task_cancelled" if task.cancelled() else "quiesced",
                )

    async def _run_workflow(self, runtime: RunRuntime) -> None:
        token = CancellationToken(runtime.stop_requested)
        try:
            await runtime.workflow_start.wait()
            token.checkpoint()
            running = await self.mark_running(runtime.snapshot.run_id)
            await self._workflow.run(WorkflowContext(running, runtime.query, token))
            token.checkpoint()
            await self.complete_run(running.run_id)
        except (WorkflowStopped, ServiceShuttingDownError) as error:
            if token.is_requested():
                self._log_activity("workflow_output_suppressed", runtime)
            else:
                await self._workflow_failed(runtime, error)
        except InvalidRunTransitionError as error:
            if error.run.status not in _RUN_TRANSITIONS:
                self._log_activity("workflow_output_suppressed", runtime)
            else:
                await self._workflow_failed(runtime, error)
        except asyncio.CancelledError as error:
            if not token.is_requested():
                await self._workflow_failed(runtime, error)
            raise
        except Exception as error:
            await self._workflow_failed(runtime, error)
        finally:
            self._mark_quiesced(runtime)

    async def _workflow_failed(
        self,
        runtime: RunRuntime,
        exception: BaseException,
    ) -> None:
        if runtime.stop_requested.is_set() or runtime.snapshot.status not in _RUN_TRANSITIONS:
            self._log_activity(
                "workflow_output_suppressed",
                runtime,
                exception=exception,
            )
            return
        error = PublicError(
            error_id=self._new_id(),
            code="workflow_failed",
            message="The analysis workflow failed.",
            failed_stage="analysis_workflow",
            retryable=False,
        )
        try:
            snapshot = await self.fail_run(runtime.snapshot.run_id, error)
        except (InvalidRunTransitionError, ServiceShuttingDownError):
            self._log_activity(
                "workflow_output_suppressed",
                runtime,
                exception=exception,
            )
        else:
            if snapshot.status is RunStatus.FAILED:
                self._log_activity(
                    "run_failed",
                    runtime,
                    timestamp=snapshot.updated_at,
                    exception=exception,
                )
            else:
                self._log_activity(
                    "workflow_output_suppressed",
                    runtime,
                    exception=exception,
                )

    def _workflow_done(
        self,
        runtime: RunRuntime,
        task: asyncio.Task[None],
    ) -> None:
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self._log_activity("workflow_task_error", runtime, exception=error)
        self._mark_quiesced(runtime)

    def _mark_quiesced(self, runtime: RunRuntime) -> None:
        if not runtime.workflow_quiesced.is_set():
            runtime.workflow_quiesced.set()
            self._log_activity("workflow_quiesced", runtime)

    def _log_activity(
        self,
        event_name: str,
        runtime: RunRuntime,
        *,
        timestamp: datetime | None = None,
        exception: BaseException | None = None,
        **fields: object,
    ) -> None:
        try:
            self._logger.log(
                logging.ERROR if exception is not None else logging.INFO,
                "Analysis workflow lifecycle.",
                exc_info=(type(exception), exception, exception.__traceback__)
                if exception is not None
                else None,
                extra={
                    "event_name": event_name,
                    "timestamp": (
                        timestamp
                        if timestamp is not None
                        else normalize_utc_datetime(self._clock.now())
                    ),
                    "correlation": CorrelationContext(
                        request_id=request_id_context.get(),
                        run_id=runtime.snapshot.run_id,
                        error_id=(
                            runtime.last_error.error_id
                            if runtime.last_error
                            else None
                        ),
                    ),
                    "exception_type": (
                        type(exception).__name__ if exception is not None else None
                    ),
                    "redaction_values": (
                        (runtime.query,) if exception is not None else ()
                    ),
                    **fields,
                },
            )
        except Exception:
            pass

    async def get_snapshot(self, run_id: UUID | str) -> AnalysisRunSnapshot:
        runtime = await self._lookup(run_id)
        async with runtime.condition:
            return runtime.snapshot

    async def list_sessions(self, run_id: UUID | str) -> tuple[SessionSummary, ...]:
        runtime = await self._lookup(run_id)
        async with runtime.condition:
            return tuple(
                session.summary
                for session in sorted(
                    runtime.agent_sessions.values(),
                    key=lambda item: (
                        item.summary.created_at,
                        item.summary.session_id,
                    ),
                )
            )

    async def get_session(
        self,
        run_id: UUID | str,
        session_id: UUID | str,
    ) -> SessionSummary:
        runtime = await self._lookup(run_id)
        key = _UUID.validate_python(session_id)
        async with runtime.condition:
            return self._lookup_agent_session_locked(runtime, key).summary

    async def prepare_agent_session(
        self,
        run_id: UUID | str,
        *,
        agent_role: AgentRole | str,
        target_comid: str | None = None,
    ) -> SessionSummary:
        runtime = await self._lookup(run_id)
        role = _ROLE.validate_python(agent_role)
        comid = _COMID.validate_python(target_comid) if target_comid is not None else None
        if role is AgentRole.QUERY_PARSER and comid is not None:
            raise ValueError("query_parser sessions must not have target_comid")
        if role is AgentRole.WATERBODY and comid is None:
            raise ValueError("waterbody sessions require target_comid")
        async with runtime.condition:
            if self._closing:
                raise ServiceShuttingDownError()
            if runtime.snapshot.status not in _RUN_TRANSITIONS:
                raise InvalidRunTransitionError(runtime.snapshot, RunStatus.RUNNING)
            existing_id = (
                runtime.query_parser_session_id
                if role is AgentRole.QUERY_PARSER
                else runtime.waterbody_session_ids.get(comid)
            )
            if existing_id is not None:
                return runtime.agent_sessions[existing_id].summary
            timestamp = self._clock.now()
            summary = SessionSummary(
                session_id=self._new_record_id(),
                agent_role=role,
                target_comid=comid,
                status=AgentSessionStatus.PENDING,
                created_at=timestamp,
                updated_at=timestamp,
            )
            mutation_id = self._new_record_id()
            event = SessionCreatedEvent(
                run_id=runtime.snapshot.run_id,
                session_id=summary.session_id,
                occurred_at=timestamp,
                payload=SessionCreatedPayload(created_at=timestamp),
            )
            item = self._reserve_transcript_item_locked(
                runtime,
                session_id=summary.session_id,
                request_id=request_id_context.get(),
                operation_id=None,
                created_at=timestamp,
            )
            agent_runtime = AgentSessionRuntime(summary=summary, events=[event])
            runtime.agent_sessions[summary.session_id] = agent_runtime
            if role is AgentRole.QUERY_PARSER:
                runtime.query_parser_session_id = summary.session_id
            else:
                runtime.waterbody_session_ids[comid] = summary.session_id
            runtime.state_mutations.append(
                SessionStateMutation(
                    mutation_id=mutation_id,
                    run_id=runtime.snapshot.run_id,
                    previous_status=None,
                    summary=summary,
                    transcript_item=item,
                )
            )
            runtime.condition.notify_all()
        self._log_agent_session_transition(runtime, None, summary, event)
        return summary

    async def mark_agent_session_running(
        self,
        run_id: UUID | str,
        session_id: UUID | str,
    ) -> SessionSummary:
        return await self._transition_agent_session(
            run_id,
            session_id,
            AgentSessionStatus.RUNNING,
        )

    async def complete_agent_session(
        self,
        run_id: UUID | str,
        session_id: UUID | str,
    ) -> SessionSummary:
        return await self._transition_agent_session(
            run_id,
            session_id,
            AgentSessionStatus.COMPLETED,
        )

    async def fail_agent_session(
        self,
        run_id: UUID | str,
        session_id: UUID | str,
        error: PublicError,
    ) -> SessionSummary:
        return await self._transition_agent_session(
            run_id,
            session_id,
            AgentSessionStatus.FAILED,
            PublicError.model_validate(error),
        )

    async def cancel_agent_session(
        self,
        run_id: UUID | str,
        session_id: UUID | str,
    ) -> SessionSummary:
        runtime = await self._lookup(run_id)
        key = _UUID.validate_python(session_id)
        session_runtime = runtime.agent_sessions.get(key)
        if session_runtime is None:
            raise AgentSessionNotFoundError(runtime.snapshot.run_id, key)
        if session_runtime.summary.status is AgentSessionStatus.CANCELLED:
            return session_runtime.summary
        session_runtime.stop_requested.set()
        try:
            return await self._transition_agent_session(
                runtime.snapshot.run_id,
                key,
                AgentSessionStatus.CANCELLED,
            )
        except BaseException:
            if session_runtime.summary.status is not AgentSessionStatus.CANCELLED:
                session_runtime.stop_requested.clear()
            raise

    async def create_operation(
        self,
        run_id: UUID | str,
        session_id: UUID | str,
    ) -> OperationIdentity:
        runtime = await self._lookup(run_id)
        key = _UUID.validate_python(session_id)
        async with runtime.condition:
            self._lookup_agent_session_locked(runtime, key)
            return OperationIdentity(
                operation_id=self._new_record_id(),
                run_id=runtime.snapshot.run_id,
                session_id=key,
            )

    async def reserve_transcript_item(
        self,
        run_id: UUID | str,
        *,
        session_id: UUID | str | None = None,
        request_id: UUID | str | None = None,
        operation_id: UUID | str | None = None,
    ) -> TranscriptItemIdentity:
        runtime = await self._lookup(run_id)
        session_key = (
            _UUID.validate_python(session_id) if session_id is not None else None
        )
        request_key = (
            _UUID.validate_python(request_id) if request_id is not None else None
        )
        operation_key = (
            _UUID.validate_python(operation_id) if operation_id is not None else None
        )
        async with runtime.condition:
            if session_key is not None:
                self._lookup_agent_session_locked(runtime, session_key)
            item = self._reserve_transcript_item_locked(
                runtime,
                session_id=session_key,
                request_id=request_key,
                operation_id=operation_key,
                created_at=self._clock.now(),
            )
            runtime.condition.notify_all()
            return item

    async def list_transcript_items(
        self,
        run_id: UUID | str,
    ) -> tuple[TranscriptItemIdentity, ...]:
        runtime = await self._lookup(run_id)
        async with runtime.condition:
            return tuple(runtime.transcript_items)

    async def list_session_transcript_items(
        self,
        run_id: UUID | str,
        session_id: UUID | str,
    ) -> tuple[TranscriptItemIdentity, ...]:
        runtime = await self._lookup(run_id)
        key = _UUID.validate_python(session_id)
        async with runtime.condition:
            self._lookup_agent_session_locked(runtime, key)
            return tuple(
                item
                for item in runtime.transcript_items
                if item.session_id == key
            )

    async def list_state_mutations(
        self,
        run_id: UUID | str,
    ) -> tuple[RunStateMutation | SessionStateMutation, ...]:
        runtime = await self._lookup(run_id)
        async with runtime.condition:
            return tuple(runtime.state_mutations)

    async def mark_running(self, run_id: UUID | str) -> AnalysisRunSnapshot:
        return await self._transition_run(run_id, RunStatus.RUNNING)

    async def complete_run(self, run_id: UUID | str) -> AnalysisRunSnapshot:
        return await self._transition_run(run_id, RunStatus.COMPLETED)

    async def complete_run_with_persistence_error(
        self,
        run_id: UUID | str,
        warning: PublicError,
    ) -> AnalysisRunSnapshot:
        return await self._transition_run(
            run_id,
            RunStatus.COMPLETED_WITH_PERSISTENCE_ERROR,
            PublicError.model_validate(warning),
        )

    async def fail_run(
        self,
        run_id: UUID | str,
        error: PublicError,
    ) -> AnalysisRunSnapshot:
        return await self._transition_run(
            run_id,
            RunStatus.FAILED,
            PublicError.model_validate(error),
        )

    async def cancel_run(self, run_id: UUID | str) -> AnalysisRunSnapshot:
        runtime = await self._lookup(run_id)
        runtime.cancel_requested.set()
        runtime.stop_requested.set()
        try:
            return await self._transition_run(
                runtime.snapshot.run_id,
                RunStatus.CANCELLED,
            )
        except BaseException:
            if runtime.snapshot.status is not RunStatus.CANCELLED:
                runtime.cancel_requested.clear()
                runtime.stop_requested.clear()
            raise

    async def subscribe(
        self,
        run_id: UUID | str,
        *,
        after_sequence: int | str | None = None,
    ) -> AsyncGenerator[RunLifecycleEvent | None, None] | None:
        if after_sequence is None:
            cursor = 0
        elif type(after_sequence) is int and after_sequence >= 0:
            cursor = after_sequence
        elif (
            isinstance(after_sequence, str)
            and after_sequence.isascii()
            and after_sequence.isdecimal()
        ):
            cursor = after_sequence.lstrip("0") or "0"
        else:
            raise InvalidEventCursorError()
        runtime = await self._lookup(run_id)
        async with runtime.condition:
            last_sequence = len(runtime.events)
            if isinstance(cursor, str):
                limit = str(last_sequence)
                if (len(cursor), cursor) > (len(limit), limit):
                    raise EventCursorAheadError(runtime.snapshot, last_sequence)
                cursor = int(cursor)
            if cursor > last_sequence:
                raise EventCursorAheadError(runtime.snapshot, last_sequence)
            if cursor == last_sequence and runtime.snapshot.status not in _RUN_TRANSITIONS:
                return None
        return self._events(runtime, cursor)

    async def _lookup(self, run_id: UUID | str) -> RunRuntime:
        key = _UUID.validate_python(run_id)
        async with self._runs_lock:
            runtime = self._runs.get(key)
        if runtime is None:
            raise RunNotFoundError(key)
        return runtime

    async def _events(
        self,
        runtime: RunRuntime,
        cursor: int,
    ) -> AsyncGenerator[RunLifecycleEvent | None, None]:
        while True:
            async with runtime.condition:
                try:
                    async with asyncio.timeout(self._keepalive_interval):
                        await runtime.condition.wait_for(
                            lambda: len(runtime.events) > cursor
                            or runtime.snapshot.status not in _RUN_TRANSITIONS
                        )
                except TimeoutError:
                    pass
                batch = tuple(runtime.events[cursor:])
                terminal = runtime.snapshot.status not in _RUN_TRANSITIONS
            for event in batch:
                cursor = event.sequence
                yield event
            if terminal:
                return
            if not batch:
                yield None

    async def _transition_run(
        self,
        run_id: UUID | str,
        target: RunStatus,
        error: PublicError | None = None,
    ) -> AnalysisRunSnapshot:
        runtime = await self._lookup(run_id)
        session_transitions: list[
            tuple[AgentSessionStatus, SessionSummary, SessionLifecycleEvent]
        ] = []
        async with runtime.condition:
            current = runtime.snapshot
            resolved = _resolve_transition_target(
                current.status,
                target,
                transitions=_RUN_TRANSITIONS,
                cancelled=RunStatus.CANCELLED,
                cancellation_requested=runtime.cancel_requested.is_set(),
            )
            if resolved == current.status == RunStatus.CANCELLED:
                return current
            if self._closing and resolved is not RunStatus.CANCELLED:
                raise ServiceShuttingDownError()
            if resolved is None:
                raise InvalidRunTransitionError(current, target)
            timestamp = self._clock.now()
            snapshot = AnalysisRunSnapshot(
                run_id=current.run_id,
                status=resolved,
                created_at=current.created_at,
                updated_at=timestamp,
                configuration=current.configuration,
            )
            event = self._run_terminal_event(
                snapshot,
                len(runtime.events) + 1,
                error,
            )
            mutation_id = self._new_record_id()
            item = None
            if event is not None:
                item = self._reserve_transcript_item_locked(
                    runtime,
                    session_id=None,
                    request_id=request_id_context.get(),
                    operation_id=None,
                    created_at=timestamp,
                )
            runtime.snapshot = snapshot
            if event is not None:
                runtime.events.append(event)
                runtime.last_error = (
                    error
                    if resolved
                    in {RunStatus.FAILED, RunStatus.COMPLETED_WITH_PERSISTENCE_ERROR}
                    else None
                )
            runtime.state_mutations.append(
                RunStateMutation(
                    mutation_id=mutation_id,
                    previous_status=current.status,
                    snapshot=snapshot,
                    transcript_item=item,
                )
            )
            if resolved is RunStatus.CANCELLED:
                runtime.cancel_requested.set()
                runtime.stop_requested.set()
                for session_runtime in runtime.agent_sessions.values():
                    session_runtime.stop_requested.set()
                    if session_runtime.summary.status in _SESSION_TRANSITIONS:
                        session_transitions.append(
                            self._transition_agent_session_locked(
                                runtime,
                                session_runtime,
                                AgentSessionStatus.CANCELLED,
                                None,
                                timestamp,
                            )
                        )
            runtime.condition.notify_all()
        self._log_run_transition(current.status, snapshot, event)
        for previous, summary, session_event in session_transitions:
            self._log_agent_session_transition(
                runtime,
                previous,
                summary,
                session_event,
            )
        if resolved is RunStatus.CANCELLED:
            self._log_activity("cancel_accepted", runtime, timestamp=snapshot.updated_at)
        return snapshot

    async def _transition_agent_session(
        self,
        run_id: UUID | str,
        session_id: UUID | str,
        target: AgentSessionStatus,
        error: PublicError | None = None,
    ) -> SessionSummary:
        runtime = await self._lookup(run_id)
        key = _UUID.validate_python(session_id)
        async with runtime.condition:
            session_runtime = self._lookup_agent_session_locked(runtime, key)
            if (
                session_runtime.summary.status is AgentSessionStatus.CANCELLED
                and target is AgentSessionStatus.CANCELLED
            ):
                return session_runtime.summary
            if (
                runtime.snapshot.status not in _RUN_TRANSITIONS
                and target is not AgentSessionStatus.CANCELLED
            ):
                raise InvalidAgentSessionTransitionError(
                    session_runtime.summary,
                    target,
                )
            previous, summary, event = self._transition_agent_session_locked(
                runtime,
                session_runtime,
                target,
                error,
                None,
            )
            runtime.condition.notify_all()
        self._log_agent_session_transition(runtime, previous, summary, event)
        return summary

    def _transition_agent_session_locked(
        self,
        runtime: RunRuntime,
        session_runtime: AgentSessionRuntime,
        target: AgentSessionStatus,
        error: PublicError | None,
        timestamp: datetime | None,
    ) -> tuple[
        AgentSessionStatus,
        SessionSummary,
        SessionLifecycleEvent | None,
    ]:
        current = session_runtime.summary
        resolved = _resolve_transition_target(
            current.status,
            target,
            transitions=_SESSION_TRANSITIONS,
            cancelled=AgentSessionStatus.CANCELLED,
            cancellation_requested=(
                runtime.cancel_requested.is_set()
                or session_runtime.stop_requested.is_set()
            ),
        )
        if resolved is None:
            raise InvalidAgentSessionTransitionError(current, target)
        if timestamp is None:
            timestamp = self._clock.now()
        summary = SessionSummary(
            session_id=current.session_id,
            agent_role=current.agent_role,
            target_comid=current.target_comid,
            status=resolved,
            created_at=current.created_at,
            updated_at=timestamp,
        )
        event = self._agent_session_terminal_event(
            runtime.snapshot.run_id,
            summary,
            len(session_runtime.events) + 1,
            error,
        )
        mutation_id = self._new_record_id()
        item = None
        if event is not None:
            item = self._reserve_transcript_item_locked(
                runtime,
                session_id=summary.session_id,
                request_id=request_id_context.get(),
                operation_id=None,
                created_at=timestamp,
            )
            session_runtime.events.append(event)
            session_runtime.last_error = (
                error if resolved is AgentSessionStatus.FAILED else None
            )
        if resolved is AgentSessionStatus.RUNNING:
            if session_runtime.execution_claimed:
                raise InvalidAgentSessionTransitionError(current, target)
            session_runtime.execution_claimed = True
        if resolved is AgentSessionStatus.CANCELLED:
            session_runtime.stop_requested.set()
        session_runtime.summary = summary
        runtime.state_mutations.append(
            SessionStateMutation(
                mutation_id=mutation_id,
                run_id=runtime.snapshot.run_id,
                previous_status=current.status,
                summary=summary,
                transcript_item=item,
            )
        )
        return current.status, summary, event

    def _run_terminal_event(
        self,
        snapshot: AnalysisRunSnapshot,
        sequence: int,
        error: PublicError | None,
    ) -> RunLifecycleEvent | None:
        if snapshot.status is RunStatus.RUNNING:
            return None
        if snapshot.status in {
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_PERSISTENCE_ERROR,
        }:
            return RunCompletedEvent(
                run_id=snapshot.run_id,
                sequence=sequence,
                occurred_at=snapshot.updated_at,
                payload=RunCompletedPayload(
                    status=snapshot.status,
                    completed_at=snapshot.updated_at,
                    warning=error,
                ),
            )
        if snapshot.status is RunStatus.FAILED:
            if error is None:
                raise ValueError("A failed transition requires a public error.")
            return RunFailedEvent(
                run_id=snapshot.run_id,
                sequence=sequence,
                occurred_at=snapshot.updated_at,
                payload=RunFailedPayload(
                    failed_at=snapshot.updated_at,
                    error=error,
                ),
            )
        if snapshot.status is RunStatus.CANCELLED:
            return RunCancelledEvent(
                run_id=snapshot.run_id,
                sequence=sequence,
                occurred_at=snapshot.updated_at,
                payload=RunCancelledPayload(cancelled_at=snapshot.updated_at),
            )
        raise ValueError("Unsupported run transition event status.")

    def _agent_session_terminal_event(
        self,
        run_id: UUID,
        summary: SessionSummary,
        sequence: int,
        error: PublicError | None,
    ) -> SessionLifecycleEvent | None:
        if summary.status is AgentSessionStatus.RUNNING:
            return None
        if summary.status is AgentSessionStatus.COMPLETED:
            return SessionCompletedEvent(
                run_id=run_id,
                session_id=summary.session_id,
                sequence=sequence,
                occurred_at=summary.updated_at,
                payload=SessionCompletedPayload(completed_at=summary.updated_at),
            )
        if summary.status is AgentSessionStatus.FAILED:
            if error is None:
                raise ValueError("A failed transition requires a public error.")
            return SessionFailedEvent(
                run_id=run_id,
                session_id=summary.session_id,
                sequence=sequence,
                occurred_at=summary.updated_at,
                payload=SessionFailedPayload(
                    failed_at=summary.updated_at,
                    error=error,
                ),
            )
        if summary.status is AgentSessionStatus.CANCELLED:
            return SessionCancelledEvent(
                run_id=run_id,
                session_id=summary.session_id,
                sequence=sequence,
                occurred_at=summary.updated_at,
                payload=SessionCancelledPayload(cancelled_at=summary.updated_at),
            )
        raise ValueError("Unsupported Agent session transition event status.")

    def _reserve_transcript_item_locked(
        self,
        runtime: RunRuntime,
        *,
        session_id: UUID | None,
        request_id: UUID | None,
        operation_id: UUID | None,
        created_at: datetime,
    ) -> TranscriptItemIdentity:
        item = TranscriptItemIdentity(
            item_id=self._new_record_id(),
            run_id=runtime.snapshot.run_id,
            session_id=session_id,
            transcript_sequence=runtime.next_transcript_sequence,
            created_at=created_at,
            request_id=request_id,
            operation_id=operation_id,
        )
        runtime.next_transcript_sequence += 1
        runtime.transcript_items.append(item)
        return item

    @staticmethod
    def _lookup_agent_session_locked(
        runtime: RunRuntime,
        session_id: UUID,
    ) -> AgentSessionRuntime:
        session = runtime.agent_sessions.get(session_id)
        if session is None:
            raise AgentSessionNotFoundError(runtime.snapshot.run_id, session_id)
        return session

    def _new_id(self) -> UUID:
        return _UUID.validate_python(self._id_factory())

    def _new_record_id(self) -> UUID:
        return _UUID.validate_python(self._record_id_factory())

    def _log_run_transition(
        self,
        previous: RunStatus | None,
        snapshot: AnalysisRunSnapshot,
        event: RunLifecycleEvent | None,
    ) -> None:
        try:
            self._logger.info(
                "Run state committed.",
                extra={
                    "event_name": (
                        "lifecycle_event_committed"
                        if event
                        else "run_status_changed"
                    ),
                    "timestamp": snapshot.updated_at,
                    "correlation": CorrelationContext(
                        request_id=request_id_context.get(),
                        run_id=snapshot.run_id,
                        event_sequence=event.sequence if event else None,
                        error_id=(
                            event.payload.error.error_id
                            if isinstance(event, RunFailedEvent)
                            else None
                        ),
                    ),
                    "from_status": previous,
                    "to_status": snapshot.status,
                    "lifecycle_event": event,
                },
            )
        except Exception:
            pass

    def _log_agent_session_transition(
        self,
        runtime: RunRuntime,
        previous: AgentSessionStatus | None,
        summary: SessionSummary,
        event: SessionLifecycleEvent | None,
    ) -> None:
        try:
            self._logger.info(
                "Agent session state committed.",
                extra={
                    "event_name": (
                        "lifecycle_event_committed"
                        if event
                        else "session_status_changed"
                    ),
                    "timestamp": summary.updated_at,
                    "correlation": CorrelationContext(
                        request_id=request_id_context.get(),
                        run_id=runtime.snapshot.run_id,
                        session_id=summary.session_id,
                        event_sequence=event.sequence if event else None,
                        error_id=(
                            event.payload.error.error_id
                            if isinstance(event, SessionFailedEvent)
                            else None
                        ),
                    ),
                    "from_status": previous,
                    "to_status": summary.status,
                    "lifecycle_event": event,
                },
            )
        except Exception:
            pass

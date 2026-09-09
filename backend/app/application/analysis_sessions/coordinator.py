import asyncio
import logging
from collections.abc import AsyncGenerator, Callable
from datetime import datetime
from math import isfinite
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from app.application.analysis_sessions.models import (
    AnalysisConfigurationSnapshot,
    AnalysisSessionSnapshot,
    CorrelationContext,
    LifecycleEvent,
    PublicError,
    SessionCancelledEvent,
    SessionCancelledPayload,
    SessionCompletedEvent,
    SessionCompletedPayload,
    SessionCreatedEvent,
    SessionCreatedPayload,
    SessionFailedEvent,
    SessionFailedPayload,
    SessionStatus,
)
from app.application.analysis_sessions.runtime import SessionRuntime
from app.application.analysis_sessions.workflow import (
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
_TRANSITIONS = {
    SessionStatus.PENDING: {SessionStatus.RUNNING, SessionStatus.CANCELLED},
    SessionStatus.RUNNING: {
        SessionStatus.COMPLETED,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
    },
}


class SessionNotFoundError(LookupError):
    def __init__(self, session_id: UUID) -> None:
        self.session_id = session_id
        super().__init__("The session was not found.")


class InvalidSessionTransitionError(Exception):
    def __init__(
        self, session: AnalysisSessionSnapshot, target_status: SessionStatus
    ) -> None:
        self.session = session
        self.target_status = target_status
        super().__init__("The session cannot enter the requested state.")


class ServiceShuttingDownError(Exception):
    pass


class InvalidEventCursorError(ValueError):
    def __init__(self) -> None:
        super().__init__("The event cursor must be a non-negative decimal integer.")


class EventCursorAheadError(Exception):
    def __init__(self, session: AnalysisSessionSnapshot, last_sequence: int) -> None:
        self.session = session
        self.last_sequence = last_sequence
        super().__init__("The event cursor is ahead of the latest session event.")


class SessionCoordinator:
    def __init__(
        self,
        clock: ClockPort,
        *,
        id_factory: Callable[[], UUID] = uuid4,
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
        self._logger = (
            logger if logger is not None else logging.getLogger("wateragents.analysis_sessions")
        )
        self._sessions: dict[UUID, SessionRuntime] = {}
        self._sessions_lock = asyncio.Lock()
        self._workflow = workflow if workflow is not None else DefaultFakeWorkflow()
        self._shutdown_grace_period = shutdown_grace_period
        self._shutdown_lock = asyncio.Lock()
        self._closing = False
        self._keepalive_interval = keepalive_interval

    async def create_session(
        self, query: str, configuration: AnalysisConfigurationSnapshot
    ) -> AnalysisSessionSnapshot:
        query = _QUERY.validate_python(query)
        configuration = AnalysisConfigurationSnapshot.model_validate(configuration)
        session_id = _UUID.validate_python(self._id_factory())
        timestamp = self._clock.now()
        snapshot = AnalysisSessionSnapshot(
            session_id=session_id,
            status=SessionStatus.PENDING,
            created_at=timestamp,
            updated_at=timestamp,
            configuration=configuration,
        )
        event = SessionCreatedEvent(
            session_id=session_id,
            occurred_at=snapshot.created_at,
            payload=SessionCreatedPayload(
                created_at=snapshot.created_at, configuration=configuration
            ),
        )
        runtime = SessionRuntime(snapshot=snapshot, query=query, events=[event])
        async with self._sessions_lock:
            if self._closing:
                raise ServiceShuttingDownError()
            if session_id in self._sessions:
                raise ValueError("The generated session ID already exists.")
            coroutine = self._run_workflow(runtime)
            try:
                task = asyncio.create_task(coroutine, name=f"analysis-session-{session_id}")
            except BaseException:
                coroutine.close()
                raise
            runtime.workflow_task = task
            task.add_done_callback(lambda completed: self._workflow_done(runtime, completed))
            self._sessions[session_id] = runtime
        self._log_transition(None, snapshot, event)
        runtime.workflow_start.set()
        return snapshot

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            async with self._sessions_lock:
                self._closing = True
                active = [
                    (runtime, runtime.snapshot.status, runtime.workflow_task)
                    for runtime in self._sessions.values()
                    if runtime.workflow_task is not None and not runtime.workflow_task.done()
                ]
                for runtime, _, _ in active:
                    runtime.stop_requested.set()
            if not active:
                return
            _, pending = await asyncio.wait(
                [task for _, _, task in active], timeout=self._shutdown_grace_period
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*(task for _, _, task in active), return_exceptions=True)
            for runtime, original_status, task in active:
                self._log_activity(
                    "service_shutdown",
                    runtime,
                    original_status=original_status,
                    outcome="task_cancelled" if task.cancelled() else "quiesced",
                )

    async def _run_workflow(self, runtime: SessionRuntime) -> None:
        token = CancellationToken(runtime.stop_requested)
        try:
            # Registration must finish before even an eager task factory runs the workflow.
            await runtime.workflow_start.wait()
            token.checkpoint()
            running = await self.mark_running(runtime.snapshot.session_id)
            await self._workflow.run(WorkflowContext(running, runtime.query, token))
            token.checkpoint()
            await self.complete_session(running.session_id)
        except (WorkflowStopped, ServiceShuttingDownError) as error:
            if token.is_requested():
                self._log_activity("workflow_output_suppressed", runtime)
            else:
                await self._workflow_failed(runtime, error)
        except InvalidSessionTransitionError as error:
            if error.session.status not in _TRANSITIONS:
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

    async def _workflow_failed(self, runtime: SessionRuntime, exception: BaseException) -> None:
        if runtime.stop_requested.is_set() or runtime.snapshot.status not in _TRANSITIONS:
            self._log_activity(
                "workflow_output_suppressed", runtime, exception=exception
            )
            return
        error = PublicError(
            error_id=_UUID.validate_python(self._id_factory()),
            code="workflow_failed",
            message="The analysis workflow failed.",
            failed_stage="analysis_workflow",
            retryable=False,
        )
        try:
            snapshot = await self.fail_session(runtime.snapshot.session_id, error)
        except (InvalidSessionTransitionError, ServiceShuttingDownError):
            self._log_activity("workflow_output_suppressed", runtime, exception=exception)
        else:
            self._log_activity(
                "session_failed",
                runtime,
                timestamp=snapshot.updated_at,
                exception=exception,
            )

    def _workflow_done(self, runtime: SessionRuntime, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            error = task.exception()
            if error is not None:
                self._log_activity(
                    "workflow_task_error", runtime, exception=error
                )
        self._mark_quiesced(runtime)

    def _mark_quiesced(self, runtime: SessionRuntime) -> None:
        if not runtime.workflow_quiesced.is_set():
            runtime.workflow_quiesced.set()
            self._log_activity("workflow_quiesced", runtime)

    def _log_activity(
        self,
        event_name: str,
        runtime: SessionRuntime,
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
                if exception is not None else None,
                extra={
                    "event_name": event_name,
                    "timestamp": (
                        timestamp if timestamp is not None
                        else normalize_utc_datetime(self._clock.now())
                    ),
                    "correlation": CorrelationContext(
                        request_id=request_id_context.get(),
                        session_id=runtime.snapshot.session_id,
                        error_id=runtime.last_error.error_id if runtime.last_error else None,
                    ),
                    "exception_type": type(exception).__name__ if exception is not None else None,
                    "redaction_values": (runtime.query,) if exception is not None else (),
                    **fields,
                },
            )
        except Exception:
            pass

    async def get_snapshot(self, session_id: UUID | str) -> AnalysisSessionSnapshot:
        runtime = await self._lookup(session_id)
        async with runtime.condition:
            return runtime.snapshot

    async def mark_running(self, session_id: UUID | str) -> AnalysisSessionSnapshot:
        return await self._transition(session_id, SessionStatus.RUNNING)

    async def complete_session(self, session_id: UUID | str) -> AnalysisSessionSnapshot:
        return await self._transition(session_id, SessionStatus.COMPLETED)

    async def fail_session(
        self, session_id: UUID | str, error: PublicError
    ) -> AnalysisSessionSnapshot:
        return await self._transition(
            session_id, SessionStatus.FAILED, PublicError.model_validate(error)
        )

    async def cancel_session(self, session_id: UUID | str) -> AnalysisSessionSnapshot:
        return await self._transition(session_id, SessionStatus.CANCELLED)

    async def subscribe(
        self, session_id: UUID | str, *, after_sequence: int | str | None = None
    ) -> AsyncGenerator[LifecycleEvent | None, None] | None:
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
        runtime = await self._lookup(session_id)
        async with runtime.condition:
            last_sequence = len(runtime.events)
            if isinstance(cursor, str):
                limit = str(last_sequence)
                if (len(cursor), cursor) > (len(limit), limit):
                    raise EventCursorAheadError(runtime.snapshot, last_sequence)
                cursor = int(cursor)
            if cursor > last_sequence:
                raise EventCursorAheadError(runtime.snapshot, last_sequence)
            if cursor == last_sequence and runtime.snapshot.status not in _TRANSITIONS:
                return None
        return self._events(runtime, cursor)

    async def _lookup(self, session_id: UUID | str) -> SessionRuntime:
        key = _UUID.validate_python(session_id)
        async with self._sessions_lock:
            runtime = self._sessions.get(key)
        if runtime is None:
            raise SessionNotFoundError(key)
        return runtime

    async def _events(
        self, runtime: SessionRuntime, cursor: int
    ) -> AsyncGenerator[LifecycleEvent | None, None]:
        while True:
            async with runtime.condition:
                try:
                    async with asyncio.timeout(self._keepalive_interval):
                        await runtime.condition.wait_for(
                            lambda: len(runtime.events) > cursor
                            or runtime.snapshot.status not in _TRANSITIONS
                        )
                except TimeoutError:
                    pass
                # Recheck under the lock so a committed event wins over a simultaneous timeout.
                batch = tuple(runtime.events[cursor:])
                terminal = runtime.snapshot.status not in _TRANSITIONS
            for event in batch:
                cursor = event.sequence
                yield event
            if terminal:
                return
            if not batch:
                yield None

    async def _transition(
        self,
        session_id: UUID | str,
        target: SessionStatus,
        error: PublicError | None = None,
    ) -> AnalysisSessionSnapshot:
        runtime = await self._lookup(session_id)
        async with runtime.condition:
            current = runtime.snapshot
            if current.status == target == SessionStatus.CANCELLED:
                return current
            if self._closing and target != SessionStatus.CANCELLED:
                raise ServiceShuttingDownError()
            if target not in _TRANSITIONS.get(current.status, ()):
                raise InvalidSessionTransitionError(current, target)
            snapshot = AnalysisSessionSnapshot(
                session_id=current.session_id,
                status=target,
                created_at=current.created_at,
                updated_at=self._clock.now(),
                configuration=current.configuration,
            )
            event = self._terminal_event(snapshot, len(runtime.events) + 1, error)
            runtime.snapshot = snapshot
            if event is not None:
                runtime.events.append(event)
                runtime.last_error = error
            if target == SessionStatus.CANCELLED:
                runtime.cancel_requested.set()
                runtime.stop_requested.set()
            runtime.condition.notify_all()
        self._log_transition(current.status, snapshot, event)
        if target == SessionStatus.CANCELLED:
            self._log_activity("cancel_accepted", runtime, timestamp=snapshot.updated_at)
        return snapshot

    def _terminal_event(
        self, snapshot: AnalysisSessionSnapshot, sequence: int, error: PublicError | None
    ) -> LifecycleEvent | None:
        if snapshot.status == SessionStatus.RUNNING:
            return None
        if snapshot.status == SessionStatus.COMPLETED:
            payload = SessionCompletedPayload(completed_at=snapshot.updated_at)
            return SessionCompletedEvent(
                session_id=snapshot.session_id,
                sequence=sequence,
                occurred_at=snapshot.updated_at,
                payload=payload,
            )
        if snapshot.status == SessionStatus.FAILED:
            if error is None:
                raise ValueError("A failed transition requires a public error.")
            return SessionFailedEvent(
                session_id=snapshot.session_id,
                sequence=sequence,
                occurred_at=snapshot.updated_at,
                payload=SessionFailedPayload(failed_at=snapshot.updated_at, error=error),
            )
        if snapshot.status == SessionStatus.CANCELLED:
            return SessionCancelledEvent(
                session_id=snapshot.session_id,
                sequence=sequence,
                occurred_at=snapshot.updated_at,
                payload=SessionCancelledPayload(cancelled_at=snapshot.updated_at),
            )
        raise ValueError("Unsupported transition event status.")

    def _log_transition(
        self,
        previous: SessionStatus | None,
        snapshot: AnalysisSessionSnapshot,
        event: LifecycleEvent | None,
    ) -> None:
        try:
            self._logger.info(
                "Session state committed.",
                extra={
                    "event_name": (
                        "lifecycle_event_committed" if event else "session_status_changed"
                    ),
                    "timestamp": snapshot.updated_at,
                    "correlation": CorrelationContext(
                        request_id=request_id_context.get(),
                        session_id=snapshot.session_id,
                        event_sequence=event.sequence if event else None,
                        error_id=event.payload.error.error_id
                        if isinstance(event, SessionFailedEvent)
                        else None,
                    ),
                    "from_status": previous,
                    "to_status": snapshot.status,
                    "lifecycle_event": event,
                },
            )
        except Exception:
            # Logging cannot change an already committed transition.
            pass

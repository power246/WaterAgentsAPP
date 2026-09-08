import asyncio
import logging
from collections.abc import AsyncIterator, Callable
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
from app.domain.common import NonEmptyString
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


class SessionCoordinator:
    def __init__(
        self,
        clock: ClockPort,
        *,
        id_factory: Callable[[], UUID] = uuid4,
        logger: logging.Logger | None = None,
    ) -> None:
        self._clock = clock
        self._id_factory = id_factory
        self._logger = (
            logger if logger is not None else logging.getLogger("wateragents.analysis_sessions")
        )
        self._sessions: dict[UUID, SessionRuntime] = {}
        self._sessions_lock = asyncio.Lock()

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
            if session_id in self._sessions:
                raise ValueError("The generated session ID already exists.")
            self._sessions[session_id] = runtime
        self._log_transition(None, snapshot, event)
        return snapshot

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

    async def subscribe(self, session_id: UUID | str) -> AsyncIterator[LifecycleEvent]:
        return self._events(await self._lookup(session_id))

    async def _lookup(self, session_id: UUID | str) -> SessionRuntime:
        key = _UUID.validate_python(session_id)
        async with self._sessions_lock:
            runtime = self._sessions.get(key)
        if runtime is None:
            raise SessionNotFoundError(key)
        return runtime

    async def _events(self, runtime: SessionRuntime) -> AsyncIterator[LifecycleEvent]:
        cursor = 0
        while True:
            async with runtime.condition:
                await runtime.condition.wait_for(
                    lambda: len(runtime.events) > cursor
                    or runtime.snapshot.status not in _TRANSITIONS
                )
                batch = tuple(runtime.events[cursor:])
                terminal = runtime.snapshot.status not in _TRANSITIONS
            for event in batch:
                cursor = event.sequence
                yield event
            if terminal:
                return

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
            runtime.condition.notify_all()
        self._log_transition(current.status, snapshot, event)
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

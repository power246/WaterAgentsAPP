import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from typing import Coroutine, TypeVar
from uuid import UUID, uuid4

from pydantic import TypeAdapter

from app.application.analysis_runs.creation import (
    CreationCommitUnconfirmedError,
    CreationPersistenceUnavailableError,
    CreationRolledBackError,
    CreationVerificationUnavailableError,
    IdempotencyKeyConflictError,
    IdempotencyWaitTimeoutError,
    InvalidSubmissionEncodingError,
    RunCreationCandidate,
    RunCreationDisposition,
    RunCreationResult,
)
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
from app.application.analysis_runs.persistence import (
    BatchVerification,
    PersistenceGroup,
    PersistenceInfo,
    PersistenceState,
    PersistenceWriteBatch,
    PersistenceWriteConflictError,
)
from app.application.analysis_runs.runtime import AgentSessionRuntime, RunRuntime
from app.application.analysis_runs.workflow import (
    AnalysisWorkflow,
    CancellationToken,
    DefaultFakeWorkflow,
    WorkflowContext,
    WorkflowFailed,
    WorkflowStopped,
)
from app.application.correlation import request_id_context
from app.application.transcripts.buffer import (
    PendingTranscriptReferencesError,
    TranscriptBuffer,
)
from app.application.transcripts.models import (
    AnalysisQuery,
    AnalysisRequestV1,
    ContentVisibility,
    GenerationStatus,
    LateMessageOutputFact,
    MessageEndReason,
    PersistenceRecoveredFact,
    TranscriptContentRef,
    TranscriptItem,
    TranscriptMetadata,
)
from app.application.transcripts.registry import (
    TranscriptRegistry,
    create_production_registry,
)
from app.application.transcripts.read_models import (
    LiveRunRead,
    TranscriptCorrelation,
    TranscriptScope,
)
from app.application.transcripts.stream_models import (
    MessageDeltaProcessRecord,
    MessageFinalizedProcessRecord,
    MessageStartedProcessRecord,
    RunSnapshotEvent,
    RunWaterbodySummary,
    SessionProcessRecord,
    StreamingMessage,
    TranscriptItemHeader,
    TranscriptItemProcessRecord,
    snapshot_from_state,
)
from app.application.transcripts.streaming import StreamingCredentialFilter
from app.domain.common import NonEmptyString, normalize_utc_datetime
from app.domain.time.time_resolution import DEFAULT_TIME_RESOLUTION
from app.domain.time.time_resolution_source import TimeResolutionSource
from app.ports.clock import ClockPort
from app.ports.transcript_store import TranscriptStorePort

_UUID = TypeAdapter(UUID)
_QUERY = TypeAdapter(AnalysisQuery)
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
WorkflowTaskFactory = Callable[[Coroutine[object, object, None], str], asyncio.Task[None]]


@dataclass
class _PreparedCreation:
    candidate: RunCreationCandidate
    runtime: RunRuntime
    event: RunCreatedEvent


@dataclass
class _CreationOwner:
    fingerprint: bytes
    prepared: _PreparedCreation
    task: asyncio.Task[AnalysisRunSnapshot] | None = None
    commit_unconfirmed: asyncio.Event = field(default_factory=asyncio.Event)


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
        transcript_store: TranscriptStorePort[
            RunCreationCandidate,
            RunCreationResult,
            PersistenceWriteBatch,
            BatchVerification,
        ],
        analysis_timezone: str = "America/Chicago",
        id_factory: Callable[[], UUID] = uuid4,
        record_id_factory: Callable[[], UUID] = uuid4,
        logger: logging.Logger | None = None,
        workflow: AnalysisWorkflow | None = None,
        shutdown_grace_period: float = 1.0,
        shutdown_save_grace_period: float = 5.0,
        keepalive_interval: float = 15.0,
        idempotency_wait_timeout: float = 5.0,
        inline_content_bytes: int = 65536,
        preview_bytes: int = 1024,
        workflow_task_factory: WorkflowTaskFactory | None = None,
        verification_retry_interval: float = 0.05,
        persistence_batch_items: int = 100,
        persistence_retry_delays: tuple[float, ...] = (1, 2, 4, 8, 16),
        persistence_attempt_timeout: float = 5.0,
        completion_delivery_grace: float = 2.0,
        live_state_grace_period: float = 60.0,
        persistence_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        transcript_registry: TranscriptRegistry | None = None,
    ) -> None:
        if not isfinite(shutdown_grace_period) or shutdown_grace_period < 0:
            raise ValueError("shutdown_grace_period must be finite and non-negative")
        if (
            not isfinite(shutdown_save_grace_period)
            or shutdown_save_grace_period < 0
        ):
            raise ValueError(
                "shutdown_save_grace_period must be finite and non-negative"
            )
        if (
            isinstance(keepalive_interval, bool)
            or not isfinite(keepalive_interval)
            or keepalive_interval <= 0
        ):
            raise ValueError("keepalive_interval must be finite and positive")
        if not isfinite(idempotency_wait_timeout) or idempotency_wait_timeout <= 0:
            raise ValueError("idempotency_wait_timeout must be finite and positive")
        if not isfinite(verification_retry_interval) or verification_retry_interval <= 0:
            raise ValueError("verification_retry_interval must be finite and positive")
        if type(persistence_batch_items) is not int or persistence_batch_items <= 0:
            raise ValueError("persistence_batch_items must be a positive integer")
        if len(persistence_retry_delays) != 5 or any(
            not isfinite(delay) or delay < 0 for delay in persistence_retry_delays
        ):
            raise ValueError(
                "persistence_retry_delays must contain five finite non-negative delays"
            )
        if (
            isinstance(persistence_attempt_timeout, bool)
            or not isfinite(persistence_attempt_timeout)
            or persistence_attempt_timeout <= 0
        ):
            raise ValueError("persistence_attempt_timeout must be finite and positive")
        if not isfinite(completion_delivery_grace) or completion_delivery_grace < 0:
            raise ValueError("completion_delivery_grace must be finite and non-negative")
        if not isfinite(live_state_grace_period) or live_state_grace_period < 0:
            raise ValueError("live_state_grace_period must be finite and non-negative")
        self._clock = clock
        self._transcript_store = transcript_store
        self._analysis_timezone = analysis_timezone
        self._id_factory = id_factory
        self._record_id_factory = record_id_factory
        self._logger = (
            logger
            if logger is not None
            else logging.getLogger("wateragents.analysis_runs")
        )
        self._runs: dict[UUID, RunRuntime] = {}
        self._creation_owners: dict[UUID, _CreationOwner] = {}
        self._creation_by_run: dict[UUID, _CreationOwner] = {}
        self._creation_tasks: set[asyncio.Task[AnalysisRunSnapshot]] = set()
        self._runs_lock = asyncio.Lock()
        self._workflow = workflow if workflow is not None else DefaultFakeWorkflow()
        self._shutdown_grace_period = shutdown_grace_period
        self._shutdown_save_grace_period = shutdown_save_grace_period
        self._shutdown_lock = asyncio.Lock()
        self._closing = False
        self._keepalive_interval = keepalive_interval
        self._idempotency_wait_timeout = idempotency_wait_timeout
        self._inline_content_bytes = inline_content_bytes
        self._preview_bytes = preview_bytes
        self._registry = (
            transcript_registry
            if transcript_registry is not None
            else create_production_registry()
        )
        self._workflow_task_factory = (
            workflow_task_factory
            if workflow_task_factory is not None
            else lambda coroutine, name: asyncio.create_task(coroutine, name=name)
        )
        self._verification_retry_interval = verification_retry_interval
        self._persistence_batch_items = persistence_batch_items
        self._persistence_retry_delays = persistence_retry_delays
        self._persistence_attempt_timeout = persistence_attempt_timeout
        self._completion_delivery_grace = completion_delivery_grace
        self._live_state_grace_period = live_state_grace_period
        self._persistence_sleep = persistence_sleep

    async def create_run(
        self,
        body: bytes | str,
        configuration: AnalysisConfigurationSnapshot | None = None,
        *,
        idempotency_key: UUID | None = None,
    ) -> AnalysisRunSnapshot:
        raw_body, configuration_override = self._submission_bytes(body, configuration)
        fingerprint = hashlib.sha256(raw_body).digest()
        deadline = asyncio.get_running_loop().time() + self._idempotency_wait_timeout
        while True:
            if idempotency_key is not None:
                owner = await self._current_owner(idempotency_key)
                if owner is not None:
                    self._require_same_fingerprint(owner.fingerprint, fingerprint)
                    try:
                        return await self._await_creation(
                            owner,
                            deadline,
                            initial=False,
                        )
                    except CreationRolledBackError:
                        if asyncio.get_running_loop().time() >= deadline:
                            raise IdempotencyWaitTimeoutError() from None
                        continue
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise IdempotencyWaitTimeoutError()
                try:
                    async with asyncio.timeout(remaining):
                        existing = await self._transcript_store.get_creation(
                            idempotency_key
                        )
                except TimeoutError:
                    raise IdempotencyWaitTimeoutError() from None
                except Exception as error:
                    raise CreationPersistenceUnavailableError() from error
                if existing is not None:
                    self._require_same_fingerprint(
                        existing.creation.request_fingerprint,
                        fingerprint,
                    )
                    return existing.creation.receipt_snapshot()

            async with self._runs_lock:
                if self._closing:
                    raise ServiceShuttingDownError()
                if idempotency_key is not None:
                    owner = self._creation_owners.get(idempotency_key)
                    if owner is not None:
                        self._require_same_fingerprint(owner.fingerprint, fingerprint)
                        initial = False
                    else:
                        owner = self._start_creation_owner(
                            raw_body,
                            fingerprint,
                            idempotency_key,
                            configuration_override,
                        )
                        initial = True
                else:
                    owner = self._start_creation_owner(
                        raw_body,
                        fingerprint,
                        None,
                        configuration_override,
                    )
                    initial = True
            try:
                return await self._await_creation(owner, deadline, initial=initial)
            except CreationRolledBackError:
                if initial or idempotency_key is None:
                    raise
                if asyncio.get_running_loop().time() >= deadline:
                    raise IdempotencyWaitTimeoutError() from None

    def _submission_bytes(
        self,
        body: bytes | str,
        configuration: AnalysisConfigurationSnapshot | None,
    ) -> tuple[bytes, AnalysisConfigurationSnapshot | None]:
        if configuration is None:
            if not isinstance(body, bytes):
                raise TypeError("raw run creation body must be bytes")
            return body, None
        query = _QUERY.validate_python(body)
        checked = AnalysisConfigurationSnapshot.model_validate(configuration)
        payload: dict[str, str] = {"query": query}
        if checked.time_resolution_source is TimeResolutionSource.FRONTEND:
            payload["time_resolution"] = checked.time_resolution.value
        return (
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8"),
            checked,
        )

    async def _current_owner(self, idempotency_key: UUID) -> _CreationOwner | None:
        async with self._runs_lock:
            return self._creation_owners.get(idempotency_key)

    def _start_creation_owner(
        self,
        raw_body: bytes,
        fingerprint: bytes,
        idempotency_key: UUID | None,
        configuration_override: AnalysisConfigurationSnapshot | None,
    ) -> _CreationOwner:
        prepared = self._prepare_creation(
            raw_body,
            fingerprint,
            idempotency_key,
            configuration_override,
        )
        owner = _CreationOwner(fingerprint=fingerprint, prepared=prepared)
        coroutine = self._own_creation(owner)
        try:
            task = asyncio.create_task(
                coroutine,
                name=f"analysis-run-create-{prepared.candidate.snapshot.run_id}",
            )
        except BaseException:
            coroutine.close()
            raise
        owner.task = task
        self._creation_tasks.add(task)
        self._creation_by_run[prepared.candidate.snapshot.run_id] = owner
        if idempotency_key is not None:
            self._creation_owners[idempotency_key] = owner
        task.add_done_callback(self._creation_task_done)
        return owner

    def _prepare_creation(
        self,
        raw_body: bytes,
        fingerprint: bytes,
        idempotency_key: UUID | None,
        configuration_override: AnalysisConfigurationSnapshot | None,
    ) -> _PreparedCreation:
        try:
            raw_body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise InvalidSubmissionEncodingError() from error
        request = AnalysisRequestV1.model_validate_json(raw_body)
        configuration = configuration_override or AnalysisConfigurationSnapshot(
            analysis_timezone=self._analysis_timezone,
            time_resolution=request.time_resolution or DEFAULT_TIME_RESOLUTION,
            time_resolution_source=(
                TimeResolutionSource.FRONTEND
                if "time_resolution" in request.model_fields_set
                else TimeResolutionSource.DEFAULT
            ),
        )
        run_id = self._new_id()
        if run_id in self._runs:
            raise ValueError("The generated run ID already exists.")
        timestamp = self._clock.now()
        snapshot = AnalysisRunSnapshot(
            run_id=run_id,
            status=RunStatus.PENDING,
            created_at=timestamp,
            updated_at=timestamp,
            configuration=configuration,
        )
        transcript_buffer = TranscriptBuffer(self._registry)
        runtime = RunRuntime(
            snapshot=snapshot,
            transcript_buffer=transcript_buffer,
            query=request.query,
        )
        request_id = request_id_context.get()
        request_item = self._reserve_transcript_item_locked(
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
        transcript_buffer.append(
            request_item,
            "analysis_request",
            1,
            request,
        )
        transcript_buffer.append(
            created_item,
            "lifecycle_event",
            2,
            event,
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
        storage = transcript_buffer.to_storage_batch(
            inline_content_bytes=self._inline_content_bytes,
            preview_bytes=self._preview_bytes,
        )
        safe_request = AnalysisRequestV1.model_validate_json(
            transcript_buffer.items[0].content.safe_body
        )
        candidate = RunCreationCandidate(
            snapshot=snapshot,
            query=safe_request.query,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint if idempotency_key is not None else None,
            creation_receipt=snapshot.model_dump(mode="json"),
            request_item_id=request_item.item_id,
            created_item_id=created_item.item_id,
            transcript=storage,
        )
        return _PreparedCreation(candidate=candidate, runtime=runtime, event=event)

    async def _own_creation(self, owner: _CreationOwner) -> AnalysisRunSnapshot:
        candidate = owner.prepared.candidate
        try:
            try:
                result = await self._transcript_store.create_run(candidate)
            except IdempotencyKeyConflictError:
                raise
            except Exception:
                owner.commit_unconfirmed.set()
                result = await self._verify_unknown_creation(candidate)
            if result.disposition is RunCreationDisposition.CREATED:
                if result.creation.run_id != candidate.snapshot.run_id:
                    raise CreationPersistenceUnavailableError()
                await self._register_committed_creation(owner.prepared)
                return candidate.snapshot
            return result.creation.receipt_snapshot()
        finally:
            async with self._runs_lock:
                if candidate.idempotency_key is not None:
                    current = self._creation_owners.get(candidate.idempotency_key)
                    if current is owner:
                        self._creation_owners.pop(candidate.idempotency_key, None)
                if self._creation_by_run.get(candidate.snapshot.run_id) is owner:
                    self._creation_by_run.pop(candidate.snapshot.run_id, None)

    async def _verify_unknown_creation(
        self,
        candidate: RunCreationCandidate,
    ) -> RunCreationResult:
        while True:
            if self._closing:
                raise CreationCommitUnconfirmedError(candidate.snapshot.run_id)
            try:
                result = await self._transcript_store.verify_creation(candidate)
            except CreationVerificationUnavailableError:
                await asyncio.sleep(self._verification_retry_interval)
                continue
            except Exception as error:
                raise CreationPersistenceUnavailableError() from error
            if result is None:
                raise CreationRolledBackError()
            return result

    async def _register_committed_creation(
        self,
        prepared: _PreparedCreation,
    ) -> None:
        runtime = prepared.runtime
        run_id = runtime.snapshot.run_id
        runtime.persisted_item_ids.update(
            item.item_id for item in prepared.candidate.transcript.items
        )
        runtime.persisted_mutation_ids.add(
            runtime.state_mutations[0].mutation_id
        )
        async with self._runs_lock:
            if run_id in self._runs:
                raise ValueError("The generated run ID already exists.")
            self._runs[run_id] = runtime
            closing = self._closing
        self._start_run_writer(runtime)
        self._start_cleanup_monitor(runtime)
        self._log_run_transition(None, runtime.snapshot, prepared.event)
        if closing:
            await self._mark_quiesced(runtime)
            return
        coroutine = self._run_workflow(runtime)
        try:
            task = self._workflow_task_factory(coroutine, f"analysis-run-{run_id}")
        except BaseException as exception:
            coroutine.close()
            await self._mark_quiesced(runtime)
            error = PublicError(
                error_id=self._new_id(),
                code="workflow_start_failed",
                message="The analysis workflow could not be started.",
                failed_stage="workflow_start",
                retryable=False,
            )
            try:
                await self.fail_run(run_id, error)
            except InvalidRunTransitionError:
                pass
            self._log_activity(
                "workflow_start_failed",
                runtime,
                exception=exception,
            )
            return
        runtime.workflow_task = task
        task.add_done_callback(lambda completed: self._workflow_done(runtime, completed))

    def _start_run_writer(self, runtime: RunRuntime) -> None:
        coroutine = self._run_writer(runtime)
        try:
            task = asyncio.create_task(
                coroutine,
                name=f"analysis-run-writer-{runtime.snapshot.run_id}",
            )
        except BaseException as error:
            coroutine.close()
            runtime.persistence_exhausted = True
            runtime.persistence_warning = self._persistence_error(error)
            runtime.writer_quiesced.set()
            return
        runtime.writer_task = task
        task.add_done_callback(
            lambda completed: self._writer_done(runtime, completed)
        )

    def _start_cleanup_monitor(self, runtime: RunRuntime) -> None:
        task = asyncio.create_task(
            self._monitor_live_state_cleanup(runtime),
            name=f"analysis-run-cleanup-{runtime.snapshot.run_id}",
        )
        runtime.cleanup_task = task
        task.add_done_callback(
            lambda completed: self._cleanup_done(runtime, completed)
        )

    async def _await_creation(
        self,
        owner: _CreationOwner,
        deadline: float,
        *,
        initial: bool,
    ) -> AnalysisRunSnapshot:
        try:
            snapshot = await self._wait_for_owner(owner, deadline, initial=initial)
        except BaseException:
            if initial and owner.task is not None:
                owner.task.add_done_callback(
                    lambda completed: self._release_owner_workflow(owner, completed)
                )
            raise
        self._release_owner_workflow(owner, owner.task)
        return snapshot

    def _release_owner_workflow(
        self,
        owner: _CreationOwner,
        completed: asyncio.Task[AnalysisRunSnapshot] | None,
    ) -> None:
        if completed is None or completed.cancelled():
            return
        try:
            completed.result()
        except Exception:
            return
        runtime = owner.prepared.runtime
        if self._runs.get(runtime.snapshot.run_id) is runtime:
            runtime.workflow_start.set()

    async def _wait_for_owner(
        self,
        owner: _CreationOwner,
        deadline: float,
        *,
        initial: bool,
    ) -> AnalysisRunSnapshot:
        if owner.task is None:
            raise RuntimeError("creation owner task was not started")
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise IdempotencyWaitTimeoutError()
        if not initial:
            try:
                async with asyncio.timeout(remaining):
                    return await asyncio.shield(owner.task)
            except TimeoutError:
                if owner.commit_unconfirmed.is_set():
                    raise CreationCommitUnconfirmedError(
                        owner.prepared.candidate.snapshot.run_id
                    ) from None
                raise IdempotencyWaitTimeoutError() from None
        unknown_wait = asyncio.create_task(owner.commit_unconfirmed.wait())
        try:
            done, _ = await asyncio.wait(
                (owner.task, unknown_wait),
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if owner.task in done:
                return owner.task.result()
            if unknown_wait in done and owner.commit_unconfirmed.is_set():
                raise CreationCommitUnconfirmedError(
                    owner.prepared.candidate.snapshot.run_id
                )
            if initial:
                raise CreationCommitUnconfirmedError(
                    owner.prepared.candidate.snapshot.run_id
                )
            raise IdempotencyWaitTimeoutError()
        finally:
            unknown_wait.cancel()
            await asyncio.gather(unknown_wait, return_exceptions=True)

    @staticmethod
    def _require_same_fingerprint(actual: bytes | None, expected: bytes) -> None:
        if actual != expected:
            raise IdempotencyKeyConflictError()

    def _creation_task_done(self, task: asyncio.Task[AnalysisRunSnapshot]) -> None:
        self._creation_tasks.discard(task)
        if task.cancelled():
            return
        task.exception()

    def _enqueue_persistence_group_locked(
        self,
        runtime: RunRuntime,
        *,
        item_ids: tuple[UUID, ...] = (),
        mutation_ids: tuple[UUID, ...] = (),
        recovery_fact: bool = False,
    ) -> None:
        runtime.persistence_groups.append(
            PersistenceGroup(
                item_ids=item_ids,
                mutation_ids=mutation_ids,
                recovery_fact=recovery_fact,
            )
        )
        runtime.writer_wakeup.set()

    def _freeze_persistence_batch_locked(
        self,
        runtime: RunRuntime,
    ) -> PersistenceWriteBatch | None:
        if runtime.persistence_exhausted:
            return None
        if runtime.persistence_active_batch is not None:
            return runtime.persistence_active_batch
        selected: list[PersistenceGroup] = []
        selected_items = 0
        for group in runtime.persistence_groups:
            if selected and (
                group.recovery_fact
                or selected[-1].recovery_fact
                or selected_items + len(group.item_ids) > self._persistence_batch_items
            ):
                break
            candidate = (*selected, group)
            try:
                self._build_persistence_batch_locked(runtime, candidate)
            except PendingTranscriptReferencesError:
                continue
            selected.append(group)
            selected_items += len(group.item_ids)
            if group.recovery_fact or selected_items >= self._persistence_batch_items:
                break
        if not selected:
            return None
        batch = self._build_persistence_batch_locked(runtime, tuple(selected))
        runtime.persistence_active_batch = batch
        return batch

    def _build_persistence_batch_locked(
        self,
        runtime: RunRuntime,
        groups: tuple[PersistenceGroup, ...],
    ) -> PersistenceWriteBatch:
        item_ids = tuple(item_id for group in groups for item_id in group.item_ids)
        mutation_ids = {
            mutation_id for group in groups for mutation_id in group.mutation_ids
        }
        selected_item_ids = set(item_ids)
        persisted = tuple(
            TranscriptContentRef(run_id=runtime.snapshot.run_id, item_id=item_id)
            for item_id in runtime.persisted_item_ids
        )
        generating = tuple(
            TranscriptContentRef(
                run_id=runtime.snapshot.run_id,
                item_id=item.item_id,
            )
            for item in runtime.transcript_items
            if item.item_id not in runtime.persisted_item_ids
            and item.item_id not in selected_item_ids
        )
        transcript = runtime.transcript_buffer.to_storage_batch(
            inline_content_bytes=self._inline_content_bytes,
            preview_bytes=self._preview_bytes,
            persisted=persisted,
            generating=generating,
            item_ids=item_ids,
        )
        run_mutations = tuple(
            mutation
            for mutation in runtime.state_mutations
            if isinstance(mutation, RunStateMutation)
            and mutation.mutation_id in mutation_ids
        )
        session_mutations = tuple(
            mutation
            for mutation in runtime.state_mutations
            if isinstance(mutation, SessionStateMutation)
            and mutation.mutation_id in mutation_ids
        )
        return PersistenceWriteBatch(
            run_id=runtime.snapshot.run_id,
            groups=groups,
            transcript=transcript,
            run_mutations=run_mutations,
            session_mutations=session_mutations,
        )

    async def _run_writer(self, runtime: RunRuntime) -> None:
        try:
            while True:
                try:
                    await runtime.writer_wakeup.wait()
                    runtime.writer_wakeup.clear()
                    while True:
                        async with runtime.condition:
                            if runtime.writer_stop.is_set():
                                return
                            batch = self._freeze_persistence_batch_locked(runtime)
                            if batch is None:
                                break
                        succeeded = await self._attempt_persistence_batch(runtime, batch)
                        if succeeded:
                            continue
                        async with runtime.condition:
                            if runtime.persistence_exhausted:
                                break
                            delay = self._persistence_retry_delays[
                                runtime.persistence_failures - 1
                            ]
                        await self._persistence_sleep(delay)
                        if runtime.writer_stop.is_set():
                            return
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    async with runtime.condition:
                        self._record_persistence_failure_locked(
                            runtime,
                            error,
                            retryable=False,
                        )
                        runtime.persistence_exhausted = True
                        runtime.condition.notify_all()
        finally:
            runtime.writer_quiesced.set()
            async with runtime.condition:
                runtime.condition.notify_all()

    async def _attempt_persistence_batch(
        self,
        runtime: RunRuntime,
        batch: PersistenceWriteBatch,
    ) -> bool:
        try:
            async with asyncio.timeout(self._persistence_attempt_timeout):
                if runtime.persistence_unknown:
                    verification = await self._transcript_store.verify_batch(batch)
                    if verification is BatchVerification.NOT_COMMITTED:
                        async with runtime.condition:
                            runtime.persistence_unknown = False
                        raise RuntimeError(
                            "the previous persistence attempt rolled back"
                        )
                else:
                    try:
                        await self._transcript_store.write_batch(batch)
                    except PersistenceWriteConflictError:
                        raise
                    except Exception:
                        async with runtime.condition:
                            runtime.persistence_unknown = True
                        verification = await self._transcript_store.verify_batch(batch)
                        if verification is BatchVerification.NOT_COMMITTED:
                            async with runtime.condition:
                                runtime.persistence_unknown = False
                            raise RuntimeError(
                                "the persistence attempt did not commit"
                            ) from None
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            async with runtime.condition:
                runtime.persistence_unknown = True
                self._record_persistence_failure_locked(runtime, error)
                runtime.condition.notify_all()
            return False
        except PersistenceWriteConflictError as error:
            async with runtime.condition:
                self._record_persistence_failure_locked(
                    runtime,
                    error,
                    retryable=False,
                )
                runtime.persistence_failures = 6
                runtime.persistence_exhausted = True
                runtime.condition.notify_all()
            return False
        except Exception as error:
            async with runtime.condition:
                self._record_persistence_failure_locked(runtime, error)
                runtime.condition.notify_all()
            return False
        async with runtime.condition:
            self._confirm_persistence_batch_locked(runtime, batch)
            runtime.condition.notify_all()
        return True

    def _record_persistence_failure_locked(
        self,
        runtime: RunRuntime,
        error: BaseException,
        *,
        retryable: bool = True,
    ) -> None:
        runtime.persistence_failures += 1
        if runtime.persistence_warning is None or (
            not retryable and runtime.persistence_warning.retryable
        ):
            runtime.persistence_warning = self._persistence_error(
                error,
                retryable=retryable,
            )
        if runtime.persistence_failures == 4:
            runtime.third_retry_failed_at = asyncio.get_running_loop().time()
        if runtime.persistence_failures >= 6:
            runtime.persistence_exhausted = True
        self._log_activity(
            "persistence_write_failed",
            runtime,
            exception=error,
            error_id=runtime.persistence_warning.error_id,
            attempt=runtime.persistence_failures,
        )

    def _confirm_persistence_batch_locked(
        self,
        runtime: RunRuntime,
        batch: PersistenceWriteBatch,
    ) -> None:
        runtime.persisted_item_ids.update(batch.item_ids)
        runtime.persisted_mutation_ids.update(batch.mutation_ids)
        selected = set(batch.groups)
        runtime.persistence_groups[:] = [
            group for group in runtime.persistence_groups if group not in selected
        ]
        runtime.persistence_active_batch = None
        runtime.persistence_unknown = False
        warning = runtime.persistence_warning
        runtime.persistence_failures = 0
        runtime.persistence_exhausted = False
        runtime.third_retry_failed_at = None
        if warning is not None and not batch.is_recovery_fact:
            self._append_persistence_recovered_locked(runtime, warning.error_id)
        runtime.persistence_warning = None
        if runtime.persistence_groups:
            runtime.writer_wakeup.set()

    def _append_persistence_recovered_locked(
        self,
        runtime: RunRuntime,
        error_id: UUID,
    ) -> None:
        timestamp = self._clock.now()
        identity = self._reserve_transcript_item_locked(
            runtime,
            session_id=None,
            request_id=request_id_context.get(),
            operation_id=None,
            created_at=timestamp,
        )
        runtime.transcript_buffer.append(
            identity,
            "runtime_fact",
            1,
            PersistenceRecoveredFact(error_id=error_id),
        )
        self._enqueue_persistence_group_locked(
            runtime,
            item_ids=(identity.item_id,),
            recovery_fact=True,
        )

    def _persistence_error(
        self,
        error: BaseException,
        *,
        retryable: bool = True,
    ) -> PublicError:
        return PublicError(
            error_id=self._new_id(),
            code=(
                "persistence_write_failed"
                if retryable
                else "persistence_write_conflict"
            ),
            message=(
                "Some analysis records have not been saved yet."
                if retryable
                else "Some analysis records conflict with stored history."
            ),
            failed_stage="persist_transcript",
            retryable=retryable,
        )

    def _persistence_info_locked(self, runtime: RunRuntime) -> PersistenceInfo:
        if runtime.persistence_exhausted:
            state = PersistenceState.RETRY_EXHAUSTED
        elif self._has_pending_persistence_locked(runtime):
            state = PersistenceState.PENDING
        else:
            state = PersistenceState.UP_TO_DATE
        return PersistenceInfo(state=state, warning=runtime.persistence_warning)

    @staticmethod
    def _has_pending_persistence_locked(runtime: RunRuntime) -> bool:
        finalized = {item.item_id for item in runtime.transcript_buffer.items}
        reserved = {item.item_id for item in runtime.transcript_items}
        mutation_ids = {
            mutation.mutation_id for mutation in runtime.state_mutations
        }
        return bool(
            reserved - finalized
            or finalized - runtime.persisted_item_ids
            or mutation_ids - runtime.persisted_mutation_ids
            or runtime.persistence_groups
            or runtime.persistence_active_batch is not None
            or runtime.persistence_unknown
        )

    async def _wait_for_required_persistence(
        self,
        runtime: RunRuntime,
        required_item_ids: set[UUID],
        required_mutation_ids: set[UUID],
    ) -> PublicError | None:
        while True:
            async with runtime.condition:
                if (
                    required_item_ids <= runtime.persisted_item_ids
                    and required_mutation_ids <= runtime.persisted_mutation_ids
                ):
                    return None
                if runtime.persistence_exhausted:
                    return runtime.persistence_warning or self._persistence_error(
                        RuntimeError("persistence writer is unavailable")
                    )
                failed_at = runtime.third_retry_failed_at
                if failed_at is None:
                    await runtime.condition.wait()
                    continue
                remaining = (
                    failed_at
                    + self._completion_delivery_grace
                    - asyncio.get_running_loop().time()
                )
                if remaining <= 0:
                    return runtime.persistence_warning or self._persistence_error(
                        RuntimeError("persistence delivery window expired")
                    )
                try:
                    async with asyncio.timeout(remaining):
                        await runtime.condition.wait()
                except TimeoutError:
                    return runtime.persistence_warning or self._persistence_error(
                        RuntimeError("persistence delivery window expired")
                    )

    def _writer_done(
        self,
        runtime: RunRuntime,
        task: asyncio.Task[None],
    ) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._log_activity("persistence_writer_failed", runtime, exception=error)

    def _cleanup_done(
        self,
        runtime: RunRuntime,
        task: asyncio.Task[None],
    ) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._log_activity("live_state_cleanup_failed", runtime, exception=error)

    async def _monitor_live_state_cleanup(self, runtime: RunRuntime) -> None:
        loop = asyncio.get_running_loop()
        while True:
            async with runtime.condition:
                if self._closing:
                    return
                if not self._cleanup_eligible_locked(runtime):
                    runtime.cleanup_eligible_at = None
                    await runtime.condition.wait()
                    continue
                if runtime.cleanup_eligible_at is None:
                    runtime.cleanup_eligible_at = loop.time()
                remaining = (
                    runtime.cleanup_eligible_at
                    + self._live_state_grace_period
                    - loop.time()
                )
                if remaining > 0:
                    try:
                        async with asyncio.timeout(remaining):
                            await runtime.condition.wait()
                    except TimeoutError:
                        pass
                    continue
            try:
                if await self._release_live_state(runtime):
                    return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._log_activity(
                    "live_state_cleanup_failed",
                    runtime,
                    exception=error,
                )
                async with runtime.condition:
                    runtime.cleanup_eligible_at = None
                await asyncio.sleep(max(self._live_state_grace_period, 0.01))

    def _cleanup_eligible_locked(self, runtime: RunRuntime) -> bool:
        return bool(
            runtime.snapshot.status not in _RUN_TRANSITIONS
            and runtime.workflow_quiesced.is_set()
            and not runtime.late_streaming_messages
            and self._persistence_info_locked(runtime).state
            is PersistenceState.UP_TO_DATE
        )

    async def _release_live_state(self, runtime: RunRuntime) -> bool:
        async with self._runs_lock:
            if self._runs.get(runtime.snapshot.run_id) is not runtime:
                return True
            async with runtime.condition:
                eligible_at = runtime.cleanup_eligible_at
                if (
                    self._closing
                    or eligible_at is None
                    or not self._cleanup_eligible_locked(runtime)
                    or asyncio.get_running_loop().time()
                    < eligible_at + self._live_state_grace_period
                ):
                    return False
                self._runs.pop(runtime.snapshot.run_id)
                runtime.writer_stop.set()
                runtime.writer_wakeup.set()
        writer = runtime.writer_task
        if writer is not None and not writer.done():
            await writer
        self._log_activity("live_state_cleaned", runtime)
        return True

    async def shutdown(self) -> None:
        async with self._shutdown_lock:
            async with self._runs_lock:
                self._closing = True
                creation_tasks = tuple(
                    task for task in self._creation_tasks if not task.done()
                )
                cleanup_tasks = tuple(
                    runtime.cleanup_task
                    for runtime in self._runs.values()
                    if runtime.cleanup_task is not None
                    and not runtime.cleanup_task.done()
                )
                for task in cleanup_tasks:
                    task.cancel()
            if creation_tasks:
                _, pending_creations = await asyncio.wait(
                    creation_tasks,
                    timeout=self._shutdown_grace_period,
                )
                for task in pending_creations:
                    task.cancel()
                await asyncio.gather(*creation_tasks, return_exceptions=True)
            async with self._runs_lock:
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
            if cleanup_tasks:
                await asyncio.gather(*cleanup_tasks, return_exceptions=True)
            if active:
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
                        outcome=(
                            "task_cancelled" if task.cancelled() else "quiesced"
                        ),
                    )
            await self._shutdown_run_writers()

    async def _shutdown_run_writers(self) -> None:
        runtimes = tuple(self._runs.values())
        drainers = [
            asyncio.create_task(self._wait_for_writer_drain(runtime))
            for runtime in runtimes
            if runtime.writer_task is not None and not runtime.writer_task.done()
        ]
        if drainers:
            try:
                async with asyncio.timeout(self._shutdown_save_grace_period):
                    await asyncio.gather(*drainers)
            except TimeoutError:
                for task in drainers:
                    task.cancel()
                await asyncio.gather(*drainers, return_exceptions=True)
        writer_tasks = []
        for runtime in runtimes:
            task = runtime.writer_task
            if task is None or task.done():
                continue
            runtime.writer_stop.set()
            runtime.writer_wakeup.set()
            writer_tasks.append(task)
        if writer_tasks:
            _, pending = await asyncio.wait(writer_tasks, timeout=1.0)
            for task in pending:
                task.cancel()
            await asyncio.gather(*writer_tasks, return_exceptions=True)

    async def _wait_for_writer_drain(self, runtime: RunRuntime) -> None:
        async with runtime.condition:
            await runtime.condition.wait_for(
                lambda: runtime.persistence_exhausted
                or not (
                    runtime.persistence_groups
                    or runtime.persistence_active_batch is not None
                    or runtime.persistence_unknown
                )
            )

    async def _run_workflow(self, runtime: RunRuntime) -> None:
        token = CancellationToken(runtime.stop_requested)
        try:
            await runtime.workflow_start.wait()
            token.checkpoint()
            running = await self.mark_running(runtime.snapshot.run_id)
            await self._workflow.run(WorkflowContext(running, runtime.query, token))
            token.checkpoint()
            async with runtime.condition:
                if self._workflow_has_unfinished_outputs_locked(runtime):
                    raise WorkflowFailed(
                        "The workflow returned before its outputs reached a terminal state."
                    )
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
            if runtime.snapshot.status is not RunStatus.PENDING and not token.is_requested():
                await self._workflow_failed(runtime, error)
            raise
        except Exception as error:
            await self._workflow_failed(runtime, error)
        finally:
            await self._mark_quiesced(runtime)

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

    async def _mark_quiesced(self, runtime: RunRuntime) -> None:
        async with runtime.condition:
            if runtime.workflow_quiesced.is_set():
                return
            for message in tuple(runtime.late_streaming_messages.values()):
                self._finalize_late_message_locked(runtime, message)
            runtime.workflow_quiesced.set()
            runtime.condition.notify_all()
        self._log_activity("workflow_quiesced", runtime)

    @staticmethod
    def _workflow_has_unfinished_outputs_locked(runtime: RunRuntime) -> bool:
        finalized_item_ids = {
            item.item_id for item in runtime.transcript_buffer.items
        }
        return bool(
            runtime.streaming_messages
            or any(
                session.summary.status in _SESSION_TRANSITIONS
                for session in runtime.agent_sessions.values()
            )
            or any(
                item.item_id not in finalized_item_ids
                for item in runtime.transcript_items
            )
        )

    def _log_activity(
        self,
        event_name: str,
        runtime: RunRuntime,
        *,
        timestamp: datetime | None = None,
        exception: BaseException | None = None,
        error_id: UUID | None = None,
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
                            error_id
                            if error_id is not None
                            else (
                                runtime.last_error.error_id
                                if runtime.last_error
                                else None
                            )
                        ),
                    ),
                    "exception_type": (
                        type(exception).__name__ if exception is not None else None
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

    async def get_live_read(self, run_id: UUID | str) -> LiveRunRead | None:
        try:
            runtime = await self._lookup(run_id)
        except RunNotFoundError:
            return None
        async with runtime.condition:
            safe_request = AnalysisRequestV1.model_validate_json(
                runtime.transcript_buffer.items[0].content.safe_body
            )
            return LiveRunRead(
                run=runtime.snapshot,
                query=safe_request.query,
                sessions=tuple(
                    session.summary
                    for session in sorted(
                        runtime.agent_sessions.values(),
                        key=lambda item: (
                            item.summary.created_at,
                            item.summary.session_id,
                        ),
                    )
                ),
                items=runtime.transcript_buffer.items,
                persistence=self._persistence_info_locked(runtime),
            )

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
            finalized = runtime.transcript_buffer.append(
                item,
                "lifecycle_event",
                2,
                event,
            )
            agent_runtime = AgentSessionRuntime(summary=summary, events=[event])
            runtime.agent_sessions[summary.session_id] = agent_runtime
            self._append_transcript_process_locked(runtime, finalized, timestamp)
            if role is AgentRole.QUERY_PARSER:
                runtime.query_parser_session_id = summary.session_id
            else:
                runtime.waterbody_session_ids[comid] = summary.session_id
            mutation = SessionStateMutation(
                mutation_id=mutation_id,
                run_id=runtime.snapshot.run_id,
                previous_status=None,
                summary=summary,
                transcript_item=item,
            )
            runtime.state_mutations.append(mutation)
            self._enqueue_persistence_group_locked(
                runtime,
                item_ids=(item.item_id,),
                mutation_ids=(mutation.mutation_id,),
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
            if runtime.snapshot.status not in _RUN_TRANSITIONS:
                raise InvalidRunTransitionError(
                    runtime.snapshot,
                    RunStatus.RUNNING,
                )
            if session_key is not None:
                session_runtime = self._lookup_agent_session_locked(
                    runtime,
                    session_key,
                )
                if session_runtime.summary.status not in _SESSION_TRANSITIONS:
                    raise InvalidAgentSessionTransitionError(
                        session_runtime.summary,
                        session_runtime.summary.status,
                    )
            item = self._reserve_transcript_item_locked(
                runtime,
                session_id=session_key,
                request_id=request_key,
                operation_id=operation_key,
                created_at=self._clock.now(),
            )
            runtime.condition.notify_all()
            return item

    async def finalize_transcript_item(
        self,
        run_id: UUID | str,
        item_id: UUID | str,
        content_type: str,
        content_schema_version: int,
        body: object,
        *,
        private_values: tuple[str, ...] = (),
    ) -> TranscriptItemIdentity:
        runtime = await self._lookup(run_id)
        key = _UUID.validate_python(item_id)
        async with runtime.condition:
            identity = next(
                (item for item in runtime.transcript_items if item.item_id == key),
                None,
            )
            if identity is None:
                suppressed = runtime.suppressed_transcript_items.get(key)
                if suppressed is not None:
                    return suppressed
                raise ValueError("transcript item identity is not reserved in this run")
            reserved_references = tuple(
                TranscriptContentRef(
                    run_id=candidate.snapshot.run_id,
                    item_id=item.item_id,
                )
                for candidate in self._runs.values()
                for item in candidate.transcript_items
            )
            try:
                finalized = runtime.transcript_buffer.append(
                    identity,
                    content_type,
                    content_schema_version,
                    body,
                    private_values=private_values,
                    reserved_references=reserved_references,
                )
            except (TypeError, ValueError):
                finalized_ids = {
                    item.item_id for item in runtime.transcript_buffer.items
                }
                if identity.item_id not in finalized_ids:
                    runtime.transcript_items.remove(identity)
                    runtime.condition.notify_all()
                raise
            self._append_transcript_process_locked(
                runtime,
                finalized,
                self._clock.now(),
            )
            self._enqueue_persistence_group_locked(
                runtime,
                item_ids=(identity.item_id,),
            )
            runtime.condition.notify_all()
            return identity

    async def start_transcript_message(
        self,
        run_id: UUID | str,
        session_id: UUID | str,
        content_type: str,
        content_schema_version: int,
        *,
        request_id: UUID | str | None = None,
        operation_id: UUID | str | None = None,
        tags: tuple[str, ...] = (),
        rag_eligible: bool = False,
        private_values: tuple[str, ...] = (),
    ) -> TranscriptItemIdentity:
        if not self._registry.is_message_type(
            content_type,
            content_schema_version,
        ):
            raise ValueError("streamed transcript content must be a registered message")
        runtime = await self._lookup(run_id)
        session_key = _UUID.validate_python(session_id)
        request_key = _UUID.validate_python(request_id) if request_id is not None else None
        operation_key = (
            _UUID.validate_python(operation_id) if operation_id is not None else None
        )
        async with runtime.condition:
            if runtime.snapshot.status not in _RUN_TRANSITIONS:
                raise InvalidRunTransitionError(
                    runtime.snapshot,
                    RunStatus.RUNNING,
                )
            session_runtime = self._lookup_agent_session_locked(runtime, session_key)
            if session_runtime.summary.status not in _SESSION_TRANSITIONS:
                raise InvalidAgentSessionTransitionError(
                    session_runtime.summary,
                    session_runtime.summary.status,
                )
            timestamp = self._clock.now()
            identity = self._reserve_transcript_item_locked(
                runtime,
                session_id=session_key,
                request_id=request_key,
                operation_id=operation_key,
                created_at=timestamp,
            )
            metadata = TranscriptMetadata(tags=tags, rag_eligible=rag_eligible)
            header = TranscriptItemHeader(
                item_id=identity.item_id,
                run_id=identity.run_id,
                transcript_sequence=identity.transcript_sequence,
                occurred_at=identity.created_at,
                content_type=content_type,
                content_schema_version=content_schema_version,
                scope=TranscriptScope(
                    session_id=session_key,
                    agent_role=session_runtime.summary.agent_role,
                    target_comid=session_runtime.summary.target_comid,
                ),
                correlation=TranscriptCorrelation(
                    request_id=request_key,
                    operation_id=operation_key,
                ),
                tags=metadata.tags,
                rag_eligible=metadata.rag_eligible,
            )
            runtime.streaming_messages[identity.item_id] = StreamingMessage(
                item_id=identity.item_id,
                session_id=session_key,
                content_type=content_type,
                content_schema_version=content_schema_version,
                metadata=metadata,
                filter=StreamingCredentialFilter(private_values),
                private_values=private_values,
            )
            session_runtime.process_events.append(MessageStartedProcessRecord(
                sequence=len(session_runtime.process_events) + 1,
                occurred_at=timestamp,
                header=header,
            ))
            runtime.condition.notify_all()
            return identity

    async def append_transcript_message_delta(
        self,
        run_id: UUID | str,
        item_id: UUID | str,
        text: str,
    ) -> str:
        runtime = await self._lookup(run_id)
        item_key = _UUID.validate_python(item_id)
        async with runtime.condition:
            message = runtime.streaming_messages.get(item_key)
            if message is None:
                message = runtime.late_streaming_messages.get(item_key)
                if message is None:
                    raise ValueError("streamed transcript message is not active")
                message.late_text += message.filter.feed(text)
                runtime.condition.notify_all()
                return ""
            safe_text = message.filter.feed(text)
            if safe_text:
                self._append_message_delta_locked(runtime, message, safe_text)
                runtime.condition.notify_all()
            return safe_text

    async def finalize_transcript_message(
        self,
        run_id: UUID | str,
        item_id: UUID | str,
        body: object,
    ) -> TranscriptItemIdentity:
        runtime = await self._lookup(run_id)
        item_key = _UUID.validate_python(item_id)
        async with runtime.condition:
            message = runtime.streaming_messages.get(item_key)
            if message is None:
                message = runtime.late_streaming_messages.get(item_key)
                if message is None:
                    raise ValueError("streamed transcript message is not active")
                encoded = self._registry.encode(
                    message.content_type,
                    message.content_schema_version,
                    body,
                    private_values=message.private_values,
                )
                projection = self._registry.message_projection(
                    message.content_type,
                    message.content_schema_version,
                    encoded.safe_body,
                )
                if projection.generation_status is GenerationStatus.GENERATING:
                    raise ValueError("a finalized transcript message cannot be generating")
                safe_tail = message.filter.finish()
                if safe_tail:
                    message.late_text += safe_tail
                observed = message.safe_text + message.late_text
                if projection.text.startswith(observed):
                    message.late_text += projection.text[len(observed):]
                self._finalize_late_message_locked(runtime, message)
                runtime.condition.notify_all()
                return next(
                    item for item in runtime.transcript_items if item.item_id == item_key
                )
            identity = next(
                item for item in runtime.transcript_items if item.item_id == item_key
            )
            encoded = self._registry.encode(
                message.content_type,
                message.content_schema_version,
                body,
                private_values=message.private_values,
            )
            projection = self._registry.message_projection(
                message.content_type,
                message.content_schema_version,
                encoded.safe_body,
            )
            if projection.generation_status is GenerationStatus.GENERATING:
                raise ValueError("a finalized transcript message cannot be generating")
            safe_tail = message.filter.finish()
            if safe_tail:
                self._append_message_delta_locked(runtime, message, safe_tail)
            mismatch = not projection.text.startswith(message.safe_text)
            if projection.text.startswith(message.safe_text):
                suffix = projection.text[len(message.safe_text):]
                if suffix:
                    self._append_message_delta_locked(runtime, message, suffix)
            finalized_body = body
            finalized_status = projection.generation_status
            if mismatch:
                finalized_status = GenerationStatus.INCOMPLETE
                finalized_body = self._registry.finalized_message_body(
                    message.content_type,
                    message.content_schema_version,
                    message.safe_text,
                    finalized_status,
                    MessageEndReason.ERROR,
                )
            finalized = runtime.transcript_buffer.append(
                identity,
                message.content_type,
                message.content_schema_version,
                finalized_body,
                private_values=message.private_values,
                metadata=message.metadata,
            )
            session_runtime = self._lookup_agent_session_locked(
                runtime,
                message.session_id,
            )
            timestamp = self._clock.now()
            session_runtime.process_events.append(MessageFinalizedProcessRecord(
                sequence=len(session_runtime.process_events) + 1,
                occurred_at=timestamp,
                item=finalized,
                generation_status=finalized_status,
            ))
            persistence_item_ids = [identity.item_id]
            if mismatch:
                diagnostic_identity = self._reserve_transcript_item_locked(
                    runtime,
                    session_id=message.session_id,
                    request_id=identity.request_id,
                    operation_id=identity.operation_id,
                    created_at=self._clock.now(),
                )
                diagnostic = runtime.transcript_buffer.append(
                    diagnostic_identity,
                    "error",
                    1,
                    PublicError(
                        error_id=self._new_record_id(),
                        code="message_content_mismatch",
                        message="The finalized message did not match its streamed text.",
                        failed_stage="message_finalize",
                        retryable=False,
                    ),
                )
                self._append_transcript_process_locked(
                    runtime,
                    diagnostic,
                    diagnostic.created_at,
                )
                persistence_item_ids.append(diagnostic.item_id)
            runtime.streaming_messages.pop(item_key)
            self._enqueue_persistence_group_locked(
                runtime,
                item_ids=tuple(persistence_item_ids),
            )
            runtime.condition.notify_all()
            return identity

    def _freeze_streaming_messages_locked(
        self,
        runtime: RunRuntime,
        *,
        session_id: UUID | None = None,
        end_reason: MessageEndReason,
    ) -> None:
        for message in tuple(runtime.streaming_messages.values()):
            if session_id is not None and message.session_id != session_id:
                continue
            safe_tail = message.filter.cancel()
            if safe_tail:
                self._append_message_delta_locked(runtime, message, safe_tail)
            identity = next(
                item
                for item in runtime.transcript_items
                if item.item_id == message.item_id
            )
            body = self._registry.finalized_message_body(
                message.content_type,
                message.content_schema_version,
                message.safe_text,
                GenerationStatus.INCOMPLETE,
                end_reason,
            )
            finalized = runtime.transcript_buffer.append(
                identity,
                message.content_type,
                message.content_schema_version,
                body,
                private_values=message.private_values,
                metadata=message.metadata,
            )
            session_runtime = self._lookup_agent_session_locked(
                runtime,
                message.session_id,
            )
            session_runtime.process_events.append(MessageFinalizedProcessRecord(
                sequence=len(session_runtime.process_events) + 1,
                occurred_at=self._clock.now(),
                item=finalized,
                generation_status=GenerationStatus.INCOMPLETE,
            ))
            runtime.streaming_messages.pop(message.item_id)
            message.late_end_reason = end_reason
            runtime.late_streaming_messages[message.item_id] = message
            self._enqueue_persistence_group_locked(
                runtime,
                item_ids=(message.item_id,),
            )

    @staticmethod
    def _suppress_pending_transcript_items_locked(
        runtime: RunRuntime,
        *,
        session_id: UUID | None = None,
    ) -> None:
        finalized_item_ids = {
            item.item_id for item in runtime.transcript_buffer.items
        }
        retained: list[TranscriptItemIdentity] = []
        for item in runtime.transcript_items:
            if (
                item.item_id not in finalized_item_ids
                and (session_id is None or item.session_id == session_id)
            ):
                runtime.suppressed_transcript_items[item.item_id] = item
            else:
                retained.append(item)
        runtime.transcript_items[:] = retained

    def _finalize_late_message_locked(
        self,
        runtime: RunRuntime,
        message: StreamingMessage,
    ) -> None:
        safe_tail = message.filter.finish()
        if safe_tail:
            message.late_text += safe_tail
        if message.late_text:
            original = next(
                item
                for item in runtime.transcript_items
                if item.item_id == message.item_id
            )
            identity = self._reserve_transcript_item_locked(
                runtime,
                session_id=message.session_id,
                request_id=original.request_id,
                operation_id=original.operation_id,
                created_at=self._clock.now(),
            )
            runtime.transcript_buffer.append(
                identity,
                "runtime_fact",
                1,
                LateMessageOutputFact(
                    original_item_id=message.item_id,
                    text=message.late_text,
                    end_reason=(
                        message.late_end_reason or MessageEndReason.CANCELLED
                    ),
                ),
            )
            self._enqueue_persistence_group_locked(
                runtime,
                item_ids=(identity.item_id,),
            )
        runtime.late_streaming_messages.pop(message.item_id, None)

    def _append_message_delta_locked(
        self,
        runtime: RunRuntime,
        message: StreamingMessage,
        safe_text: str,
    ) -> None:
        session_runtime = self._lookup_agent_session_locked(
            runtime,
            message.session_id,
        )
        message.safe_text += safe_text
        session_runtime.process_events.append(MessageDeltaProcessRecord(
            sequence=len(session_runtime.process_events) + 1,
            occurred_at=self._clock.now(),
            item_id=message.item_id,
            text=safe_text,
        ))

    async def set_waterbody_participation(
        self,
        run_id: UUID | str,
        participation: Mapping[str, bool],
    ) -> None:
        runtime = await self._lookup(run_id)
        if any(type(value) is not bool for value in participation.values()):
            raise TypeError("waterbody participation values must be booleans")
        validated = {
            _COMID.validate_python(comid): value
            for comid, value in participation.items()
        }
        async with runtime.condition:
            runtime.waterbody_participation = validated
            runtime.condition.notify_all()

    def _append_transcript_process_locked(
        self,
        runtime: RunRuntime,
        item: TranscriptItem,
        occurred_at: datetime,
    ) -> None:
        if item.session_id is None or item.content.visibility is not ContentVisibility.AUTO:
            return
        session_runtime = self._lookup_agent_session_locked(runtime, item.session_id)
        session_runtime.process_events.append(TranscriptItemProcessRecord(
            sequence=len(session_runtime.process_events) + 1,
            occurred_at=occurred_at,
            item=item,
        ))

    async def get_persistence_info(
        self,
        run_id: UUID | str,
    ) -> PersistenceInfo:
        runtime = await self._lookup(run_id)
        async with runtime.condition:
            return self._persistence_info_locked(runtime)

    async def retry_persistence(
        self,
        run_id: UUID | str,
    ) -> PersistenceInfo:
        runtime = await self._lookup(run_id)
        async with runtime.condition:
            if runtime.persistence_exhausted and self._has_pending_persistence_locked(
                runtime
            ):
                runtime.persistence_exhausted = False
                runtime.persistence_failures = 0
                runtime.third_retry_failed_at = None
                runtime.writer_wakeup.set()
                runtime.condition.notify_all()
            return self._persistence_info_locked(runtime)

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
        runtime = await self._lookup(run_id)
        async with runtime.condition:
            if runtime.snapshot.status is not RunStatus.RUNNING:
                raise InvalidRunTransitionError(runtime.snapshot, RunStatus.COMPLETED)
            required_items = frozenset(
                item.item_id for item in runtime.transcript_buffer.items
            )
            required_mutations = frozenset(
                mutation.mutation_id for mutation in runtime.state_mutations
            )
        warning = await self._wait_for_required_persistence(
            runtime,
            required_items,
            required_mutations,
        )
        async with runtime.condition:
            if runtime.snapshot.status is RunStatus.CANCELLED:
                return runtime.snapshot
        if warning is None:
            return await self._transition_run(run_id, RunStatus.COMPLETED)
        return await self._transition_run(
            run_id,
            RunStatus.COMPLETED_WITH_PERSISTENCE_ERROR,
            warning,
        )

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

    async def subscribe_run(
        self,
        run_id: UUID | str,
    ) -> AsyncGenerator[RunSnapshotEvent | None, None]:
        runtime = await self._lookup(run_id)
        async with runtime.condition:
            initial = self._run_snapshot_locked(runtime)
        return self._run_snapshots(runtime, initial)

    async def subscribe_session(
        self,
        run_id: UUID | str,
        session_id: UUID | str,
        *,
        after_sequence: int | str | None = None,
    ) -> AsyncGenerator[SessionProcessRecord | None, None]:
        cursor = self._event_cursor(after_sequence)
        runtime = await self._lookup(run_id)
        session_key = _UUID.validate_python(session_id)
        async with runtime.condition:
            session_runtime = self._lookup_agent_session_locked(runtime, session_key)
            last_sequence = len(session_runtime.process_events)
            if isinstance(cursor, str):
                limit = str(last_sequence)
                if (len(cursor), cursor) > (len(limit), limit):
                    raise EventCursorAheadError(runtime.snapshot, last_sequence)
                cursor = int(cursor)
            if cursor > last_sequence:
                raise EventCursorAheadError(runtime.snapshot, last_sequence)
        return self._session_process_events(runtime, session_key, cursor)

    @staticmethod
    def _event_cursor(after_sequence: int | str | None) -> int | str:
        if after_sequence is None:
            return 0
        if type(after_sequence) is int and after_sequence >= 0:
            return after_sequence
        if (
            isinstance(after_sequence, str)
            and after_sequence.isascii()
            and after_sequence.isdecimal()
        ):
            return after_sequence.lstrip("0") or "0"
        raise InvalidEventCursorError()

    def _run_snapshot_locked(self, runtime: RunRuntime) -> RunSnapshotEvent:
        sessions = tuple(
            item.summary
            for item in sorted(
                runtime.agent_sessions.values(),
                key=lambda value: (value.summary.created_at, value.summary.session_id),
            )
        )
        participation = dict(runtime.waterbody_participation)
        participation.update({comid: True for comid in runtime.waterbody_session_ids})
        waterbodies = tuple(
            RunWaterbodySummary(
                target_comid=comid,
                participating=participating,
                session_id=runtime.waterbody_session_ids.get(comid),
                session_status=(
                    runtime.agent_sessions[runtime.waterbody_session_ids[comid]].summary.status
                    if comid in runtime.waterbody_session_ids
                    else None
                ),
            )
            for comid, participating in sorted(participation.items())
        )
        return snapshot_from_state(
            runtime.snapshot,
            sessions,
            waterbodies,
            self._persistence_info_locked(runtime),
        )

    def _run_stream_closed_locked(self, runtime: RunRuntime) -> bool:
        return bool(
            runtime.snapshot.status not in _RUN_TRANSITIONS
            and runtime.workflow_quiesced.is_set()
            and self._persistence_info_locked(runtime).state is PersistenceState.UP_TO_DATE
        )

    async def _run_snapshots(
        self,
        runtime: RunRuntime,
        initial: RunSnapshotEvent,
    ) -> AsyncGenerator[RunSnapshotEvent | None, None]:
        current = initial
        yield current
        while True:
            async with runtime.condition:
                if self._run_stream_closed_locked(runtime):
                    return
                try:
                    async with asyncio.timeout(self._keepalive_interval):
                        await runtime.condition.wait_for(
                            lambda: self._run_snapshot_locked(runtime) != current
                            or self._run_stream_closed_locked(runtime)
                        )
                except TimeoutError:
                    pass
                updated = self._run_snapshot_locked(runtime)
                closed = self._run_stream_closed_locked(runtime)
            changed = updated != current
            if changed:
                current = updated
                yield updated
            if closed:
                return
            if not changed:
                yield None

    async def _session_process_events(
        self,
        runtime: RunRuntime,
        session_id: UUID,
        cursor: int,
    ) -> AsyncGenerator[SessionProcessRecord | None, None]:
        while True:
            async with runtime.condition:
                session_runtime = self._lookup_agent_session_locked(runtime, session_id)
                terminal = session_runtime.summary.status not in _SESSION_TRANSITIONS
                message_active = any(
                    item.session_id == session_id
                    for item in runtime.streaming_messages.values()
                )
                if cursor == len(session_runtime.process_events) and terminal and not message_active:
                    return
                try:
                    async with asyncio.timeout(self._keepalive_interval):
                        await runtime.condition.wait_for(
                            lambda: len(session_runtime.process_events) > cursor
                            or (
                                session_runtime.summary.status not in _SESSION_TRANSITIONS
                                and not any(
                                    item.session_id == session_id
                                    for item in runtime.streaming_messages.values()
                                )
                            )
                        )
                except TimeoutError:
                    pass
                batch = tuple(session_runtime.process_events[cursor:])
                terminal = session_runtime.summary.status not in _SESSION_TRANSITIONS
                message_active = any(
                    item.session_id == session_id
                    for item in runtime.streaming_messages.values()
                )
            for event in batch:
                cursor = event.sequence
                yield event
            if terminal and not message_active and cursor == len(session_runtime.process_events):
                return
            if not batch:
                yield None

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
            owner = self._creation_by_run.get(key)
        if runtime is None and owner is not None and owner.task is not None:
            try:
                async with asyncio.timeout(self._idempotency_wait_timeout):
                    await asyncio.shield(owner.task)
            except TimeoutError:
                raise CreationCommitUnconfirmedError(key) from None
            except (CreationRolledBackError, IdempotencyKeyConflictError):
                pass
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
                runtime.transcript_buffer.append(
                    item,
                    "lifecycle_event",
                    2,
                    event,
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
            mutation = RunStateMutation(
                mutation_id=mutation_id,
                previous_status=current.status,
                snapshot=snapshot,
                transcript_item=item,
            )
            runtime.state_mutations.append(mutation)
            self._enqueue_persistence_group_locked(
                runtime,
                item_ids=(item.item_id,) if item is not None else (),
                mutation_ids=(mutation.mutation_id,),
            )
            if resolved in {RunStatus.FAILED, RunStatus.CANCELLED}:
                if resolved is RunStatus.CANCELLED:
                    runtime.cancel_requested.set()
                runtime.stop_requested.set()
                end_reason = (
                    MessageEndReason.CANCELLED
                    if resolved is RunStatus.CANCELLED
                    else MessageEndReason.ERROR
                )
                session_target = (
                    AgentSessionStatus.CANCELLED
                    if resolved is RunStatus.CANCELLED
                    else AgentSessionStatus.FAILED
                )
                self._freeze_streaming_messages_locked(
                    runtime,
                    end_reason=end_reason,
                )
                for session_runtime in runtime.agent_sessions.values():
                    if session_runtime.summary.status in _SESSION_TRANSITIONS:
                        session_error = None
                        if session_target is AgentSessionStatus.FAILED:
                            assert error is not None
                            session_error = error.model_copy(
                                update={"error_id": self._new_record_id()}
                            )
                        session_transitions.append(
                            self._transition_agent_session_locked(
                                runtime,
                                session_runtime,
                                session_target,
                                session_error,
                                timestamp,
                            )
                        )
                    session_runtime.stop_requested.set()
                self._suppress_pending_transcript_items_locked(runtime)
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
        if resolved in {
            AgentSessionStatus.FAILED,
            AgentSessionStatus.CANCELLED,
        }:
            self._freeze_streaming_messages_locked(
                runtime,
                session_id=current.session_id,
                end_reason=(
                    MessageEndReason.CANCELLED
                    if resolved is AgentSessionStatus.CANCELLED
                    else MessageEndReason.ERROR
                ),
            )
            self._suppress_pending_transcript_items_locked(
                runtime,
                session_id=current.session_id,
            )
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
            finalized = runtime.transcript_buffer.append(
                item,
                "lifecycle_event",
                2,
                event,
            )
            self._append_transcript_process_locked(runtime, finalized, timestamp)
            session_runtime.events.append(event)
            session_runtime.last_error = (
                error if resolved is AgentSessionStatus.FAILED else None
            )
        if resolved is AgentSessionStatus.RUNNING:
            if session_runtime.execution_claimed:
                raise InvalidAgentSessionTransitionError(current, target)
            session_runtime.execution_claimed = True
        if resolved in {
            AgentSessionStatus.FAILED,
            AgentSessionStatus.CANCELLED,
        }:
            session_runtime.stop_requested.set()
        session_runtime.summary = summary
        mutation = SessionStateMutation(
            mutation_id=mutation_id,
            run_id=runtime.snapshot.run_id,
            previous_status=current.status,
            summary=summary,
            transcript_item=item,
        )
        runtime.state_mutations.append(mutation)
        self._enqueue_persistence_group_locked(
            runtime,
            item_ids=(item.item_id,) if item is not None else (),
            mutation_ids=(mutation.mutation_id,),
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

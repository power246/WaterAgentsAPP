import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import isfinite
from typing import Coroutine, TypeVar
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError

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
    AnalysisPhase,
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
    RunSummarySessionResultSummary,
    RunSummaryStatus,
    SessionCancelledEvent,
    SessionCancelledPayload,
    SessionCompletedEvent,
    SessionCompletedPayload,
    SessionCreatedEvent,
    SessionCreatedPayload,
    SessionFailedEvent,
    SessionFailedPayload,
    SessionLifecycleEvent,
    SessionResultReference,
    SessionResultSummary,
    SessionStateMutation,
    SessionSummary,
    TranscriptItemIdentity,
    TopologyRunResultSummary,
    WaterbodySessionResultSummary,
)
from app.application.analysis_runs.persistence import (
    BatchVerification,
    PersistenceInfo,
    PersistenceState,
    PersistenceWriteBatch,
)
from app.application.analysis_runs.topology_result_models import (
    DependencyOutcome,
    DependencyStatus,
    GraphOutcomeProjection,
    RunSummaryProjection,
    TopologyRunResult,
    WaterbodySchedulingStatus,
)
from app.application.analysis_runs.runtime import AgentSessionRuntime, RunRuntime
from app.application.analysis_runs.writer import TranscriptWriter
from app.application.analysis_runs.workflow import (
    AnalysisWorkflow,
    CancellationToken,
    WorkflowContext,
    WorkflowFailed,
    WorkflowStopped,
)
from app.application.correlation import request_id_context
from app.application.run_summary.models import RunSummaryResult
from app.application.transcripts.buffer import TranscriptBuffer
from app.application.transcripts.models import (
    AnalysisQuery,
    AnalysisRequestV1,
    ContentVisibility,
    GenerationStatus,
    LateMessageOutputFact,
    MessageEndReason,
    PersistenceRecoveredFact,
    SuppressedTranscriptItemFact,
    TranscriptContentRef,
    TranscriptItem,
    TranscriptMetadata,
)
from app.application.transcripts.registry import (
    TranscriptRegistry,
    create_production_registry,
)
from app.application.transcripts.redaction import redact_tags
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
from app.domain import HydrologyGraph, HydrologyParticipation, InferenceResult
from app.domain.common import NonEmptyString, normalize_utc_datetime
from app.domain.model_catalog import ModelRole
from app.domain.stable_validation_error import stable_validation_error
from app.domain.time.time_resolution_preference_source import (
    TimeResolutionPreferenceSource,
)
from app.domain.time.timezone import get_iana_timezone
from app.ports.clock import ClockPort
from app.ports.model_catalog import ModelCatalogPort
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
RunCleanupHook = Callable[[UUID], Awaitable[None]]


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
        model_catalog: ModelCatalogPort,
        analysis_timezone: str = "America/Chicago",
        id_factory: Callable[[], UUID] = uuid4,
        record_id_factory: Callable[[], UUID] = uuid4,
        logger: logging.Logger | None = None,
        workflow: AnalysisWorkflow,
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
        if not isfinite(live_state_grace_period) or live_state_grace_period < 0:
            raise ValueError("live_state_grace_period must be finite and non-negative")
        self._clock = clock
        self._transcript_store = transcript_store
        self._model_catalog = model_catalog
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
        self._workflow = workflow
        self._shutdown_grace_period = shutdown_grace_period
        self._shutdown_save_grace_period = shutdown_save_grace_period
        self._shutdown_lock = asyncio.Lock()
        self._closing = False
        self._run_cleanup_hook: RunCleanupHook | None = None
        self._cleaned_run_resources: set[UUID] = set()
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
        self._live_state_grace_period = live_state_grace_period
        self._transcript_writer = TranscriptWriter(
            transcript_store,
            error_id_factory=self._new_id,
            recovered=self._append_persistence_recovered_locked,
            log_activity=self._log_activity,
            inline_content_bytes=inline_content_bytes,
            preview_bytes=preview_bytes,
            batch_items=persistence_batch_items,
            retry_delays=persistence_retry_delays,
            attempt_timeout=persistence_attempt_timeout,
            completion_delivery_grace=completion_delivery_grace,
            sleep=persistence_sleep,
        )

    def register_run_cleanup_hook(self, hook: RunCleanupHook) -> None:
        if self._closing:
            raise RuntimeError("cannot register run cleanup during shutdown")
        if self._runs:
            raise RuntimeError("run cleanup hook must be registered before runs")
        if self._run_cleanup_hook is not None:
            raise RuntimeError("run cleanup hook is already registered")
        self._run_cleanup_hook = hook

    async def _cleanup_run_resources(self, run_id: UUID) -> None:
        if (
            self._run_cleanup_hook is None
            or run_id in self._cleaned_run_resources
        ):
            return
        await self._run_cleanup_hook(run_id)
        self._cleaned_run_resources.add(run_id)

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
        payload: dict[str, object] = {
            "query": query,
            "parser_model": checked.parser_model.model_dump(mode="json"),
            "waterbody_model": checked.waterbody_model.model_dump(mode="json"),
            "memory_mode": checked.memory_mode.value,
        }
        if (
            checked.time_resolution_preference_source
            is TimeResolutionPreferenceSource.FRONTEND
        ):
            payload["time_resolution"] = checked.time_resolution_preference.value
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
        timestamp = self._clock.now()
        catalog = self._model_catalog.snapshot()
        validation_errors: list[dict[str, object]] = []
        if request.date_range is not None:
            local_today = timestamp.astimezone(
                get_iana_timezone(self._analysis_timezone)
            ).date()
            earliest = local_today - timedelta(days=7)
            latest = local_today + timedelta(days=7)
            if (
                request.date_range.start_date < earliest
                or request.date_range.end_date > latest
            ):
                validation_errors.append(
                    {
                        "type": "value_error",
                        "ctx": {
                            "error": stable_validation_error(
                                "date_range_out_of_bounds",
                                "date_range must be within {earliest} and {latest}",
                                {
                                    "earliest": earliest.isoformat(),
                                    "latest": latest.isoformat(),
                                },
                            )
                        },
                        "loc": ("date_range",),
                        "input": request.date_range.model_dump(mode="json"),
                    }
                )
        for field_name, role in (
            ("parser_model", ModelRole.QUERY_PARSER),
            ("waterbody_model", ModelRole.WATERBODY),
        ):
            selection = getattr(request, field_name)
            if not catalog.supports(selection, role):
                validation_errors.append(
                    {
                        "type": "value_error",
                        "ctx": {
                            "error": stable_validation_error(
                                "model_selection_unsupported",
                                "model is not selectable for role {role}",
                                {"role": role.value},
                            )
                        },
                        "loc": (field_name,),
                        "input": selection.model_dump(mode="json"),
                    }
                )
        if validation_errors:
            raise ValidationError.from_exception_data(
                "AnalysisRequestV1", validation_errors
            )
        configuration = configuration_override or request.to_configuration_snapshot(
            self._analysis_timezone
        )
        run_id = self._new_id()
        if run_id in self._runs:
            raise ValueError("The generated run ID already exists.")
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
        coroutine = self._transcript_writer.run(runtime)
        try:
            task = asyncio.create_task(
                coroutine,
                name=f"analysis-run-writer-{runtime.snapshot.run_id}",
            )
        except BaseException as error:
            coroutine.close()
            runtime.persistence_exhausted = True
            runtime.persistence_warning = self._transcript_writer.error_for(error)
            runtime.writer_quiesced.set()
            return
        runtime.writer_task = task
        task.add_done_callback(
            lambda completed: self._transcript_writer.task_done(runtime, completed)
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
            if initial:
                raise CreationCommitUnconfirmedError(
                    owner.prepared.candidate.snapshot.run_id
                )
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
        self._transcript_writer.enqueue_locked(
            runtime,
            item_ids=(identity.item_id,),
            recovery_fact=True,
        )

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
            and self._transcript_writer.info_locked(runtime).state
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
                await self._cleanup_run_resources(runtime.snapshot.run_id)
                self._runs.pop(runtime.snapshot.run_id)
                self._cleaned_run_resources.discard(runtime.snapshot.run_id)
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
            cleanup_runtimes = tuple(self._runs.values())
            cleanup_results = await asyncio.gather(
                *(
                    self._cleanup_run_resources(runtime.snapshot.run_id)
                    for runtime in cleanup_runtimes
                ),
                return_exceptions=True,
            )
            for runtime, result in zip(
                cleanup_runtimes,
                cleanup_results,
                strict=True,
            ):
                if isinstance(result, BaseException):
                    self._log_activity(
                        "run_resource_cleanup_failed",
                        runtime,
                        exception=result,
                    )

    async def _shutdown_run_writers(self) -> None:
        runtimes = tuple(self._runs.values())
        drainers = [
            asyncio.create_task(self._transcript_writer.drain(runtime))
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

    async def _run_workflow(self, runtime: RunRuntime) -> None:
        token = CancellationToken(
            runtime.stop_requested,
            runtime.workflow_commit_gate,
        )
        try:
            await runtime.workflow_start.wait()
            token.checkpoint()
            running = await self.mark_running(runtime.snapshot.run_id)
            safe_request = AnalysisRequestV1.model_validate_json(
                runtime.transcript_buffer.items[0].content.safe_body
            )
            await self._workflow.run(
                WorkflowContext(
                    running,
                    runtime.query,
                    token,
                    safe_request,
                    self,
                )
            )
            token.checkpoint()
            async with runtime.condition:
                if self._workflow_has_unfinished_outputs_locked(runtime):
                    raise WorkflowFailed(
                        "The workflow returned before its outputs reached a terminal state."
                    )
            async with runtime.condition:
                should_complete = runtime.snapshot.status is RunStatus.RUNNING
            if should_complete:
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
        failure = exception if isinstance(exception, WorkflowFailed) else None
        error = PublicError(
            error_id=self._new_id(),
            code=failure.code if failure is not None else "workflow_failed",
            message=(
                failure.public_message
                if failure is not None
                else "The analysis workflow failed."
            ),
            failed_stage=(
                failure.failed_stage
                if failure is not None
                else "analysis_workflow"
            ),
            retryable=failure.retryable if failure is not None else False,
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
                persistence=self._transcript_writer.info_locked(runtime),
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

    async def begin_waterbody_execution(
        self,
        run_id: UUID | str,
    ) -> tuple[TranscriptContentRef, TranscriptContentRef]:
        runtime = await self._lookup(run_id)
        async with runtime.condition:
            if (
                runtime.cancel_requested.is_set()
                or runtime.stop_requested.is_set()
                or runtime.snapshot.status is not RunStatus.RUNNING
            ):
                raise InvalidRunTransitionError(
                    runtime.snapshot,
                    RunStatus.RUNNING,
                )
            if (
                runtime.analysis_phase is not AnalysisPhase.TOPOLOGY_READY
                or runtime.context_ref is None
                or runtime.topology_ref is None
            ):
                raise ValueError(
                    "waterbody execution requires saved context and topology"
                )
            runtime.analysis_phase = AnalysisPhase.WATERBODY_EXECUTION
            runtime.condition.notify_all()
            return runtime.context_ref, runtime.topology_ref

    async def begin_run_summarization(
        self,
        run_id: UUID | str,
    ) -> None:
        runtime = await self._lookup(run_id)
        async with runtime.condition:
            if (
                runtime.cancel_requested.is_set()
                or runtime.stop_requested.is_set()
                or runtime.snapshot.status is not RunStatus.RUNNING
            ):
                raise InvalidRunTransitionError(
                    runtime.snapshot,
                    RunStatus.RUNNING,
                )
            if runtime.analysis_phase is not AnalysisPhase.WATERBODY_EXECUTION:
                raise ValueError(
                    "run summarization requires settled waterbody execution"
                )
            runtime.analysis_phase = AnalysisPhase.RUN_SUMMARIZATION
            runtime.condition.notify_all()

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
        if (role is AgentRole.WATERBODY) != (comid is not None):
            raise ValueError("only waterbody sessions require target_comid")
        async with runtime.condition:
            if self._closing:
                raise ServiceShuttingDownError()
            if (
                runtime.cancel_requested.is_set()
                or runtime.stop_requested.is_set()
                or runtime.snapshot.status not in _RUN_TRANSITIONS
            ):
                raise InvalidRunTransitionError(runtime.snapshot, RunStatus.RUNNING)
            if role is AgentRole.QUERY_PARSER:
                existing_id = runtime.query_parser_session_id
            elif role is AgentRole.WATERBODY:
                existing_id = runtime.waterbody_session_ids.get(comid)
            else:
                existing_id = runtime.run_summary_session_id
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
            elif role is AgentRole.WATERBODY:
                runtime.waterbody_session_ids[comid] = summary.session_id
                runtime.waterbody_ready.discard(comid)
            else:
                runtime.run_summary_session_id = summary.session_id
            mutation = SessionStateMutation(
                mutation_id=mutation_id,
                run_id=runtime.snapshot.run_id,
                previous_status=None,
                summary=summary,
                transcript_item=item,
            )
            runtime.state_mutations.append(mutation)
            self._transcript_writer.enqueue_locked(
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

    async def complete_waterbody_agent_session(
        self,
        run_id: UUID | str,
        session_id: UUID | str,
        result: InferenceResult,
    ) -> SessionSummary:
        runtime = await self._lookup(run_id)
        key = _UUID.validate_python(session_id)
        validated_result = InferenceResult.model_validate(result)
        async with runtime.condition:
            session_runtime = self._lookup_agent_session_locked(runtime, key)
            current = session_runtime.summary
            if (
                current.agent_role is not AgentRole.WATERBODY
                or validated_result.run_id != runtime.snapshot.run_id
                or validated_result.session_id != key
                or validated_result.target_comid != current.target_comid
            ):
                raise ValueError("inference result does not match the waterbody session")
            if current.status is AgentSessionStatus.CANCELLED:
                return current
            if current.status not in _SESSION_TRANSITIONS:
                raise InvalidAgentSessionTransitionError(
                    current,
                    AgentSessionStatus.COMPLETED,
                )
            if (
                runtime.cancel_requested.is_set()
                or session_runtime.stop_requested.is_set()
            ):
                previous, summary, event = self._transition_agent_session_locked(
                    runtime,
                    session_runtime,
                    AgentSessionStatus.CANCELLED,
                    None,
                    None,
                )
                runtime.condition.notify_all()
            else:
                timestamp = self._clock.now()
                result_identity = self._reserve_transcript_item_locked(
                    runtime,
                    session_id=key,
                    request_id=request_id_context.get(),
                    operation_id=None,
                    created_at=timestamp,
                )
                result_item = runtime.transcript_buffer.append(
                    result_identity,
                    "inference_result",
                    1,
                    validated_result,
                )
                self._append_transcript_process_locked(
                    runtime,
                    result_item,
                    timestamp,
                )
                result_ref = SessionResultReference(
                    run_id=runtime.snapshot.run_id,
                    item_id=result_identity.item_id,
                )
                result_summary = WaterbodySessionResultSummary(
                    result_id=validated_result.result_id,
                    target_comid=validated_result.target_comid,
                    risk_level=validated_result.overall_assessment.risk_level,
                    availability=validated_result.overall_assessment.availability,
                    overall_confidence=validated_result.overall_confidence,
                )
                previous, summary, event = self._transition_agent_session_locked(
                    runtime,
                    session_runtime,
                    AgentSessionStatus.COMPLETED,
                    None,
                    timestamp,
                    result_ref=result_ref,
                    result_summary=result_summary,
                    additional_item_ids=(result_identity.item_id,),
                )
                runtime.condition.notify_all()
        self._log_agent_session_transition(runtime, previous, summary, event)
        return summary

    async def complete_run_summary_agent_session(
        self,
        run_id: UUID | str,
        session_id: UUID | str,
        result: RunSummaryResult,
    ) -> SessionSummary:
        runtime = await self._lookup(run_id)
        key = _UUID.validate_python(session_id)
        validated_result = RunSummaryResult.model_validate(result)
        async with runtime.condition:
            session_runtime = self._lookup_agent_session_locked(runtime, key)
            current = session_runtime.summary
            if (
                current.agent_role is not AgentRole.RUN_SUMMARIZER
                or validated_result.run_id != runtime.snapshot.run_id
                or validated_result.session_id != key
            ):
                raise ValueError("run summary result does not match its session")
            if current.status is AgentSessionStatus.CANCELLED:
                return current
            if current.status not in _SESSION_TRANSITIONS:
                raise InvalidAgentSessionTransitionError(
                    current,
                    AgentSessionStatus.COMPLETED,
                )
            if (
                runtime.cancel_requested.is_set()
                or session_runtime.stop_requested.is_set()
            ):
                previous, summary, event = self._transition_agent_session_locked(
                    runtime,
                    session_runtime,
                    AgentSessionStatus.CANCELLED,
                    None,
                    None,
                )
                runtime.condition.notify_all()
            else:
                timestamp = self._clock.now()
                result_identity = self._reserve_transcript_item_locked(
                    runtime,
                    session_id=key,
                    request_id=request_id_context.get(),
                    operation_id=None,
                    created_at=timestamp,
                )
                result_item = runtime.transcript_buffer.append(
                    result_identity,
                    "run_summary_result",
                    1,
                    validated_result,
                )
                self._append_transcript_process_locked(
                    runtime,
                    result_item,
                    timestamp,
                )
                result_ref = SessionResultReference(
                    run_id=runtime.snapshot.run_id,
                    item_id=result_identity.item_id,
                )
                result_summary = RunSummarySessionResultSummary(
                    highlight_count=len(validated_result.highlights),
                    language=validated_result.language,
                )
                previous, summary, event = self._transition_agent_session_locked(
                    runtime,
                    session_runtime,
                    AgentSessionStatus.COMPLETED,
                    None,
                    timestamp,
                    result_ref=result_ref,
                    result_summary=result_summary,
                    additional_item_ids=(result_identity.item_id,),
                )
                runtime.condition.notify_all()
        self._log_agent_session_transition(runtime, previous, summary, event)
        return summary

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
            suppressed = runtime.suppressed_transcript_items.get(key)
            if suppressed is not None:
                return suppressed
            identity = next(
                (item for item in runtime.transcript_items if item.item_id == key),
                None,
            )
            if identity is None:
                raise ValueError("transcript item identity is not reserved in this run")
            self._validate_m4_content_scope_locked(
                runtime,
                identity,
                content_type,
                content_schema_version,
            )
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
            content_ref = TranscriptContentRef(
                run_id=runtime.snapshot.run_id,
                item_id=identity.item_id,
            )
            if (content_type, content_schema_version) == ("query_understood", 1):
                runtime.context_ref = content_ref
                runtime.analysis_phase = AnalysisPhase.TOPOLOGY_BUILDING
            elif (content_type, content_schema_version) == ("topology_ready", 1):
                runtime.topology_ref = content_ref
                runtime.analysis_phase = AnalysisPhase.TOPOLOGY_READY
                topology = HydrologyGraph.model_validate(body)
                runtime.waterbody_participation = {
                    node.comid: node.participation is HydrologyParticipation.ACTIVE
                    for node in topology.nodes
                }
            self._transcript_writer.enqueue_locked(
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
        initial_body: object | None = None,
    ) -> TranscriptItemIdentity:
        if not self._registry.is_message_type(
            content_type,
            content_schema_version,
        ):
            raise ValueError("streamed transcript content must be a registered message")
        base_safe_body = None
        derived_metadata = TranscriptMetadata()
        if initial_body is not None:
            encoded = self._registry.encode(
                content_type,
                content_schema_version,
                initial_body,
                private_values=private_values,
            )
            projection = self._registry.message_projection(
                content_type,
                content_schema_version,
                encoded.safe_body,
            )
            if (
                projection.generation_status is not GenerationStatus.GENERATING
                or projection.text
                or projection.end_reason is not None
            ):
                raise ValueError("initial streamed message body must be empty and generating")
            base_safe_body = encoded.safe_body
            derived_metadata = encoded.metadata
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
            if (
                (content_type, content_schema_version)
                == ("query_parser_message", 1)
                and session_runtime.summary.agent_role is not AgentRole.QUERY_PARSER
            ):
                raise ValueError(
                    "query_parser_message must belong to the query_parser session"
                )
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
            metadata = TranscriptMetadata(
                tags=tuple(dict.fromkeys((*derived_metadata.tags, *tags))),
                rag_eligible=(derived_metadata.rag_eligible or rag_eligible),
                confidence=derived_metadata.confidence,
                provenance=derived_metadata.provenance,
            )
            metadata = metadata.model_copy(
                update={
                    "tags": redact_tags(metadata.tags, private_values),
                },
            )
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
                base_safe_body=base_safe_body,
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
                    message.base_safe_body,
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
            self._transcript_writer.enqueue_locked(
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
                message.base_safe_body,
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
            self._transcript_writer.enqueue_locked(
                runtime,
                item_ids=(message.item_id,),
            )

    def _suppress_pending_transcript_items_locked(
        self,
        runtime: RunRuntime,
        *,
        session_id: UUID | None = None,
        reason: MessageEndReason,
    ) -> None:
        finalized_items = runtime.transcript_buffer.items
        finalized_item_ids = {item.item_id for item in finalized_items}
        referenced_item_ids = {
            reference_id
            for item in finalized_items
            if item.item_id not in runtime.persisted_item_ids
            for reference_id in item.content.references
        }
        retained: list[TranscriptItemIdentity] = []
        for item in runtime.transcript_items:
            if (
                item.item_id not in finalized_item_ids
                and (session_id is None or item.session_id == session_id)
            ):
                runtime.suppressed_transcript_items[item.item_id] = item
                if item.item_id in referenced_item_ids:
                    runtime.transcript_buffer.append(
                        item,
                        "runtime_fact",
                        1,
                        SuppressedTranscriptItemFact(reason=reason.value),
                    )
                    self._transcript_writer.enqueue_locked(
                        runtime,
                        item_ids=(item.item_id,),
                    )
                    retained.append(item)
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
            self._transcript_writer.enqueue_locked(
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

    async def mark_waterbody_ready(
        self,
        run_id: UUID | str,
        target_comid: str,
    ) -> None:
        runtime = await self._lookup(run_id)
        comid = _COMID.validate_python(target_comid)
        async with runtime.condition:
            if (
                runtime.cancel_requested.is_set()
                or runtime.stop_requested.is_set()
                or runtime.snapshot.status is not RunStatus.RUNNING
                or runtime.analysis_phase is not AnalysisPhase.WATERBODY_EXECUTION
                or runtime.waterbody_participation.get(comid) is not True
                or comid in runtime.waterbody_session_ids
            ):
                raise ValueError("only an unclaimed active waterbody can become ready")
            runtime.waterbody_ready.add(comid)
            runtime.condition.notify_all()

    async def record_waterbody_dependency_outcome(
        self,
        run_id: UUID | str,
        outcome: DependencyOutcome,
    ) -> None:
        runtime = await self._lookup(run_id)
        validated = DependencyOutcome.model_validate(outcome)
        async with runtime.condition:
            session_id = runtime.waterbody_session_ids.get(validated.target_comid)
            if (
                validated.run_id != runtime.snapshot.run_id
                or session_id != validated.session_id
            ):
                raise ValueError("waterbody outcome does not match the running workflow")
            session = runtime.agent_sessions[session_id].summary
            expected_status = {
                AgentSessionStatus.COMPLETED: {
                    DependencyStatus.USABLE,
                    DependencyStatus.INSUFFICIENT,
                },
                AgentSessionStatus.FAILED: {DependencyStatus.INVALID},
                AgentSessionStatus.CANCELLED: {DependencyStatus.CANCELLED},
            }.get(session.status, set())
            if (
                validated.status not in expected_status
                or validated.result_ref != session.result_ref
                or validated.result_summary != session.result_summary
            ):
                raise ValueError("waterbody outcome is not a committed session projection")
            runtime.waterbody_dependency_statuses[validated.target_comid] = (
                validated.status
            )
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
            return self._transcript_writer.info_locked(runtime)

    async def retry_persistence(
        self,
        run_id: UUID | str,
    ) -> PersistenceInfo | None:
        key = _UUID.validate_python(run_id)
        while True:
            async with self._runs_lock:
                runtime = self._runs.get(key)
                owner = self._creation_by_run.get(key)
                if runtime is not None:
                    async with runtime.condition:
                        return self._transcript_writer.retry_locked(runtime)
            if owner is None or owner.task is None:
                return None
            try:
                async with asyncio.timeout(self._idempotency_wait_timeout):
                    await asyncio.shield(owner.task)
            except TimeoutError:
                raise CreationCommitUnconfirmedError(key) from None
            except (CreationRolledBackError, IdempotencyKeyConflictError):
                return None

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

    async def complete_run(
        self,
        run_id: UUID | str,
        result: TopologyRunResult | None = None,
    ) -> AnalysisRunSnapshot:
        runtime = await self._lookup(run_id)
        validated_result = (
            TopologyRunResult.model_validate(result) if result is not None else None
        )
        async with runtime.condition:
            if runtime.snapshot.status is not RunStatus.RUNNING:
                raise InvalidRunTransitionError(runtime.snapshot, RunStatus.COMPLETED)
            if validated_result is not None:
                self._validate_topology_result_locked(runtime, validated_result)
            required_items = frozenset(
                item.item_id for item in runtime.transcript_buffer.items
            )
            required_mutations = frozenset(
                mutation.mutation_id for mutation in runtime.state_mutations
            )
        warning = await self._transcript_writer.wait_for_required(
            runtime,
            required_items,
            required_mutations,
        )
        async with runtime.condition:
            if runtime.snapshot.status is RunStatus.CANCELLED:
                return runtime.snapshot
        if warning is None:
            return await self._transition_run(
                run_id,
                RunStatus.COMPLETED,
                topology_result=validated_result,
            )
        return await self._transition_run(
            run_id,
            RunStatus.COMPLETED_WITH_PERSISTENCE_ERROR,
            warning,
            topology_result=validated_result,
        )

    @staticmethod
    def _validate_topology_result_locked(
        runtime: RunRuntime,
        result: TopologyRunResult,
    ) -> None:
        if (
            result.run_id != runtime.snapshot.run_id
            or result.analysis_context_ref != runtime.context_ref
            or result.topology_ref != runtime.topology_ref
            or runtime.analysis_phase is not AnalysisPhase.RUN_SUMMARIZATION
        ):
            raise ValueError("topology result does not match the running workflow")
        active = {
            comid
            for comid, participating in runtime.waterbody_participation.items()
            if participating
        }
        if active != {entry.target_comid for entry in result.node_outcomes}:
            raise ValueError("topology result must exactly cover active waterbodies")
        for entry in result.node_outcomes:
            session_id = runtime.waterbody_session_ids.get(entry.target_comid)
            if session_id != entry.session_id:
                raise ValueError("topology result waterbody session identity changed")
            summary = runtime.agent_sessions[entry.session_id].summary
            if (
                summary.status != entry.session_status
                or summary.result_ref != entry.result_ref
                or summary.result_summary != entry.result_summary
            ):
                raise ValueError("topology result waterbody projection is not committed")
        summary_runtime = runtime.agent_sessions.get(result.summary.session_id)
        if (
            summary_runtime is None
            or summary_runtime.summary.agent_role is not AgentRole.RUN_SUMMARIZER
        ):
            raise ValueError("topology result summary session is unavailable")
        if result.summary.status is RunSummaryStatus.COMPLETED:
            if (
                summary_runtime.summary.status is not AgentSessionStatus.COMPLETED
                or summary_runtime.summary.result_ref != result.summary.result_ref
            ):
                raise ValueError("topology result summary is not committed")
        elif (
            summary_runtime.summary.status is not AgentSessionStatus.FAILED
            or summary_runtime.last_error != result.summary.public_error
        ):
            raise ValueError("topology result summary failure is not committed")

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
        async with runtime.workflow_commit_gate:
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
                scheduling_status=self._waterbody_scheduling_status_locked(
                    runtime,
                    comid,
                    participating,
                ),
                dependency_status=runtime.waterbody_dependency_statuses.get(comid),
                session_id=runtime.waterbody_session_ids.get(comid),
                session_status=(
                    runtime.agent_sessions[runtime.waterbody_session_ids[comid]].summary.status
                    if comid in runtime.waterbody_session_ids
                    else None
                ),
                result_ref=(
                    runtime.agent_sessions[
                        runtime.waterbody_session_ids[comid]
                    ].summary.result_ref
                    if comid in runtime.waterbody_session_ids
                    else None
                ),
            )
            for comid, participating in sorted(participation.items())
        )
        return snapshot_from_state(
            runtime.snapshot,
            runtime.analysis_phase,
            runtime.context_ref,
            runtime.topology_ref,
            sessions,
            waterbodies,
            self._transcript_writer.info_locked(runtime),
            runtime.result_ref,
            self._run_summary_projection_locked(runtime),
            self._graph_outcome_projection_locked(runtime),
        )

    @staticmethod
    def _run_summary_projection_locked(runtime: RunRuntime) -> RunSummaryProjection:
        session_id = runtime.run_summary_session_id
        if session_id is None:
            return RunSummaryProjection()
        session_runtime = runtime.agent_sessions[session_id]
        summary = session_runtime.summary
        if summary.status in {
            AgentSessionStatus.PENDING,
            AgentSessionStatus.RUNNING,
        }:
            return RunSummaryProjection(
                status=RunSummaryStatus.RUNNING,
                session_id=session_id,
            )
        if summary.status is AgentSessionStatus.COMPLETED:
            return RunSummaryProjection(
                status=RunSummaryStatus.COMPLETED,
                session_id=session_id,
                result_ref=summary.result_ref,
            )
        if summary.status is AgentSessionStatus.FAILED:
            if session_runtime.last_error is None:
                raise RuntimeError("failed run summary session has no public error")
            return RunSummaryProjection(
                status=RunSummaryStatus.FAILED,
                session_id=session_id,
                error=session_runtime.last_error,
            )
        return RunSummaryProjection(
            status=RunSummaryStatus.CANCELLED,
            session_id=session_id,
        )

    @staticmethod
    def _graph_outcome_projection_locked(runtime: RunRuntime) -> GraphOutcomeProjection:
        summary = runtime.result_summary
        if summary is None:
            return GraphOutcomeProjection()
        counts = summary.outcome_counts
        return GraphOutcomeProjection(
            coverage=summary.graph_coverage,
            usable_nodes=counts.usable,
            insufficient_nodes=counts.insufficient,
            invalid_nodes=counts.invalid,
            cancelled_nodes=counts.cancelled,
        )

    @staticmethod
    def _waterbody_scheduling_status_locked(
        runtime: RunRuntime,
        comid: str,
        participating: bool,
    ) -> WaterbodySchedulingStatus:
        if not participating:
            return WaterbodySchedulingStatus.EXCLUDED
        session_id = runtime.waterbody_session_ids.get(comid)
        if session_id is None:
            if comid in runtime.waterbody_ready:
                return WaterbodySchedulingStatus.READY
            return WaterbodySchedulingStatus.WAITING
        if runtime.agent_sessions[session_id].summary.status in {
            AgentSessionStatus.COMPLETED,
            AgentSessionStatus.FAILED,
            AgentSessionStatus.CANCELLED,
        }:
            return WaterbodySchedulingStatus.TERMINAL
        return WaterbodySchedulingStatus.RUNNING

    def _run_stream_closed_locked(self, runtime: RunRuntime) -> bool:
        return bool(
            runtime.snapshot.status not in _RUN_TRANSITIONS
            and runtime.workflow_quiesced.is_set()
            and self._transcript_writer.info_locked(runtime).state
            is PersistenceState.UP_TO_DATE
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

    async def _transition_run(
        self,
        run_id: UUID | str,
        target: RunStatus,
        error: PublicError | None = None,
        *,
        topology_result: TopologyRunResult | None = None,
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
            if topology_result is not None:
                if resolved not in {
                    RunStatus.COMPLETED,
                    RunStatus.COMPLETED_WITH_PERSISTENCE_ERROR,
                }:
                    topology_result = None
                else:
                    self._validate_topology_result_locked(runtime, topology_result)
            snapshot = AnalysisRunSnapshot(
                run_id=current.run_id,
                status=resolved,
                created_at=current.created_at,
                updated_at=timestamp,
                configuration=current.configuration,
            )
            result_identity = None
            result_ref = None
            result_summary = None
            if topology_result is not None:
                result_identity = self._reserve_transcript_item_locked(
                    runtime,
                    session_id=None,
                    request_id=request_id_context.get(),
                    operation_id=None,
                    created_at=timestamp,
                )
                runtime.transcript_buffer.append(
                    result_identity,
                    "topology_run_result",
                    1,
                    topology_result,
                )
                result_ref = SessionResultReference(
                    run_id=snapshot.run_id,
                    item_id=result_identity.item_id,
                )
                result_summary = TopologyRunResultSummary(
                    target_comid=topology_result.target_comid,
                    target_result_summary=topology_result.target_result_summary,
                    graph_coverage=topology_result.graph_coverage,
                    outcome_counts=topology_result.outcome_counts,
                    summary_status=topology_result.summary.status,
                    summary_result_ref=topology_result.summary.result_ref,
                    summary_error=topology_result.summary.public_error,
                )
            event = self._run_terminal_event(
                snapshot,
                len(runtime.events) + 1,
                error,
                result_ref=result_ref,
                result_summary=result_summary,
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
            if result_identity is not None:
                runtime.result_ref = TranscriptContentRef(
                    run_id=snapshot.run_id,
                    item_id=result_identity.item_id,
                )
                runtime.result_summary = result_summary
                runtime.waterbody_dependency_statuses = {
                    outcome.target_comid: outcome.dependency_status
                    for outcome in topology_result.node_outcomes
                }
            if resolved is RunStatus.RUNNING:
                runtime.analysis_phase = AnalysisPhase.QUERY_UNDERSTANDING
            elif resolved not in _RUN_TRANSITIONS:
                runtime.analysis_phase = AnalysisPhase.TERMINAL
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
            self._transcript_writer.enqueue_locked(
                runtime,
                item_ids=(
                    *((result_identity.item_id,) if result_identity is not None else ()),
                    *((item.item_id,) if item is not None else ()),
                ),
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
                self._suppress_pending_transcript_items_locked(
                    runtime,
                    reason=end_reason,
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
                target is AgentSessionStatus.COMPLETED
                and session_runtime.summary.agent_role is not AgentRole.QUERY_PARSER
            ):
                raise ValueError(
                    "result sessions must complete with a typed result"
                )
            if (
                session_runtime.summary.status is AgentSessionStatus.CANCELLED
                and target in {
                    AgentSessionStatus.COMPLETED,
                    AgentSessionStatus.FAILED,
                    AgentSessionStatus.CANCELLED,
                }
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
        *,
        result_ref: SessionResultReference | None = None,
        result_summary: SessionResultSummary | None = None,
        additional_item_ids: tuple[UUID, ...] = (),
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
            end_reason = (
                MessageEndReason.CANCELLED
                if resolved is AgentSessionStatus.CANCELLED
                else MessageEndReason.ERROR
            )
            self._freeze_streaming_messages_locked(
                runtime,
                session_id=current.session_id,
                end_reason=end_reason,
            )
            self._suppress_pending_transcript_items_locked(
                runtime,
                session_id=current.session_id,
                reason=end_reason,
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
            result_ref=result_ref,
            result_summary=result_summary,
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
        self._transcript_writer.enqueue_locked(
            runtime,
            item_ids=(
                *additional_item_ids,
                *((item.item_id,) if item is not None else ()),
            ),
            mutation_ids=(mutation.mutation_id,),
        )
        return current.status, summary, event

    def _run_terminal_event(
        self,
        snapshot: AnalysisRunSnapshot,
        sequence: int,
        error: PublicError | None,
        *,
        result_ref: SessionResultReference | None = None,
        result_summary: TopologyRunResultSummary | None = None,
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
                    result_ref=result_ref,
                    result_summary=result_summary,
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
                payload=SessionCompletedPayload(
                    completed_at=summary.updated_at,
                    result_ref=summary.result_ref,
                    result_summary=summary.result_summary,
                ),
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
    def _validate_m4_content_scope_locked(
        runtime: RunRuntime,
        identity: TranscriptItemIdentity,
        content_type: str,
        content_schema_version: int,
    ) -> None:
        key = content_type, content_schema_version
        if key in {("query_parser_input", 1), ("query_understood", 1)}:
            if identity.session_id is None:
                raise ValueError(f"{content_type} must belong to a parser session")
            session = RunCoordinator._lookup_agent_session_locked(
                runtime,
                identity.session_id,
            )
            if session.summary.agent_role is not AgentRole.QUERY_PARSER:
                raise ValueError(f"{content_type} must belong to a parser session")
        if key == ("query_understood", 1) and runtime.context_ref is not None:
            raise ValueError("query_understood must be unique within a run")
        if key == ("topology_ready", 1):
            if identity.session_id is not None:
                raise ValueError("topology_ready must be run-owned")
            if runtime.context_ref is None:
                raise ValueError("topology_ready requires query_understood")
            if runtime.topology_ref is not None:
                raise ValueError("topology_ready must be unique within a run")

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

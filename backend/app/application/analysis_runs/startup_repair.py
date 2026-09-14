from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid4

from app.application.analysis_runs.models import (
    AgentSessionStatus,
    AnalysisRunSnapshot,
    CorrelationContext,
    PublicError,
    RunCreatedEvent,
    RunFailedEvent,
    RunFailedPayload,
    RunStateMutation,
    RunStatus,
    SessionFailedEvent,
    SessionFailedPayload,
    SessionStateMutation,
    SessionSummary,
    TranscriptItemIdentity,
)
from app.application.analysis_runs.persistence import (
    PersistenceGroup,
    PersistenceWriteBatch,
)
from app.application.transcripts.buffer import TranscriptBuffer
from app.application.transcripts.models import ParseStatus, StoredTranscriptItem
from app.application.transcripts.read_models import StoredRunRead
from app.application.transcripts.registry import TranscriptRegistry
from app.ports.clock import ClockPort
from app.ports.transcript_store import StartupRepairStorePort

_ACTIVE_RUNS = {RunStatus.PENDING, RunStatus.RUNNING}
_ACTIVE_SESSIONS = {AgentSessionStatus.PENDING, AgentSessionStatus.RUNNING}


class StartupRepairError(RuntimeError):
    def __init__(self, error: PublicError) -> None:
        self.error = error
        super().__init__(f"startup repair failed (error_id={error.error_id})")


async def repair_interrupted_runs(
    store: StartupRepairStorePort[StoredRunRead, PersistenceWriteBatch],
    registry: TranscriptRegistry,
    clock: ClockPort,
    *,
    id_factory: Callable[[], UUID] = uuid4,
    inline_content_bytes: int,
    preview_bytes: int,
    logger: logging.Logger | None = None,
) -> int:
    repaired = 0
    try:
        run_ids = await store.list_interrupted_run_ids()
        for run_id in run_ids:
            stored = await store.read_run(run_id)
            if stored is None:
                continue
            batch = _repair_batch(
                stored,
                registry,
                clock.now(),
                id_factory=id_factory,
                inline_content_bytes=inline_content_bytes,
                preview_bytes=preview_bytes,
            )
            if batch is None:
                continue
            await store.write_batch(batch)
            repaired += 1
    except Exception as cause:
        error = PublicError(
            error_id=id_factory(),
            code="startup_repair_failed",
            message="Interrupted analysis records could not be repaired.",
            failed_stage="startup_repair",
            retryable=True,
        )
        (logger or logging.getLogger("wateragents.startup")).error(
            "Startup repair failed.",
            exc_info=(type(cause), cause, cause.__traceback__),
            extra={
                "event_name": "startup_repair_failed",
                "correlation": CorrelationContext(error_id=error.error_id),
            },
        )
        raise StartupRepairError(error) from cause
    return repaired


def _repair_batch(
    stored: StoredRunRead,
    registry: TranscriptRegistry,
    repaired_at: datetime,
    *,
    id_factory: Callable[[], UUID],
    inline_content_bytes: int,
    preview_bytes: int,
) -> PersistenceWriteBatch | None:
    active_sessions = tuple(
        session for session in stored.sessions if session.status in _ACTIVE_SESSIONS
    )
    if stored.run.status not in _ACTIVE_RUNS and not active_sessions:
        return None
    repaired_at = max(
        repaired_at,
        stored.run.updated_at,
        *(session.updated_at for session in active_sessions),
    )

    bodies = _read_lifecycle_bodies(stored, registry)
    created = next(
        (
            body
            for item, body in bodies
            if item.item_id == stored.run.created_item_id
            and isinstance(body, RunCreatedEvent)
        ),
        None,
    )
    if created is None:
        raise ValueError("the stored run creation lifecycle item is unreadable")

    transcript = TranscriptBuffer(registry)
    next_transcript_sequence = max(
        (item.transcript_sequence for item in stored.items),
        default=0,
    ) + 1
    item_ids: list[UUID] = []
    mutation_ids: list[UUID] = []
    run_mutations: list[RunStateMutation] = []
    session_mutations: list[SessionStateMutation] = []

    def identity(session_id: UUID | None) -> TranscriptItemIdentity:
        nonlocal next_transcript_sequence
        result = TranscriptItemIdentity(
            item_id=id_factory(),
            run_id=stored.run.run_id,
            session_id=session_id,
            transcript_sequence=next_transcript_sequence,
            created_at=repaired_at,
        )
        next_transcript_sequence += 1
        item_ids.append(result.item_id)
        return result

    if stored.run.status in _ACTIVE_RUNS:
        error = _interruption_error(id_factory)
        terminal_item = identity(None)
        run_sequence = 1 + max(
            (
                body.sequence
                for item, body in bodies
                if item.session_id is None and hasattr(body, "sequence")
            ),
            default=0,
        )
        event = RunFailedEvent(
            run_id=stored.run.run_id,
            sequence=run_sequence,
            occurred_at=repaired_at,
            payload=RunFailedPayload(failed_at=repaired_at, error=error),
        )
        transcript.append(terminal_item, "lifecycle_event", 2, event)
        snapshot = AnalysisRunSnapshot(
            run_id=stored.run.run_id,
            status=RunStatus.FAILED,
            created_at=stored.run.created_at,
            updated_at=repaired_at,
            configuration=created.payload.configuration,
        )
        mutation = RunStateMutation(
            mutation_id=id_factory(),
            previous_status=stored.run.status,
            snapshot=snapshot,
            transcript_item=terminal_item,
        )
        mutation_ids.append(mutation.mutation_id)
        run_mutations.append(mutation)

    for session in active_sessions:
        error = _interruption_error(id_factory)
        terminal_item = identity(session.session_id)
        session_sequence = 1 + max(
            (
                body.sequence
                for item, body in bodies
                if item.session_id == session.session_id and hasattr(body, "sequence")
            ),
            default=0,
        )
        event = SessionFailedEvent(
            run_id=stored.run.run_id,
            session_id=session.session_id,
            sequence=session_sequence,
            occurred_at=repaired_at,
            payload=SessionFailedPayload(failed_at=repaired_at, error=error),
        )
        transcript.append(terminal_item, "lifecycle_event", 2, event)
        summary = SessionSummary(
            session_id=session.session_id,
            agent_role=session.agent_role,
            target_comid=session.target_comid,
            status=AgentSessionStatus.FAILED,
            created_at=session.created_at,
            updated_at=repaired_at,
        )
        mutation = SessionStateMutation(
            mutation_id=id_factory(),
            run_id=stored.run.run_id,
            previous_status=session.status,
            summary=summary,
            transcript_item=terminal_item,
        )
        mutation_ids.append(mutation.mutation_id)
        session_mutations.append(mutation)

    group = PersistenceGroup(
        item_ids=tuple(item_ids),
        mutation_ids=tuple(mutation_ids),
    )
    return PersistenceWriteBatch(
        run_id=stored.run.run_id,
        groups=(group,),
        transcript=transcript.to_storage_batch(
            inline_content_bytes=inline_content_bytes,
            preview_bytes=preview_bytes,
        ),
        run_mutations=tuple(run_mutations),
        session_mutations=tuple(session_mutations),
    )


def _read_lifecycle_bodies(
    stored: StoredRunRead,
    registry: TranscriptRegistry,
) -> tuple[tuple[StoredTranscriptItem, object], ...]:
    external = {content.item_id: content.body for content in stored.contents}
    parsed_items: list[tuple[StoredTranscriptItem, object]] = []
    for item in stored.items:
        if item.content_type != "lifecycle_event":
            continue
        safe_body = item.inline_body
        if safe_body is None:
            safe_body = external.get(item.item_id)
        if safe_body is None:
            raise ValueError("a lifecycle transcript body is missing")
        parsed = registry.parse(item, safe_body)
        if parsed.parse_status is not ParseStatus.OK or parsed.body is None:
            raise ValueError("a lifecycle transcript item is unreadable")
        parsed_items.append((item, parsed.body))
    return tuple(parsed_items)


def _interruption_error(id_factory: Callable[[], UUID]) -> PublicError:
    return PublicError(
        error_id=id_factory(),
        code="service_interrupted",
        message="The analysis was interrupted by a service restart.",
        failed_stage="service_runtime",
        retryable=False,
    )

import asyncio
from dataclasses import dataclass, field
from uuid import UUID

from app.application.analysis_runs.models import (
    AnalysisPhase,
    AnalysisRunSnapshot,
    PublicError,
    RunLifecycleEvent,
    SessionLifecycleEvent,
    SessionSummary,
    StateMutation,
    TranscriptItemIdentity,
    TopologyRunResultSummary,
)
from app.application.analysis_runs.topology_result_models import DependencyStatus
from app.application.analysis_runs.persistence import (
    PersistenceGroup,
    PersistenceWriteBatch,
    RAGConsumptionLink,
)
from app.application.transcripts.buffer import TranscriptBuffer
from app.application.transcripts.models import TranscriptContentRef
from app.application.transcripts.stream_models import SessionProcessRecord, StreamingMessage
from app.application.analysis_runs.workflow import RunWorkflowBundle


@dataclass
class AgentSessionRuntime:
    summary: SessionSummary
    events: list[SessionLifecycleEvent] = field(default_factory=list)
    process_events: list[SessionProcessRecord] = field(default_factory=list)
    stop_requested: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    execution_claimed: bool = False
    last_error: PublicError | None = None


@dataclass
class RunRuntime:
    snapshot: AnalysisRunSnapshot
    transcript_buffer: TranscriptBuffer
    query: str = field(repr=False)
    workflow_bundle: RunWorkflowBundle | None = field(default=None, repr=False)
    analysis_phase: AnalysisPhase = AnalysisPhase.QUEUED
    context_ref: TranscriptContentRef | None = None
    topology_ref: TranscriptContentRef | None = None
    result_ref: TranscriptContentRef | None = None
    result_summary: TopologyRunResultSummary | None = None
    events: list[RunLifecycleEvent] = field(default_factory=list)
    transcript_items: list[TranscriptItemIdentity] = field(default_factory=list)
    state_mutations: list[StateMutation] = field(default_factory=list)
    agent_sessions: dict[UUID, AgentSessionRuntime] = field(default_factory=dict)
    query_parser_session_id: UUID | None = None
    run_summary_session_id: UUID | None = None
    waterbody_session_ids: dict[str, UUID] = field(default_factory=dict)
    waterbody_participation: dict[str, bool] = field(default_factory=dict)
    waterbody_ready: set[str] = field(default_factory=set)
    waterbody_dependency_statuses: dict[str, DependencyStatus] = field(
        default_factory=dict
    )
    streaming_messages: dict[UUID, StreamingMessage] = field(default_factory=dict)
    late_streaming_messages: dict[UUID, StreamingMessage] = field(default_factory=dict)
    suppressed_transcript_items: dict[UUID, TranscriptItemIdentity] = field(
        default_factory=dict
    )
    next_transcript_sequence: int = 1
    condition: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)
    cancel_requested: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    stop_requested: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    workflow_start: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    workflow_quiesced: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    history_deleted: bool = False
    workflow_commit_gate: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    workflow_task: asyncio.Task[None] | None = field(default=None, repr=False)
    last_error: PublicError | None = None
    persistence_groups: list[PersistenceGroup] = field(default_factory=list)
    rag_consumption_links: dict[UUID, tuple[RAGConsumptionLink, ...]] = field(
        default_factory=dict
    )
    persistence_active_batch: PersistenceWriteBatch | None = None
    persisted_item_ids: set[UUID] = field(default_factory=set)
    persisted_mutation_ids: set[UUID] = field(default_factory=set)
    persistence_failures: int = 0
    persistence_unknown: bool = False
    persistence_exhausted: bool = False
    persistence_warning: PublicError | None = None
    third_retry_failed_at: float | None = None
    writer_wakeup: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    writer_stop: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    writer_quiesced: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    writer_task: asyncio.Task[None] | None = field(default=None, repr=False)
    cleanup_eligible_at: float | None = field(default=None, repr=False)
    cleanup_task: asyncio.Task[None] | None = field(default=None, repr=False)

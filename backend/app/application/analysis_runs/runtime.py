import asyncio
from dataclasses import dataclass, field
from uuid import UUID

from app.application.analysis_runs.models import (
    AnalysisRunSnapshot,
    PublicError,
    RunLifecycleEvent,
    SessionLifecycleEvent,
    SessionSummary,
    StateMutation,
    TranscriptItemIdentity,
)


@dataclass
class AgentSessionRuntime:
    summary: SessionSummary
    events: list[SessionLifecycleEvent] = field(default_factory=list)
    stop_requested: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    execution_claimed: bool = False
    last_error: PublicError | None = None


@dataclass
class RunRuntime:
    snapshot: AnalysisRunSnapshot
    query: str = field(repr=False)
    events: list[RunLifecycleEvent] = field(default_factory=list)
    transcript_items: list[TranscriptItemIdentity] = field(default_factory=list)
    state_mutations: list[StateMutation] = field(default_factory=list)
    agent_sessions: dict[UUID, AgentSessionRuntime] = field(default_factory=dict)
    query_parser_session_id: UUID | None = None
    waterbody_session_ids: dict[str, UUID] = field(default_factory=dict)
    next_transcript_sequence: int = 1
    condition: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)
    cancel_requested: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    stop_requested: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    workflow_start: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    workflow_quiesced: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    workflow_task: asyncio.Task[None] | None = field(default=None, repr=False)
    last_error: PublicError | None = None

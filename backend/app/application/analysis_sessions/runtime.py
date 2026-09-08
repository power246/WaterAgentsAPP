import asyncio
from dataclasses import dataclass, field

from app.application.analysis_sessions.models import (
    AnalysisSessionSnapshot,
    LifecycleEvent,
    PublicError,
)


@dataclass
class SessionRuntime:
    snapshot: AnalysisSessionSnapshot
    query: str = field(repr=False)
    events: list[LifecycleEvent] = field(default_factory=list)
    condition: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)
    cancel_requested: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    workflow_quiesced: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    workflow_task: asyncio.Task[None] | None = field(default=None, repr=False)
    last_error: PublicError | None = None

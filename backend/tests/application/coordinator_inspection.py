"""Test-only access to coordinator state used by lifecycle assertions."""

from collections.abc import Mapping
from uuid import UUID

from app.application.analysis_runs.coordinator import RunCoordinator
from app.application.analysis_runs.models import (
    RunStateMutation,
    SessionStateMutation,
    TranscriptItemIdentity,
)


async def set_waterbody_participation(
    coordinator: RunCoordinator,
    run_id: UUID,
    participation: Mapping[str, bool],
) -> None:
    runtime = await coordinator._lookup(run_id)
    async with runtime.condition:
        runtime.waterbody_participation = dict(participation)
        runtime.condition.notify_all()


async def list_transcript_items(
    coordinator: RunCoordinator, run_id: UUID
) -> tuple[TranscriptItemIdentity, ...]:
    runtime = await coordinator._lookup(run_id)
    async with runtime.condition:
        return tuple(runtime.transcript_items)


async def list_session_transcript_items(
    coordinator: RunCoordinator,
    run_id: UUID,
    session_id: UUID,
) -> tuple[TranscriptItemIdentity, ...]:
    runtime = await coordinator._lookup(run_id)
    async with runtime.condition:
        coordinator._lookup_agent_session_locked(runtime, session_id)
        return tuple(
            item for item in runtime.transcript_items if item.session_id == session_id
        )


async def list_state_mutations(
    coordinator: RunCoordinator, run_id: UUID
) -> tuple[RunStateMutation | SessionStateMutation, ...]:
    runtime = await coordinator._lookup(run_id)
    async with runtime.condition:
        return tuple(runtime.state_mutations)

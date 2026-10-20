from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass
from uuid import UUID

from starlette.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from app.api.sse.encoder import KEEPALIVE_FRAME, encode_event
from app.application.analysis_runs.coordinator import (
    AgentSessionNotFoundError,
    RunCoordinator,
    RunNotFoundError,
)
from app.application.analysis_runs.models import SessionSummary
from app.application.transcripts.reader import TranscriptReader
from app.application.transcripts.stream_models import (
    MessageDeltaPayload,
    MessageDeltaProcessRecord,
    MessageFinalizedPayload,
    MessageFinalizedProcessRecord,
    MessageStartedPayload,
    MessageStartedProcessRecord,
    SessionProcessEvent,
    SessionProcessRecord,
    StreamEndEvent,
    TranscriptItemPayload,
    TranscriptItemProcessRecord,
)
from app.domain.domain_model import DomainModel


class LiveStreamReleasedError(LookupError):
    def __init__(self, run_id: UUID, history_url: str, session_id: UUID | None = None) -> None:
        self.run_id = run_id
        self.session_id = session_id
        self.history_url = history_url
        super().__init__("The live event stream has been released.")


@dataclass(frozen=True, slots=True)
class _Frame:
    event_name: str
    data: DomainModel
    event_id: int | None = None


class _EventStreamingResponse(StreamingResponse):
    def __init__(
        self,
        events: AsyncGenerator[_Frame | None, None],
        stream_end: StreamEndEvent,
    ) -> None:
        self._events = events
        self._stream_end = stream_end
        self._frames = self._encode_frames()
        super().__init__(
            self._frames,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def _encode_frames(self) -> AsyncGenerator[bytes, None]:
        async for frame in self._events:
            if frame is None:
                yield KEEPALIVE_FRAME
            else:
                yield encode_event(
                    frame.event_name,
                    frame.data,
                    event_id=frame.event_id,
                )
        yield encode_event("stream_end", self._stream_end)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async with aclosing(self._frames), aclosing(self._events):
            await super().__call__(scope, receive, send)


async def create_run_event_response(
    coordinator: RunCoordinator,
    reader: TranscriptReader,
    run_id: UUID,
) -> Response:
    try:
        snapshots = await coordinator.subscribe_run(run_id)
    except RunNotFoundError:
        await _classify_released_run(reader, run_id)
        raise AssertionError("release classification must raise")

    async def events() -> AsyncGenerator[_Frame | None, None]:
        async with aclosing(snapshots):
            async for snapshot in snapshots:
                yield None if snapshot is None else _Frame("run_snapshot", snapshot)

    return _EventStreamingResponse(
        events(),
        StreamEndEvent(run_id=run_id, stream="run"),
    )


async def create_session_event_response(
    coordinator: RunCoordinator,
    reader: TranscriptReader,
    run_id: UUID,
    session_id: UUID,
    *,
    last_event_id: str | None = None,
) -> Response:
    try:
        records = await coordinator.subscribe_session(
            run_id,
            session_id,
            after_sequence=last_event_id,
        )
        session = await coordinator.get_session(run_id, session_id)
    except RunNotFoundError:
        await _classify_released_session(reader, run_id, session_id)
        raise AssertionError("release classification must raise")

    async def events() -> AsyncGenerator[_Frame | None, None]:
        async with aclosing(records):
            async for record in records:
                if record is None:
                    yield None
                    continue
                event_name, payload = _session_payload(reader, session, record)
                yield _Frame(
                    event_name,
                    SessionProcessEvent(
                        run_id=run_id,
                        session_id=session_id,
                        sequence=record.sequence,
                        occurred_at=record.occurred_at,
                        payload=payload,
                    ),
                    record.sequence,
                )

    return _EventStreamingResponse(
        events(),
        StreamEndEvent(run_id=run_id, session_id=session_id, stream="session"),
    )


def _session_payload(
    reader: TranscriptReader,
    session: SessionSummary,
    record: SessionProcessRecord,
) -> tuple[str, DomainModel]:
    if isinstance(record, TranscriptItemProcessRecord):
        return "transcript_item", TranscriptItemPayload(
            item=reader.present_live_item(record.item, session)
        )
    if isinstance(record, MessageStartedProcessRecord):
        return "message_started", MessageStartedPayload(item=record.header)
    if isinstance(record, MessageDeltaProcessRecord):
        return "message_delta", MessageDeltaPayload(
            item_id=record.item_id,
            text=record.text,
        )
    if isinstance(record, MessageFinalizedProcessRecord):
        return "message_finalized", MessageFinalizedPayload(
            item=reader.present_live_item(record.item, session),
            generation_status=record.generation_status,
        )
    raise TypeError("unsupported session process event")


async def _classify_released_run(reader: TranscriptReader, run_id: UUID) -> None:
    if not await reader.persisted_run_exists(run_id):
        raise RunNotFoundError(run_id)
    raise LiveStreamReleasedError(
        run_id,
        f"/api/analysis/runs/{run_id}?view=history",
    )


async def _classify_released_session(
    reader: TranscriptReader,
    run_id: UUID,
    session_id: UUID,
) -> None:
    if not await reader.persisted_run_exists(run_id):
        raise RunNotFoundError(run_id)
    if not await reader.persisted_session_exists(run_id, session_id):
        raise AgentSessionNotFoundError(run_id, session_id)
    raise LiveStreamReleasedError(
        run_id,
        (
            f"/api/analysis/runs/{run_id}/sessions/{session_id}/transcript"
            "?view=history"
        ),
        session_id,
    )

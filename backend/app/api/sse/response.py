from collections.abc import AsyncGenerator
from contextlib import aclosing
from uuid import UUID

from starlette.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from app.api.sse.encoder import KEEPALIVE_FRAME, encode_event
from app.application.analysis_runs.coordinator import RunCoordinator
from app.application.analysis_runs.models import RunLifecycleEvent


class _EventStreamingResponse(StreamingResponse):
    def __init__(self, events: AsyncGenerator[RunLifecycleEvent | None, None]) -> None:
        self._events = events
        self._frames = self._encode_frames()
        super().__init__(
            self._frames, media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    async def _encode_frames(self) -> AsyncGenerator[bytes, None]:
        async for event in self._events:
            yield KEEPALIVE_FRAME if event is None else encode_event(event)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async with aclosing(self._frames), aclosing(self._events):
            await super().__call__(scope, receive, send)


async def create_event_response(
    coordinator: RunCoordinator,
    run_id: UUID | str,
    *,
    last_event_id: str | None = None,
) -> Response:
    events = await coordinator.subscribe(run_id, after_sequence=last_event_id)
    if events is None:
        return Response(status_code=204)
    return _EventStreamingResponse(events)

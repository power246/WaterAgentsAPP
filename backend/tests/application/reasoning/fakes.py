import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.application.reasoning.model_models import (
    WaterbodyModelEvent,
    WaterbodyModelRequest,
)
from app.ports import WaterbodyModelPortError


@dataclass(frozen=True, slots=True)
class ScriptedCancellation:
    event: asyncio.Event


class DeterministicWaterbodyModelPort:
    def __init__(
        self,
        *scripts: tuple[object, ...],
    ) -> None:
        self._scripts = scripts
        self.calls: list[WaterbodyModelRequest] = []
        self.emitted_events: list[object] = []

    async def stream(
        self,
        request: WaterbodyModelRequest,
    ) -> AsyncIterator[WaterbodyModelEvent]:
        call_index = len(self.calls)
        self.calls.append(request)
        if call_index >= len(self._scripts):
            raise AssertionError("waterbody model fake received an unscripted call")
        for step in self._scripts[call_index]:
            if isinstance(step, ScriptedCancellation):
                step.event.set()
                continue
            if isinstance(step, WaterbodyModelPortError):
                raise step
            self.emitted_events.append(step)
            yield step  # type: ignore[misc]

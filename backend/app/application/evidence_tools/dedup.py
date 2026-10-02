import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Generic, TypeVar


ValueT = TypeVar("ValueT")


@dataclass(slots=True)
class _RunState(Generic[ValueT]):
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    in_flight: dict[str, asyncio.Task[ValueT]] = field(default_factory=dict)
    successes: dict[str, ValueT] = field(default_factory=dict)


class RunLocalSingleFlight(Generic[ValueT]):
    """Shares exact in-flight work and retains successes until run cleanup."""

    def __init__(self) -> None:
        self._runs: dict[str, _RunState[ValueT]] = {}

    async def execute(
        self,
        run_id: str,
        canonical_key: str,
        work: Callable[[], Awaitable[ValueT]],
        *,
        cache_success: Callable[[ValueT], bool] = lambda _value: True,
    ) -> tuple[ValueT, bool]:
        state = self._runs.setdefault(run_id, _RunState())
        async with state.lock:
            cached = state.successes.get(canonical_key)
            if cached is not None:
                return cached, True
            task = state.in_flight.get(canonical_key)
            reused = task is not None
            if task is None:
                task = asyncio.create_task(work())
                state.in_flight[canonical_key] = task

        try:
            value = await asyncio.shield(task)
        except BaseException:
            async with state.lock:
                if state.in_flight.get(canonical_key) is task and task.done():
                    state.in_flight.pop(canonical_key, None)
            raise

        async with state.lock:
            if cache_success(value):
                state.successes.setdefault(canonical_key, value)
            if state.in_flight.get(canonical_key) is task:
                state.in_flight.pop(canonical_key, None)
        return value, reused

    async def cleanup_run(self, run_id: str) -> None:
        state = self._runs.pop(run_id, None)
        if state is None:
            return
        async with state.lock:
            tasks = tuple(state.in_flight.values())
            state.in_flight.clear()
            state.successes.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def live_counts(self, run_id: str) -> tuple[int, int]:
        state = self._runs.get(run_id)
        if state is None:
            return 0, 0
        return len(state.in_flight), len(state.successes)

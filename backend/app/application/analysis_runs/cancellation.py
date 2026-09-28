from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.application.analysis_runs.workflow import (
    CancellationToken,
    WorkflowStopped,
)


ResultT = TypeVar("ResultT")


async def await_with_cancellation(
    awaitable: Awaitable[ResultT],
    cancellation: CancellationToken,
    *,
    timeout: float | None = None,
    timeout_error: Callable[[], BaseException] = TimeoutError,
) -> ResultT:
    """Await owned work while making cancellation and timeout deterministic."""

    work = asyncio.ensure_future(awaitable)
    stopped = asyncio.create_task(cancellation.wait())
    try:
        done, _ = await asyncio.wait(
            (work, stopped),
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation.is_requested() or stopped in done:
            raise WorkflowStopped()
        if work not in done:
            raise timeout_error()
        return work.result()
    finally:
        for task in (work, stopped):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, stopped, return_exceptions=True)

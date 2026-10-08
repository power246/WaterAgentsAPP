import asyncio
import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.application.analysis_runs.coordinator import RunCoordinator
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from tests.m4_support import MODEL_CATALOG, create_request, create_request_bytes


class _Clock:
    def now(self):
        return datetime(2026, 10, 8, tzinfo=UTC)


class _Workflow:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, context):
        self.started.set()
        await self.release.wait()
        context.cancellation.checkpoint()


class _Bundle:
    def __init__(self):
        self.workflow = _Workflow()
        self.run_id = None
        self.closes = 0

    def bind_run(self, run_id):
        assert self.run_id is None
        self.run_id = run_id

    async def aclose(self):
        self.closes += 1


def test_coordinator_closes_uncommitted_completed_and_shutdown_bundles():
    async def exercise():
        bundles = []

        async def factory(configuration):
            bundle = _Bundle()
            bundles.append(bundle)
            return bundle

        coordinator = RunCoordinator(
            _Clock(), transcript_store=InMemoryTranscriptStore(),
            model_catalog=MODEL_CATALOG, workflow=None,
            workflow_factory=factory, shutdown_grace_period=0,
        )
        try:
            bad = create_request()
            bad["parser_model"] = {"provider": "unsupported", "model": "missing"}
            with pytest.raises(ValidationError):
                await coordinator.create_run(json.dumps(bad).encode())
            assert bundles[0].closes == 1
            assert bundles[0].run_id is None
            assert coordinator._runs == {}

            first = await coordinator.create_run(create_request_bytes("first"))
            await asyncio.wait_for(bundles[1].workflow.started.wait(), 2)
            assert bundles[1].run_id == first.run_id
            bundles[1].workflow.release.set()
            await asyncio.wait_for(coordinator._runs[first.run_id].workflow_task, 2)
            assert bundles[1].closes == 1

            second = await coordinator.create_run(create_request_bytes("second"))
            await asyncio.wait_for(bundles[2].workflow.started.wait(), 2)
            await coordinator.shutdown()
            assert bundles[2].run_id == second.run_id
            assert bundles[2].closes == 1
        finally:
            await coordinator.shutdown()

    asyncio.run(exercise())

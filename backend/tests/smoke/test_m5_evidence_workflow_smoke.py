import asyncio

from app.application.evidence_tools import (
    EvidenceAcquisitionService,
    EvidenceToolExecutor,
    create_evidence_tool_registry,
)
from app.infrastructure.config import load_parameter_capability_registry
from tests.application.evidence_tools.conftest import make_scope
from tests.application.evidence_tools.fakes import (
    FakeCollectionPort,
    FixedClock,
    MemoryEvidenceStore,
    SequentialIds,
    complete_batch,
)


def test_station_backed_empty_preflight_stops_before_any_model_call() -> None:
    async def exercise() -> None:
        scope = make_scope(run_id="run-smoke", session_id="session-smoke")
        store = MemoryEvidenceStore()
        capabilities = load_parameter_capability_registry()
        registry = create_evidence_tool_registry(capabilities)
        executor = EvidenceToolExecutor(
            registry=registry,
            capability_registry=capabilities,
            writer=store,
            reader=store,
            water_port=FakeCollectionPort(lambda plan: complete_batch(plan)),
            clock=FixedClock(),
            operation_id_factory=SequentialIds(),
        )
        service = EvidenceAcquisitionService(
            registry=registry,
            executor=executor,
            reader=store,
            writer=store,
            capability_registry=capabilities,
            clock=FixedClock(),
        )

        handoff = await service.start(scope)
        model_calls = 0
        if not handoff.stop_before_model:
            model_calls += 1

        assert handoff.stop_before_model
        assert model_calls == 0
        assert len(store.calls) == 1
        assert len(store.results) == 1
        assert store.fragments
        assert len(store.bundles) == 1
        assert store.bundles[0][1].tool_result_refs

    asyncio.run(exercise())

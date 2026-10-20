import asyncio

from app.application.evidence_tools import (
    EvidenceToolExecutor,
    RunLocalSingleFlight,
    canonical_request_key,
    create_evidence_tool_registry,
)
from app.domain import (
    CollectionStatus,
    EvidenceToolExecutionStatus,
    EvidenceToolOptions,
    EvidenceToolRequest,
)
from app.infrastructure.config import load_parameter_capability_registry
from tests.application.evidence_tools.conftest import (
    WEATHER_PARAMETER,
    make_weather_context,
)
from tests.application.evidence_tools.fakes import (
    FakeCollectionPort,
    FixedClock,
    MemoryEvidenceStore,
    SequentialIds,
    complete_batch,
)


def weather_request(scope, call_id, *, include_forecast=None):
    return EvidenceToolRequest(
        tool_call_id=call_id,
        session_id=scope.session_id,
        tool_name="collect_weather_evidence",
        purpose="bounded weather context",
        window_ids=(scope.time_windows[0].window_id,),
        parameter_keys=(WEATHER_PARAMETER,),
        options=EvidenceToolOptions(include_forecast=include_forecast),
    )


def runtime(store, port, *, single_flight=None):
    capabilities = load_parameter_capability_registry()
    return EvidenceToolExecutor(
        registry=create_evidence_tool_registry(capabilities),
        capability_registry=capabilities,
        writer=store,
        reader=store,
        weather_port=port,
        clock=FixedClock(),
        operation_id_factory=SequentialIds(),
        single_flight=single_flight,
    )


def test_concurrent_exact_request_runs_provider_once_but_writes_two_results(
    scope_factory,
) -> None:
    async def exercise() -> None:
        evidence_scope = scope_factory(context=make_weather_context())
        store = MemoryEvidenceStore()
        port = FakeCollectionPort(
            lambda provider_request: complete_batch(provider_request),
            gated=True,
        )
        executor = runtime(store, port)
        first_task = asyncio.create_task(
            executor.execute(evidence_scope, weather_request(evidence_scope, "call-1"))
        )
        await port.started.wait()
        second_task = asyncio.create_task(
            executor.execute(evidence_scope, weather_request(evidence_scope, "call-2"))
        )
        await asyncio.sleep(0)
        port.release.set()
        first, second = await asyncio.gather(first_task, second_task)

        assert len(port.calls) == 1
        assert len(store.calls) == 2
        assert len(store.results) == 2
        originals = [item for item in (first, second) if not item.deduplicated]
        reused = [item for item in (first, second) if item.deduplicated]
        assert len(originals) == len(reused) == 1
        assert reused[0].operation_id != originals[0].operation_id
        assert reused[0].deduplicated_from_operation_id == originals[0].operation_id
        assert reused[0].deduplicated_from_result_ref.item_id == "result-item-001"
        assert executor.live_dedup_counts(evidence_scope.run_id) == (0, 1)
        await executor.cleanup_run(evidence_scope.run_id)
        assert executor.live_dedup_counts(evidence_scope.run_id) == (0, 0)

    asyncio.run(exercise())


def test_options_run_and_manifest_digest_do_not_cross_dedup_boundaries(
    scope_factory,
) -> None:
    async def exercise() -> None:
        store = MemoryEvidenceStore()
        port = FakeCollectionPort(lambda provider_request: complete_batch(provider_request))
        executor = runtime(store, port)
        context = make_weather_context()
        first_scope = scope_factory(run_id="run-a", session_id="session-a", context=context)
        other_session = scope_factory(
            run_id="run-a", session_id="session-other", context=context,
        )
        second_scope = scope_factory(run_id="run-b", session_id="session-b", context=context)

        await executor.execute(first_scope, weather_request(first_scope, "call-a1"))
        await executor.execute(
            first_scope,
            weather_request(first_scope, "call-a2", include_forecast=True),
        )
        await executor.execute(second_scope, weather_request(second_scope, "call-b1"))
        await executor.execute(
            other_session,
            weather_request(other_session, "call-a-other-session"),
        )

        assert len(port.calls) == 5
        assert all(not result.deduplicated for _, result in store.results)

        base = weather_request(first_scope, "key-1")
        first_key = canonical_request_key(
            first_scope,
            base,
            policy_manifest_digest="digest-a",
        )
        second_key = canonical_request_key(
            first_scope,
            base,
            policy_manifest_digest="digest-b",
        )
        assert first_key != second_key

    asyncio.run(exercise())


def test_failed_and_cancelled_work_is_retryable_and_never_cached(
    scope_factory,
) -> None:
    async def exercise() -> None:
        context = make_weather_context()
        scope = scope_factory(context=context)
        failed_store = MemoryEvidenceStore()
        failed_port = FakeCollectionPort(
            lambda provider_request: complete_batch(provider_request, complete=False)
        )
        failed_runtime = runtime(failed_store, failed_port)
        first = await failed_runtime.execute(scope, weather_request(scope, "failed-1"))
        second = await failed_runtime.execute(scope, weather_request(scope, "failed-2"))

        assert len(failed_port.calls) == 2
        assert first.execution_status is EvidenceToolExecutionStatus.FAILED
        assert second.execution_status is EvidenceToolExecutionStatus.FAILED
        assert first.collection_status is CollectionStatus.UNAVAILABLE
        assert not first.deduplicated and not second.deduplicated
        assert failed_runtime.live_dedup_counts(scope.run_id) == (0, 0)

        cancel_event = asyncio.Event()
        cancelled_scope = scope_factory(cancel_event=cancel_event, context=context)
        cancelled_store = MemoryEvidenceStore()
        cancelled_port = FakeCollectionPort(
            lambda provider_request: complete_batch(provider_request),
            gated=True,
        )
        cancelled_runtime = runtime(cancelled_store, cancelled_port)
        task = asyncio.create_task(
            cancelled_runtime.execute(
                cancelled_scope,
                weather_request(cancelled_scope, "cancelled-1"),
            )
        )
        await cancelled_port.started.wait()
        cancel_event.set()
        cancelled_port.release.set()
        cancelled = await task

        assert cancelled.execution_status is EvidenceToolExecutionStatus.CANCELLED
        assert cancelled.safe_error.code == "evidence_cancelled"
        assert cancelled_store.records == {}
        retry_scope = scope_factory(context=context)
        retried = await cancelled_runtime.execute(
            retry_scope,
            weather_request(retry_scope, "cancelled-2"),
        )
        assert retried.execution_status is EvidenceToolExecutionStatus.COMPLETED
        assert len(cancelled_port.calls) == 2
        assert not retried.deduplicated

    asyncio.run(exercise())


def test_single_flight_cleanup_cancels_live_work() -> None:
    async def exercise() -> None:
        single_flight: RunLocalSingleFlight[str] = RunLocalSingleFlight()
        started = asyncio.Event()

        async def work() -> str:
            started.set()
            await asyncio.Event().wait()
            return "unreachable"

        task = asyncio.create_task(single_flight.execute("run", "key", work))
        await started.wait()
        assert single_flight.live_counts("run") == (1, 0)
        await single_flight.cleanup_run("run")
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert single_flight.live_counts("run") == (0, 0)

    asyncio.run(exercise())

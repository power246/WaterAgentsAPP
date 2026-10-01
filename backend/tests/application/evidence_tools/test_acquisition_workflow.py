import asyncio
from datetime import timedelta

import pytest

from app.application.evidence_tools import (
    EvidenceAcquisitionService,
    EvidenceProducerStatus,
    EvidenceScopeViolationError,
    EvidenceToolExecutor,
    create_evidence_tool_registry,
)
from app.domain import (
    DischargeObservationRecord,
    EvidenceCellStatus,
    EvidenceLocation,
    EvidenceToolOptions,
    EvidenceToolRequest,
    EvidenceToolExecutionStatus,
    ItemReference,
    Provenance,
    ProviderQuality,
    ResultSource,
    UpstreamResultRecord,
    WaterObservationRecord,
)
from app.infrastructure.config import load_parameter_capability_registry
from tests.application.evidence_tools.fakes import (
    FakeCollectionPort,
    FixedClock,
    MemoryEvidenceStore,
    SequentialIds,
    complete_batch,
)


def _runtime(
    store,
    *,
    water_port=None,
    weather_port=None,
    facility_port=None,
    upstream_port=None,
    upstream_ready=False,
):
    capabilities = load_parameter_capability_registry()
    registry = create_evidence_tool_registry(
        capabilities,
        producer_statuses=(
            {"get_upstream_results": EvidenceProducerStatus.READY}
            if upstream_ready
            else None
        ),
    )
    executor = EvidenceToolExecutor(
        registry=registry,
        capability_registry=capabilities,
        writer=store,
        reader=store,
        water_port=water_port,
        weather_port=weather_port,
        facility_port=facility_port,
        upstream_port=upstream_port,
        clock=FixedClock(),
        operation_id_factory=SequentialIds(),
    )
    return EvidenceAcquisitionService(
        registry=registry,
        executor=executor,
        reader=store,
        writer=store,
        clock=FixedClock(),
    )


def _water_records(scope, plan, *, include=True, invalid_unit=False):
    if not include:
        return ()
    registry = load_parameter_capability_registry()
    parameter = plan.parameter_keys[0]
    capability = registry.get(parameter)
    policy = registry.get_channel_policy(parameter, plan.provider, plan.channel)
    assert capability is not None and policy is not None
    record_type = (
        DischargeObservationRecord
        if parameter == scope.supporting_parameters[0]
        else WaterObservationRecord
    )
    return tuple(
        record_type(
            provider=plan.provider.value,
            channel=plan.channel,
            source_uri="https://example.test/public",
            retrieved_at=scope.query_envelope.end,
            observed_time=window.time_range.start + timedelta(minutes=5),
            location=EvidenceLocation(
                point=None,
                comid=scope.agent_comid,
                station_id=plan.station_id,
                distance_to_target_km=0,
            ),
            provider_quality=ProviderQuality(approval="approved"),
            raw_unit=("unknown-unit" if invalid_unit else capability.canonical_unit),
            provenance=Provenance(
                operation_id=plan.operation_id,
                request_identity=f"request:{plan.operation_id}",
                page_identity=f"page:{plan.operation_id}",
                provider_series_id=plan.series_refs[0],
                provider_record_id=f"{plan.operation_id}:{window.window_id}",
            ),
            parameter=parameter,
            value="7",
            medium=policy.medium_constraint,
            fraction=policy.fraction_constraint,
            method=policy.accepted_methods[0],
            statistic=policy.accepted_statistics[0],
        )
        for window in scope.time_windows
    )


def _water_port(scope, *, mode="full"):
    def factory(plan):
        include = mode in {"full", "incomplete"} or (
            mode == "partial" and plan.parameter_keys[0] == scope.requested_parameters[0]
        ) or mode == "invalid"
        return complete_batch(
            plan,
            _water_records(
                scope,
                plan,
                include=include,
                invalid_unit=mode == "invalid",
            ),
            complete=mode != "incomplete",
        )

    return FakeCollectionPort(factory)


def _optional_request(scope, tool_name, *, call_id, options=None):
    return EvidenceToolRequest(
        tool_call_id=call_id,
        session_id=scope.session_id,
        tool_name=tool_name,
        purpose=f"Optional {tool_name} evidence.",
        window_ids=tuple(item.window_id for item in scope.time_windows),
        parameter_keys=scope.requested_parameters,
        options=options or EvidenceToolOptions(),
    )


def _upstream_port(scope, store, *, result_kind):
    def factory(plan):
        records = []
        for window_id in plan.affected_window_ids:
            window = next(item for item in scope.time_windows if item.window_id == window_id)
            for parameter in plan.parameter_keys:
                suffix = f"{parameter.code}-{window_id}"
                result_ref = ItemReference(item_id=f"upstream-result-{suffix}")
                closure_ref = ItemReference(item_id=f"upstream-bundle-{suffix}")
                store.seed_scope(
                    result_ref.item_id,
                    run_id=scope.run_id,
                    session_id="session-upstream",
                )
                store.seed_scope(
                    closure_ref.item_id,
                    run_id=scope.run_id,
                    session_id="session-upstream",
                )
                records.append(
                    UpstreamResultRecord(
                        provider="analysis_run",
                        channel="upstream_result",
                        source_uri=None,
                        retrieved_at=scope.query_envelope.end,
                        valid_time_range=window.time_range,
                        location=EvidenceLocation(
                            point=None,
                            comid="200",
                            station_id=None,
                            distance_to_target_km=1,
                        ),
                        provider_quality=ProviderQuality(),
                        provenance=Provenance(
                            operation_id=plan.operation_id,
                            page_identity=f"page:{plan.operation_id}",
                            provider_record_id=f"upstream:{suffix}",
                        ),
                        upstream_session_id="session-upstream",
                        result_ref=result_ref,
                        parameter=parameter,
                        window_id=window_id,
                        result_kind=result_kind,
                        result_source=ResultSource.INFERRED,
                        confidence=(0.7 if result_kind != "indeterminate" else None),
                        evidence_closure_refs=(closure_ref,),
                    )
                )
        return complete_batch(plan, tuple(records))

    return FakeCollectionPort(factory)


def test_station_backed_full_partial_empty_and_quality_invalid_paths(scope_factory):
    async def exercise():
        full_scope = scope_factory(run_id="run-full", session_id="session-full")
        full_store = MemoryEvidenceStore()
        full = await _runtime(
            full_store,
            water_port=_water_port(full_scope),
        ).acquire(full_scope)
        assert not full.stop_before_model
        assert {item.status for item in full.cell_gates} == {
            EvidenceCellStatus.SUPPORTED
        }
        assert len(full_store.calls) == 1
        assert len(full_store.results) == 1
        assert len(full_store.bundles) == 1
        assert not hasattr(full, "inference_result")

        optional = FakeCollectionPort(lambda plan: complete_batch(plan))
        partial_scope = scope_factory(
            run_id="run-partial",
            session_id="session-partial",
        )
        partial_store = MemoryEvidenceStore()
        partial = await _runtime(
            partial_store,
            water_port=_water_port(partial_scope, mode="partial"),
            weather_port=optional,
        ).acquire(
            partial_scope,
            optional_requests=(
                _optional_request(
                    partial_scope,
                    "collect_weather_evidence",
                    call_id="weather-partial",
                ),
            ),
        )
        assert not partial.stop_before_model
        assert optional.calls
        assert {
            item.discharge_status for item in partial.cell_gates
        } == {EvidenceCellStatus.UNSUPPORTED}

        for mode in ("empty", "invalid"):
            scope = scope_factory(run_id=f"run-{mode}", session_id=f"session-{mode}")
            store = MemoryEvidenceStore()
            skipped_optional = FakeCollectionPort(lambda plan: complete_batch(plan))
            handoff = await _runtime(
                store,
                water_port=_water_port(scope, mode=mode),
                weather_port=skipped_optional,
            ).acquire(
                scope,
                optional_requests=(
                    _optional_request(
                        scope,
                        "collect_weather_evidence",
                        call_id=f"weather-{mode}",
                    ),
                ),
            )
            model_calls = 0
            if not handoff.stop_before_model:
                model_calls += 1
            assert handoff.stop_before_model
            assert handoff.stop_facts == (
                "station_backed_no_usable_direct_evidence",
            )
            assert model_calls == 0
            assert skipped_optional.calls == []
            assert store.calls and store.results and store.fragments and store.bundles
            assert all(
                fragment.source != "topology_ready"
                for fragment in store.fragments.values()
            )

    asyncio.run(exercise())


def test_station_backed_technical_failure_and_incomplete_are_not_natural_empty(
    scope_factory,
):
    async def exercise():
        for mode, expected in (
            ("incomplete", "evidence_collection_incomplete"),
            ("transport", "evidence_provider_response_invalid"),
        ):
            scope = scope_factory(run_id=f"run-{mode}", session_id=f"session-{mode}")
            store = MemoryEvidenceStore()
            if mode == "transport":
                def fail(_plan):
                    raise RuntimeError("private provider detail")

                port = FakeCollectionPort(fail)
            else:
                port = _water_port(scope, mode=mode)
            handoff = await _runtime(store, water_port=port).acquire(scope)
            assert not handoff.stop_before_model
            assert expected in handoff.block_facts
            assert {item.status for item in handoff.cell_gates} == {
                EvidenceCellStatus.BLOCKED
            }
            assert len(store.bundles) == 1
            assert all(
                result.execution_status is not EvidenceToolExecutionStatus.COMPLETED
                for _, result in store.results
            )

    asyncio.run(exercise())


def test_station_backed_cancellation_returns_auditable_technical_handoff(scope_factory):
    async def exercise():
        cancel_event = asyncio.Event()
        scope = scope_factory(
            run_id="run-cancel",
            session_id="session-cancel",
            cancel_event=cancel_event,
        )
        store = MemoryEvidenceStore()
        class CancelAfterConfirmedBatch:
            def __init__(self):
                self.calls = []
                self.started = asyncio.Event()

            async def collect(self, plan, *, deadline=None):
                self.calls.append((plan, deadline))
                if len(self.calls) == 1:
                    return complete_batch(
                        plan,
                        _water_records(scope, plan),
                    )
                self.started.set()
                await asyncio.Event().wait()

        water = CancelAfterConfirmedBatch()
        task = asyncio.create_task(
            _runtime(store, water_port=water).acquire(scope)
        )
        await water.started.wait()
        cancel_event.set()
        handoff = await task
        assert not handoff.stop_before_model
        assert handoff.block_facts == ("evidence_cancelled",)
        assert {item.status for item in handoff.cell_gates} == {
            EvidenceCellStatus.BLOCKED
        }
        assert len(store.calls) == 1
        assert len(store.results) == 1
        assert len(store.bundles) == 1
        assert store.records
        result = store.results[0][1]
        assert result.source_record_refs
        assert result.collection_status.value == "unavailable"

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("result_kind", "expected"),
    [
        ("quantified", EvidenceCellStatus.SUPPORTED),
        ("indeterminate", EvidenceCellStatus.UNSUPPORTED),
    ],
)
def test_stationless_consumes_only_upstream_and_indeterminate_never_unlocks_gate(
    scope_factory,
    result_kind,
    expected,
):
    async def exercise():
        scope = scope_factory(
            run_id=f"run-upstream-{result_kind}",
            session_id=f"session-downstream-{result_kind}",
            with_station=False,
            with_upstream=True,
        )
        store = MemoryEvidenceStore()
        upstream = _upstream_port(scope, store, result_kind=result_kind)
        water = FakeCollectionPort(lambda plan: complete_batch(plan))
        handoff = await _runtime(
            store,
            water_port=water,
            upstream_port=upstream,
            upstream_ready=True,
        ).acquire(scope)
        assert water.calls == []
        assert upstream.calls
        assert "collect_water_observations" not in {
            item.name for item in handoff.tool_roster
        }
        assert {item.status for item in handoff.cell_gates} == {expected}
        if result_kind == "indeterminate":
            assert all(
                fragment.relation_id is None
                for fragment in store.fragments.values()
            )

    asyncio.run(exercise())


def test_stationless_unavailable_upstream_allows_bounded_optional_context(
    scope_factory,
):
    async def exercise():
        scope = scope_factory(
            run_id="run-upstream-unavailable",
            session_id="session-upstream-unavailable",
            with_station=False,
            with_upstream=True,
        )
        store = MemoryEvidenceStore()
        weather = FakeCollectionPort(lambda plan: complete_batch(plan))
        facility = FakeCollectionPort(lambda plan: complete_batch(plan))
        handoff = await _runtime(
            store,
            weather_port=weather,
            facility_port=facility,
        ).acquire(
            scope,
            optional_requests=(
                _optional_request(
                    scope,
                    "collect_weather_evidence",
                    call_id="weather-unavailable-upstream",
                ),
                _optional_request(
                    scope,
                    "collect_facility_evidence",
                    call_id="facility-unavailable-upstream",
                    options=EvidenceToolOptions(facility_radius_miles=20),
                ),
            ),
        )
        assert weather.calls and facility.calls
        assert "evidence_producer_unavailable" in handoff.block_facts
        assert "get_upstream_results" not in {
            item.name for item in handoff.tool_roster
        }
        assert "collect_water_observations" not in {
            item.name for item in handoff.tool_roster
        }
        assert {item.status for item in handoff.cell_gates} == {
            EvidenceCellStatus.BLOCKED
        }
        assert all(
            not cell.direct_fragment_refs
            and not cell.allowed_indirect_fragment_refs
            for cell in handoff.cells
        )

    asyncio.run(exercise())


def test_acquisition_rejects_non_optional_requests_before_execution(evidence_scope):
    async def exercise():
        store = MemoryEvidenceStore()
        service = _runtime(store, water_port=_water_port(evidence_scope))
        request = EvidenceToolRequest(
            tool_call_id="not-optional",
            session_id=evidence_scope.session_id,
            tool_name="collect_water_observations",
            purpose="Attempt to bypass acquisition policy.",
            window_ids=(evidence_scope.time_windows[0].window_id,),
            parameter_keys=evidence_scope.requested_parameters,
        )
        with pytest.raises(EvidenceScopeViolationError):
            await service.acquire(evidence_scope, optional_requests=(request,))
        assert store.calls == []

    asyncio.run(exercise())

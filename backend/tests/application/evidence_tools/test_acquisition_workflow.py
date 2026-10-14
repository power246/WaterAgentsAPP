import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest

from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    SessionResultReference,
    SessionSummary,
    WaterbodySessionResultSummary,
)
from app.application.evidence_tools import (
    EvidenceAcquisitionService,
    EvidenceProducerStatus,
    EvidenceScopeViolationError,
    EvidenceToolExecutor,
    create_evidence_tool_registry,
)
from app.application.query_understanding import (
    EvidenceInferenceStrength,
    EvidenceRuleOrigin,
    EvidenceSpatialRelation,
    EvidenceTemporalRelation,
    create_evidence_rule,
    create_evidence_rule_plan,
)
from app.domain import (
    DischargeObservationRecord,
    EvidenceCellRole,
    EvidenceCellStatus,
    EvidenceLocation,
    EvidenceToolOptions,
    EvidenceToolRequest,
    EvidenceToolExecutionStatus,
    ItemReference,
    ParameterKey,
    Provenance,
    ProviderQuality,
    ResultSource,
    RelationSupportPayload,
    UpstreamResultRecord,
    WaterObservationRecord,
    WeatherObservationRecord,
)
from app.infrastructure.config import load_parameter_capability_registry
from tests.application.evidence_tools.fakes import (
    FakeCollectionPort,
    FixedClock,
    MemoryEvidenceStore,
    SequentialIds,
    complete_batch,
)
from tests.application.evidence_tools.conftest import (
    SUPPORT_PARAMETER,
    make_discharge_context,
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
        capability_registry=capabilities,
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
        if parameter == SUPPORT_PARAMETER
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


def _upstream_port(
    scope,
    store,
    *,
    result_kind,
    record_comid=None,
    session_comid=None,
    session_status=AgentSessionStatus.COMPLETED,
    reference_session_id=None,
    record_parameter=None,
    record_window_id=None,
    mismatched_time=False,
    duplicate_closure=False,
):
    upstream_session_id = "00000000-0000-0000-0000-000000000200"
    upstream_result_item_id = "00000000-0000-0000-0000-000000000300"
    upstream_comid = scope.direct_upstream_comids[0]
    reference_session_id = reference_session_id or upstream_session_id
    target_comid = session_comid or upstream_comid
    completed = session_status is AgentSessionStatus.COMPLETED
    store.seed_session(
        SessionSummary(
            session_id=UUID(upstream_session_id),
            agent_role=AgentRole.WATERBODY,
            target_comid=target_comid,
            status=session_status,
            created_at=scope.query_envelope.start,
            updated_at=scope.query_envelope.end,
            result_ref=(
                SessionResultReference(
                    run_id=UUID("00000000-0000-0000-0000-000000000100"),
                    item_id=UUID(upstream_result_item_id),
                )
                if completed
                else None
            ),
            result_summary=(
                WaterbodySessionResultSummary(
                    result_id=f"result:{upstream_comid}",
                    target_comid=target_comid,
                    risk_level="indeterminate",
                    availability="indeterminate",
                    overall_confidence=0.0,
                )
                if completed
                else None
            ),
        )
    )

    def factory(plan):
        records = []
        for window_id in plan.affected_window_ids:
            window = next(item for item in scope.time_windows if item.window_id == window_id)
            for parameter in plan.parameter_keys:
                suffix = f"{parameter.code}-{window_id}"
                result_ref = ItemReference(item_id=upstream_result_item_id)
                closure_ref = ItemReference(item_id=f"upstream-bundle-{suffix}")
                store.seed_scope(
                    result_ref.item_id,
                    run_id=scope.run_id,
                    session_id=reference_session_id,
                )
                store.seed_scope(
                    closure_ref.item_id,
                    run_id=scope.run_id,
                    session_id=reference_session_id,
                )
                records.append(
                    UpstreamResultRecord(
                        provider="analysis_run",
                        channel="upstream_result",
                        source_uri=None,
                        retrieved_at=scope.query_envelope.end,
                        valid_time_range=(
                            scope.query_envelope
                            if mismatched_time
                            else window.time_range
                        ),
                        location=EvidenceLocation(
                            point=None,
                            comid=record_comid or upstream_comid,
                            station_id=None,
                            distance_to_target_km=1,
                        ),
                        provider_quality=ProviderQuality(),
                        provenance=Provenance(
                            operation_id=plan.operation_id,
                            page_identity=f"page:{plan.operation_id}",
                            provider_record_id=f"upstream:{suffix}",
                        ),
                        upstream_session_id=upstream_session_id,
                        result_ref=result_ref,
                        parameter=record_parameter or parameter,
                        window_id=record_window_id or window_id,
                        result_kind=result_kind,
                        result_source=ResultSource.INFERRED,
                        confidence=(0.7 if result_kind != "indeterminate" else None),
                        evidence_closure_refs=(
                            (result_ref,) if duplicate_closure else (closure_ref,)
                        ),
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
        ).start(full_scope)
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
        partial_service = _runtime(
            partial_store,
            water_port=_water_port(partial_scope, mode="partial"),
            weather_port=optional,
        )
        partial = await partial_service.start(partial_scope)
        partial = await partial_service.execute_optional(
            partial_scope,
            (
                _optional_request(
                    partial_scope,
                    "collect_weather_evidence",
                    call_id="weather-partial",
                ),
            ),
            partial,
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
            ).start(scope)
            model_calls = 0
            if not handoff.stop_before_model:
                model_calls += 1
            assert handoff.stop_before_model
            assert handoff.stop_facts == (
                "no_usable_local_or_upstream_evidence",
            )
            assert model_calls == 0
            assert skipped_optional.calls == []
            assert store.calls and store.results and store.fragments and store.bundles
            assert all(
                fragment.source != "topology_ready"
                for fragment in store.fragments.values()
            )

    asyncio.run(exercise())


@pytest.mark.parametrize("include_oxygen", [False, True])
def test_discharge_target_builds_a_runnable_bundle_without_support_cells(
    scope_factory, include_oxygen,
) -> None:
    async def exercise() -> None:
        scope = scope_factory(context=make_discharge_context(
            include_oxygen=include_oxygen,
        ))
        store = MemoryEvidenceStore()
        handoff = await _runtime(store, water_port=_water_port(scope)).start(scope)

        assert handoff.bundle.supporting_parameters == ()
        assert handoff.bundle.requested_parameters[0] == SUPPORT_PARAMETER
        assert all(cell.role is EvidenceCellRole.TARGET for cell in handoff.cells)
        assert all(gate.status is EvidenceCellStatus.SUPPORTED for gate in handoff.cell_gates)
        assert not handoff.stop_before_model

    asyncio.run(exercise())


def test_station_backed_missing_target_uses_local_discharge_and_usable_upstream(
    scope_factory,
) -> None:
    async def exercise() -> None:
        scope = scope_factory(
            run_id="00000000-0000-0000-0000-000000000100",
            session_id="00000000-0000-0000-0000-000000000500",
            with_upstream=True,
        )
        store = MemoryEvidenceStore()

        def local_discharge_only(plan):
            include = plan.parameter_keys[0] == scope.supporting_parameters[0]
            return complete_batch(
                plan,
                _water_records(scope, plan, include=include),
            )

        local = FakeCollectionPort(local_discharge_only)
        upstream = _upstream_port(
            scope,
            store,
            result_kind="directional",
        )
        handoff = await _runtime(
            store,
            water_port=local,
            upstream_port=upstream,
            upstream_ready=True,
        ).start(scope)

        assert local.calls
        assert upstream.calls
        assert handoff.stop_before_model is False
        target_cells = tuple(
            cell for cell in handoff.cells if cell.role is EvidenceCellRole.TARGET
        )
        discharge_cells = tuple(
            cell
            for cell in handoff.cells
            if cell.role is EvidenceCellRole.REQUIRED_SUPPORT
        )
        assert all(not cell.direct_fragment_refs for cell in target_cells)
        assert all(cell.direct_fragment_refs for cell in discharge_cells)

    asyncio.run(exercise())


def test_required_rain_and_discharge_rule_materializes_directional_support(
    scope_factory,
    context_factory,
):
    async def exercise():
        registry = load_parameter_capability_registry()
        precipitation = registry.exact_matches("precipitation")[0].key
        base_context = context_factory(
            decision_instant=datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
        )
        rule = create_evidence_rule(
            output_parameter=base_context.params_used[0].key,
            input_parameters=(precipitation, base_context.supporting_params[0].key),
            temporal_relation=EvidenceTemporalRelation.SAME_WINDOW,
            spatial_relation=EvidenceSpatialRelation.SAME_WATERBODY,
            maximum_inference_strength=EvidenceInferenceStrength.DIRECTIONAL,
            required_collection_attempt=True,
            rationale="Rainfall plus discharge directionally supports the target.",
            origin=EvidenceRuleOrigin.QUERY_PARSER,
        )
        context = base_context.model_copy(
            update={"evidence_rule_plan": create_evidence_rule_plan((rule,))}
        )
        scope = scope_factory(
            run_id="run-rain-flow-rule",
            session_id="session-rain-flow-rule",
            context=context,
        )

        def weather_batch(plan):
            window = scope.time_windows[0]
            record = WeatherObservationRecord(
                provider="nws",
                channel="observation",
                source_uri="https://weather.example.test/observation",
                retrieved_at=scope.query_envelope.end,
                observed_time=window.time_range.start + timedelta(minutes=5),
                location=EvidenceLocation(
                    point=scope.target_node.point,
                    comid=None,
                    station_id="NWS-TEST",
                    distance_to_target_km=0,
                ),
                provider_quality=ProviderQuality(),
                raw_unit="mm",
                provenance=Provenance(
                    operation_id=plan.operation_id,
                    page_identity=f"page:{plan.operation_id}",
                    provider_record_id=f"weather:{plan.operation_id}",
                ),
                grid_id="LWX/96,70",
                weather_variable="precipitationLastHour",
                value="2.5",
                duration_seconds=3600,
                statistic="interval_total",
            )
            return complete_batch(plan, (record,))

        store = MemoryEvidenceStore()
        weather = FakeCollectionPort(weather_batch)
        handoff = await _runtime(
            store,
            water_port=_water_port(scope),
            weather_port=weather,
        ).start(scope)

        relations = tuple(
            fragment
            for fragment in store.fragments.values()
            if isinstance(fragment.payload, RelationSupportPayload)
        )
        assert weather.calls
        assert relations
        assert all(fragment.relation_id == rule.rule_id for fragment in relations)
        assert all(
            fragment.payload.plan_digest == scope.evidence_rule_plan.plan_digest
            for fragment in relations
        )
        assert any(
            cell.allowed_indirect_fragment_refs
            for cell in handoff.cells
            if cell.parameter == base_context.params_used[0].key
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
            handoff = await _runtime(store, water_port=port).start(scope)
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
            _runtime(store, water_port=water).start(scope)
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
    context_factory,
    result_kind,
    expected,
):
    async def exercise():
        base_context = context_factory()
        upstream_rule = create_evidence_rule(
            output_parameter=base_context.params_used[0].key,
            input_parameters=(base_context.params_used[0].key,),
            temporal_relation=EvidenceTemporalRelation.SAME_WINDOW,
            spatial_relation=EvidenceSpatialRelation.UPSTREAM_TO_DOWNSTREAM,
            maximum_inference_strength=EvidenceInferenceStrength.DIRECTIONAL,
            required_collection_attempt=True,
            rationale="Use a completed direct-upstream result directionally.",
            origin=EvidenceRuleOrigin.QUERY_PARSER,
        )
        discharge_rule = create_evidence_rule(
            output_parameter=base_context.supporting_params[0].key,
            input_parameters=(base_context.supporting_params[0].key,),
            temporal_relation=EvidenceTemporalRelation.SAME_WINDOW,
            spatial_relation=EvidenceSpatialRelation.UPSTREAM_TO_DOWNSTREAM,
            maximum_inference_strength=EvidenceInferenceStrength.DIRECTIONAL,
            required_collection_attempt=True,
            rationale="Use completed direct-upstream discharge directionally.",
            origin=EvidenceRuleOrigin.QUERY_PARSER,
        )
        context = base_context.model_copy(
            update={
                "evidence_rule_plan": create_evidence_rule_plan(
                    (upstream_rule, discharge_rule)
                )
            }
        )
        scope = scope_factory(
            run_id=f"run-upstream-{result_kind}",
            session_id=f"session-downstream-{result_kind}",
            with_station=False,
            with_upstream=True,
            context=context,
        )
        store = MemoryEvidenceStore()
        upstream = _upstream_port(scope, store, result_kind=result_kind)
        water = FakeCollectionPort(lambda plan: complete_batch(plan))
        handoff = await _runtime(
            store,
            water_port=water,
            upstream_port=upstream,
            upstream_ready=True,
        ).start(scope)
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


@pytest.mark.parametrize(
    ("case", "overrides"),
    (
        ("unauthorized_comid", {"record_comid": "999"}),
        ("session_comid", {"session_comid": "999"}),
        (
            "session_not_completed",
            {"session_status": AgentSessionStatus.RUNNING},
        ),
        ("reference_session", {"reference_session_id": "other-session"}),
        (
            "parameter",
            {
                "record_parameter": ParameterKey(
                    code_system="usgs",
                    code="00065",
                )
            },
        ),
        ("window", {"record_window_id": "window-not-authorized"}),
        ("time_range", {"mismatched_time": True}),
        ("closure", {"duplicate_closure": True}),
    ),
)
def test_upstream_output_is_authorized_before_it_can_support_cells(
    scope_factory,
    case,
    overrides,
) -> None:
    async def exercise() -> None:
        scope = scope_factory(
            run_id=f"run-invalid-upstream-{case}",
            session_id=f"session-invalid-upstream-{case}",
            with_station=False,
            with_upstream=True,
        )
        store = MemoryEvidenceStore()
        handoff = await _runtime(
            store,
            upstream_port=_upstream_port(
                scope,
                store,
                result_kind="quantified",
                **overrides,
            ),
            upstream_ready=True,
        ).start(scope)

        assert len(store.calls) == 1
        assert len(store.results) == 1
        assert store.results[0][1].safe_error.code == (
            "evidence_provider_response_invalid"
        )
        assert store.records == {}
        assert all(
            item.status is not EvidenceCellStatus.SUPPORTED
            for item in handoff.cell_gates
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
        service = _runtime(
            store,
            weather_port=weather,
            facility_port=facility,
        )
        handoff = await service.start(scope)
        handoff = await service.execute_optional(
            scope,
            (
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
            handoff,
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
        handoff = await service.start(evidence_scope)
        calls_before = tuple(store.calls)
        with pytest.raises(EvidenceScopeViolationError):
            await service.execute_optional(evidence_scope, (request,), handoff)
        assert tuple(store.calls) == calls_before

    asyncio.run(exercise())

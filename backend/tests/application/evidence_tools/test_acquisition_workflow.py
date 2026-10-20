import asyncio
from datetime import date, datetime, timedelta, timezone
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
    EvidenceCollectionArtifacts,
    EvidenceProducerStatus,
    EvidenceScopeViolationError,
    EvidenceToolExecutor,
    StoredPolicyObservations,
    assemble_evidence_session_scope,
    create_evidence_tool_registry,
)
from app.application.evidence_tools.collection_service import build_policy_observation
from app.application.query_understanding import (
    AnalysisContext,
    EvidenceInferenceStrength,
    EvidenceRuleOrigin,
    EvidenceSpatialRelation,
    EvidenceTemporalRelation,
    ParameterIntentSource,
    ResolvedParameterIntent,
    create_evidence_rule,
    create_evidence_rule_plan,
)
from app.domain import (
    AggregatePayload,
    CoverageStatus,
    CoverageUnit,
    DataQualityFlag,
    DirectRecordPayload,
    DischargeObservationRecord,
    EvidenceCellRole,
    EvidenceCellStatus,
    EvidenceLocation,
    EvidenceQualityClass,
    EvidenceResultEligibility,
    EvidenceToolExecutionStatus,
    EvidenceToolOptions,
    EvidenceToolRequest,
    HydrologyGraph,
    ItemReference,
    ParameterKey,
    ParameterProvider,
    Provenance,
    ProviderQuality,
    RelationSupportPayload,
    ResultSource,
    SourceRecordReference,
    TemporalEstimatePayload,
    TimeResolution,
    TimeResolutionSource,
    TopologySeriesRef,
    TopologyStationRef,
    UpstreamResultRecord,
    WaterObservationRecord,
    WeatherObservationRecord,
)
from app.infrastructure.config import load_parameter_capability_registry
from tests.application.evidence_tools.conftest import (
    SUPPORT_PARAMETER,
    WEATHER_PARAMETER,
    make_discharge_context,
    make_weather_context,
)
from tests.application.evidence_tools.fakes import (
    FakeCollectionPort,
    FixedClock,
    MemoryEvidenceStore,
    SequentialIds,
    complete_batch,
)

PH_PARAMETER = ParameterKey(code_system="usgs", code="00400")


def _runtime(
    store,
    *,
    water_port=None,
    weather_port=None,
    facility_port=None,
    upstream_port=None,
    upstream_ready=False,
    numeric_registry=None,
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
        numeric_registry=numeric_registry,
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


class _PersistedPolicyStore(MemoryEvidenceStore):
    def __init__(self, *, collection_complete: bool = True):
        super().__init__()
        self.collection_complete = collection_complete

    async def write_collection(self, scope, *, operation_id, manifests, records):
        saved = []
        for manifest in manifests:
            page_ids = {
                page.page_identity for page in manifest.page_or_chunk_manifests
            }
            selected = tuple(
                record for record in records
                if record.provenance.page_identity in page_ids
            )
            saved.append(await super().write_collection(
                scope,
                operation_id=f"{operation_id}:{manifest.manifest_id}",
                manifests=(manifest,),
                records=selected,
            ))
        return EvidenceCollectionArtifacts(
            manifest_refs=tuple(
                reference for item in saved for reference in item.manifest_refs
            ),
            source_record_refs=tuple(
                reference for item in saved for reference in item.source_record_refs
            ),
        )

    async def read_policy_observations(self, scope, references, window):
        registry = load_parameter_capability_registry()
        by_ref = {
            (item_id, record.record_id): record
            for (run_id, session_id, item_id), records in self.records.items()
            if run_id == scope.run_id and session_id == scope.session_id
            for record in records
        }
        manifest_by_page = {
            page.page_identity: manifest
            for manifest in self.manifests.values()
            for page in manifest.page_or_chunk_manifests
        }
        selected_records = tuple(
            by_ref[(reference.item_id, reference.record_id)]
            for reference in references
        )
        observations = tuple(
            build_policy_observation(
                registry,
                record,
                reference,
                provider_name=manifest_by_page[
                    record.provenance.page_identity
                ].provider,
                channel=manifest_by_page[
                    record.provenance.page_identity
                ].channel,
            )
            for reference, record in zip(references, selected_records, strict=True)
        )
        assert all(item is not None for item in observations)
        selected = observations[0]
        selected_item_ids = {reference.item_id for reference in references}
        eligible_refs = set()
        for item_id in selected_item_ids:
            for record in self.records[(scope.run_id, scope.session_id, item_id)]:
                reference = SourceRecordReference(
                    item_id=item_id, record_id=record.record_id,
                )
                observation = build_policy_observation(
                    registry,
                    record,
                    reference,
                    provider_name=manifest_by_page[
                        record.provenance.page_identity
                    ].provider,
                    channel=manifest_by_page[
                        record.provenance.page_identity
                    ].channel,
                )
                if (
                    observation is not None
                    and observation.parameter == selected.parameter
                    and observation.station_id == selected.station_id
                    and observation.series_identity == selected.series_identity
                    and observation.observed_time is not None
                    and window.contains(observation.observed_time)
                ):
                    eligible_refs.add(reference)
        return StoredPolicyObservations(
            observations=observations,
            collection_complete=self.collection_complete,
            selection_complete=eligible_refs == set(references),
            expected_cadence_seconds=None,
        )


@pytest.mark.parametrize("aggregate_complete", [True, False])
def test_preflight_persists_means_and_compacts_24_hour_96_point_series(
    scope_factory, context_factory, aggregate_complete,
) -> None:

    async def exercise():
        context = context_factory()
        context = type(context).model_validate(context.model_copy(update={
            "time_resolution": TimeResolution.TWENTY_FOUR_HOURS,
            "time_resolution_source": TimeResolutionSource.FRONTEND,
        }).model_dump(mode="python"))
        scope = scope_factory(context=context)
        assert len(scope.time_windows) == 1
        store = _PersistedPolicyStore(collection_complete=aggregate_complete)

        def factory(plan):
            base = _water_records(scope, plan)[0]
            window = scope.time_windows[0]
            records = []
            for index in range(96):
                values = base.model_dump(mode="python", exclude={"record_id"})
                values["observed_time"] = (
                    window.time_range.start + timedelta(minutes=15 * index)
                )
                values["provenance"] = base.provenance.model_copy(update={
                    "provider_record_id": f"{plan.operation_id}:{index:03d}",
                })
                records.append(type(base).model_validate(values))
            return complete_batch(plan, tuple(records), response_bytes=100_000)

        handoff = await _runtime(
            store, water_port=FakeCollectionPort(factory)
        ).start(scope)
        raw = tuple(
            fragment for fragment in store.fragments.values()
            if isinstance(fragment.payload, DirectRecordPayload)
        )
        assert len(raw) == 192
        assert len(store.records) == 2
        assert len(handoff.bundle.source_record_item_refs) == 2
        first_window = scope.time_windows[0].window_id
        cells = {cell.role: cell for cell in handoff.cells if cell.window_id == first_window}
        if not aggregate_complete:
            assert len(cells[EvidenceCellRole.TARGET].direct_fragment_refs) == 96
            assert len(cells[EvidenceCellRole.REQUIRED_SUPPORT].direct_fragment_refs) == 96
            assert {item.evidence_id for item in raw} <= {
                reference.evidence_id for reference in handoff.bundle.fragment_refs
            }
            assert handoff.block_facts == ("evidence_collection_incomplete",)
            assert not handoff.stop_before_model
            return
        assert len(cells[EvidenceCellRole.TARGET].direct_fragment_refs) == 1
        assert len(cells[EvidenceCellRole.REQUIRED_SUPPORT].direct_fragment_refs) == 1
        aggregate_ids = {
            *cells[EvidenceCellRole.TARGET].direct_fragment_refs,
            *cells[EvidenceCellRole.REQUIRED_SUPPORT].direct_fragment_refs,
        }
        assert not {item.evidence_id for item in raw} & {
            reference.evidence_id
            for reference, _ in handoff.validated_inputs.fragments
        }
        aggregates = tuple(
            fragment for fragment in store.fragments.values()
            if fragment.evidence_id in aggregate_ids
        )
        assert len(aggregates) == 2
        assert all(
            isinstance(item.payload, AggregatePayload)
            and len(item.payload.input_record_refs) == 96
            and item.coverage.status is CoverageStatus.COMPLETE
            and item.quality_class is EvidenceQualityClass.VALID
            and DataQualityFlag.PARTIAL_COVERAGE not in item.quality_flags
            and item.result_eligibility is EvidenceResultEligibility.INFERRED_ONLY
            for item in aggregates
        )
        assert not handoff.block_facts
        assert not handoff.stop_before_model

    asyncio.run(exercise())


def _sparse_continuous_port(
    scope, *, complete=True, target_empty=False, flow_first_only=False,
    invalid_first_target=False, unit_unresolved_first_target=False,
    unit_unresolved_middle_target=False, legacy_wdfn_source=False,
    second_sample_bad_unit_only=False,
):
    def factory(plan):
        if target_empty and plan.parameter_keys[0] == scope.requested_parameters[0]:
            return complete_batch(plan, (), complete=complete)
        bases = _water_records(scope, plan)
        if legacy_wdfn_source and plan.provider is ParameterProvider.WDFN_TIME_SERIES:
            bases = tuple(
                base.model_copy(update={"provider": "usgs_waterdata"})
                for base in bases
            )
        if second_sample_bad_unit_only and plan.station_id == "USGS-PH-2":
            base = bases[1]
            data = base.model_dump(mode="python", exclude={"record_id"})
            data["value"] = "14"
            data["raw_unit"] = "unknown-unit"
            data["observed_time"] = (
                scope.time_windows[1].time_range.start + timedelta(minutes=25)
            )
            data["provenance"] = base.provenance.model_copy(update={
                "provider_record_id": f"{plan.operation_id}:1:invalid-unit",
            })
            return complete_batch(
                plan, (type(base).model_validate(data),), complete=complete,
            )
        values = (
            (("8", "12"), "30")
            if plan.parameter_keys[0] == SUPPORT_PARAMETER
            else (("6", "8"), "9")
        )
        records = []
        selected_windows = (
            ((0, values[0]),)
            if flow_first_only else ((0, values[0]), (2, (values[1],)))
        )
        for window_index, samples in selected_windows:
            for sample_index, value in enumerate(samples):
                base = bases[window_index]
                data = base.model_dump(mode="python", exclude={"record_id"})
                data["value"] = value
                data["observed_time"] = (
                    scope.time_windows[window_index].time_range.start
                    + timedelta(minutes=5 + sample_index * 10)
                )
                data["provenance"] = base.provenance.model_copy(update={
                    "provider_record_id": (
                        f"{plan.operation_id}:{window_index}:{sample_index}"
                    ),
                })
                records.append(type(base).model_validate(data))
        if (
            (invalid_first_target or unit_unresolved_first_target)
            and plan.parameter_keys[0] != SUPPORT_PARAMETER
        ):
            base = bases[0]
            data = base.model_dump(mode="python", exclude={"record_id"})
            data["value"] = "14"
            if invalid_first_target:
                data["provider_quality"] = ProviderQuality(approval="rejected")
            if unit_unresolved_first_target:
                data["raw_unit"] = "unknown-unit"
            data["observed_time"] = (
                scope.time_windows[0].time_range.start + timedelta(minutes=25)
            )
            data["provenance"] = base.provenance.model_copy(update={
                "provider_record_id": f"{plan.operation_id}:0:invalid",
            })
            records.append(type(base).model_validate(data))
        if unit_unresolved_middle_target and plan.parameter_keys[0] != SUPPORT_PARAMETER:
            base = bases[1]
            data = base.model_dump(mode="python", exclude={"record_id"})
            data["value"] = "14"
            data["raw_unit"] = "unknown-unit"
            data["observed_time"] = (
                scope.time_windows[1].time_range.start + timedelta(minutes=25)
            )
            data["provenance"] = base.provenance.model_copy(update={
                "provider_record_id": f"{plan.operation_id}:1:invalid-unit",
            })
            records.append(type(base).model_validate(data))
        return complete_batch(plan, tuple(records), complete=complete)

    return FakeCollectionPort(factory)


def _sample_ph_scope(scope_factory, context_factory, *, sample_station_count=1):
    registry = load_parameter_capability_registry()
    capability = registry.get(PH_PARAMETER)
    assert capability is not None
    original_context = context_factory(
        decision_instant=datetime(2026, 10, 2, tzinfo=timezone.utc)
    )
    base = scope_factory(context=original_context)
    original_target = base.requested_parameters[0]
    context = AnalysisContext.model_validate(original_context.model_copy(update={
        "query": "Analyze pH.",
        "params_used": (ResolvedParameterIntent(
            key=PH_PARAMETER,
            canonical_name=capability.canonical_name,
            canonical_unit=capability.canonical_unit,
            source=ParameterIntentSource.EXPLICIT,
            rationale="Requested pH.",
        ),),
    }).model_dump(mode="python"))
    flow_station = base.station_refs[0].model_copy(update={
        "series_refs": tuple(
            reference for reference in base.station_refs[0].series_refs
            if reference.parameter == SUPPORT_PARAMETER
        ),
    })
    sample_station = TopologyStationRef(
        provider=ParameterProvider.USGS_SAMPLES,
        station_id="USGS-PH",
        series_refs=(TopologySeriesRef(
            series_identity="series-ph-sample",
            parameter=PH_PARAMETER,
            channel="discrete_samples",
            covered_segment_indexes=(0,),
            covered_window_ids=("window-000", "window-002"),
        ),),
    )
    sample_stations = [sample_station]
    if sample_station_count == 2:
        sample_stations.append(sample_station.model_copy(update={
            "station_id": "USGS-PH-2",
            "series_refs": tuple(
                reference.model_copy(update={
                    "series_identity": "series-ph-sample-2",
                })
                for reference in sample_station.series_refs
            ),
        }))
    node = base.target_node.model_copy(update={
        "qualified_units": tuple(
            CoverageUnit(
                parameter=(
                    PH_PARAMETER if unit.parameter == original_target
                    else unit.parameter
                ),
                segment_index=unit.segment_index,
            )
            for unit in base.target_node.qualified_units
        ),
        "station_refs": tuple(sorted(
            (flow_station, *sample_stations),
            key=lambda station: (station.provider.value, station.station_id),
        )),
    })
    topology = HydrologyGraph.model_validate(base.topology.model_copy(update={
        "nodes": (node,),
        "branch_outcomes": tuple(
            outcome.model_copy(update={
                "coverage_unit": outcome.coverage_unit.model_copy(update={
                    "parameter": (
                        PH_PARAMETER
                        if outcome.coverage_unit.parameter == original_target
                        else outcome.coverage_unit.parameter
                    ),
                }),
            })
            for outcome in base.topology.branch_outcomes
        ),
    }).model_dump())
    return assemble_evidence_session_scope(
        run_id=base.run_id,
        session_id=base.session_id,
        session_fact=base.session_ref,
        analysis_context_ref=base.analysis_context_ref,
        topology_ref=base.topology_ref,
        context=context,
        topology=topology,
        agent_comid=base.agent_comid,
        registry=registry,
        cancel_token=base.cancel_token,
    )


def test_preflight_fills_empty_historical_windows_from_original_means(
    scope_factory, context_factory,
) -> None:
    async def exercise():
        context = context_factory(
            decision_instant=datetime(2026, 10, 2, tzinfo=timezone.utc)
        )
        base = scope_factory(context=context)
        restricted = base.station_refs[0].model_copy(update={
            "series_refs": tuple(
                reference.model_copy(update={
                    "covered_window_ids": ("window-000", "window-002"),
                })
                for reference in base.station_refs[0].series_refs
            ),
        })
        node = base.target_node.model_copy(update={
            "station_refs": (restricted,),
        })
        topology = HydrologyGraph.model_validate(base.topology.model_copy(update={
            "nodes": (node,),
        }).model_dump())
        scope = assemble_evidence_session_scope(
            run_id=base.run_id,
            session_id=base.session_id,
            session_fact=base.session_ref,
            analysis_context_ref=base.analysis_context_ref,
            topology_ref=base.topology_ref,
            context=context,
            topology=topology,
            agent_comid=base.agent_comid,
            registry=load_parameter_capability_registry(),
            cancel_token=base.cancel_token,
        )
        assert len(scope.time_windows) == 4
        store = _PersistedPolicyStore()
        port = _sparse_continuous_port(scope)
        handoff = await _runtime(
            store,
            water_port=port,
        ).start(scope)
        assert len(port.calls) == 2
        assert all(
            plan.affected_window_ids
            == tuple(window.window_id for window in scope.time_windows)
            for plan, _ in port.calls
        )

        aggregates = {
            (fragment.parameter_refs[0], fragment.time_range): fragment
            for fragment in store.fragments.values()
            if isinstance(fragment.payload, AggregatePayload)
        }
        assert len(aggregates) == 4
        for parameter, values in (
            (SUPPORT_PARAMETER, ("10", "30")),
            (scope.requested_parameters[0], ("7", "9")),
        ):
            for index, expected in ((0, values[0]), (2, values[1])):
                fragment = aggregates[(parameter, scope.time_windows[index].time_range)]
                assert fragment.payload.value == expected
                assert fragment.coverage.status is CoverageStatus.COMPLETE

        estimates = {
            (fragment.parameter_refs[0], fragment.time_range): fragment
            for fragment in store.fragments.values()
            if isinstance(fragment.payload, TemporalEstimatePayload)
        }
        assert len(estimates) == 4
        operation_ids = {
            key: fragment.provenance.operation_id
            for key, fragment in estimates.items()
        }
        assert len(set(operation_ids.values())) == len(operation_ids)
        assert all(
            str(UUID(operation_id)) == operation_id
            for operation_id in operation_ids.values()
        )
        repeated_store = _PersistedPolicyStore()
        await _runtime(
            repeated_store,
            water_port=_sparse_continuous_port(scope),
        ).start(scope)
        repeated_operation_ids = {
            (fragment.parameter_refs[0], fragment.time_range):
                fragment.provenance.operation_id
            for fragment in repeated_store.fragments.values()
            if isinstance(fragment.payload, TemporalEstimatePayload)
        }
        assert repeated_operation_ids == operation_ids
        aggregate_ids = {fragment.evidence_id for fragment in aggregates.values()}
        for parameter, expected_middle, expected_tail in (
            (SUPPORT_PARAMETER, "20", "30"),
            (scope.requested_parameters[0], "8", "9"),
        ):
            middle = estimates[(parameter, scope.time_windows[1].time_range)]
            tail = estimates[(parameter, scope.time_windows[3].time_range)]
            assert middle.payload.value == expected_middle
            assert middle.payload.method == "linear_interpolation"
            assert middle.payload.anchor_window_ids == ("window-000", "window-002")
            assert middle.payload.anchor_distance_seconds == (21600, 21600)
            assert tail.payload.value == expected_tail
            assert tail.payload.method == "nearest_hold"
            assert tail.payload.anchor_window_ids == ("window-002",)
            assert tail.payload.anchor_distance_seconds == (21600,)
            for estimate in (middle, tail):
                assert all(
                    ref.evidence_id in aggregate_ids
                    for ref in estimate.payload.input_evidence_refs
                )
                assert DataQualityFlag.TEMPORAL_ESTIMATE in estimate.quality_flags
                assert estimate.coverage.status is CoverageStatus.INSUFFICIENT
                assert estimate.coverage.observed_count == 0
        assert not handoff.block_facts
        assert not handoff.stop_before_model
        for cell in handoff.cells:
            if cell.window_id in {"window-001", "window-003"}:
                assert cell.status is EvidenceCellStatus.SUPPORTED
                assert cell.allowed_indirect_fragment_refs

    asyncio.run(exercise())


def test_wdfn_raw_alias_matches_canonical_manifest_facts_for_means_and_gaps(
    scope_factory, context_factory,
) -> None:
    async def exercise():
        scope = scope_factory(context=context_factory(
            decision_instant=datetime(2026, 10, 2, tzinfo=timezone.utc)
        ))
        store = _PersistedPolicyStore()
        handoff = await _runtime(
            store,
            water_port=_sparse_continuous_port(
                scope, legacy_wdfn_source=True,
            ),
        ).start(scope)
        assert any(
            fragment.source == "usgs_waterdata"
            and isinstance(fragment.payload, DirectRecordPayload)
            for fragment in store.fragments.values()
        )
        assert any(
            fragment.source == ParameterProvider.WDFN_TIME_SERIES.value
            and getattr(fragment.payload, "reason", None) == "records_collected"
            for fragment in store.fragments.values()
        )
        assert len(tuple(
            fragment for fragment in store.fragments.values()
            if isinstance(fragment.payload, AggregatePayload)
        )) == 4
        assert len(tuple(
            fragment for fragment in store.fragments.values()
            if isinstance(fragment.payload, TemporalEstimatePayload)
        )) == 4
        assert not handoff.block_facts

    asyncio.run(exercise())


def test_preflight_mean_keeps_invalid_same_series_record_without_using_its_value(
    scope_factory, context_factory,
) -> None:
    async def exercise():
        scope = scope_factory(context=context_factory(
            decision_instant=datetime(2026, 10, 2, tzinfo=timezone.utc)
        ))
        store = _PersistedPolicyStore()
        handoff = await _runtime(
            store,
            water_port=_sparse_continuous_port(
                scope, invalid_first_target=True,
            ),
        ).start(scope)
        assert not handoff.block_facts
        target = scope.requested_parameters[0]
        first_window = scope.time_windows[0]
        aggregate = next(
            fragment for fragment in store.fragments.values()
            if isinstance(fragment.payload, AggregatePayload)
            and fragment.parameter_refs == (target,)
            and fragment.time_range == first_window.time_range
        )
        assert aggregate.payload.value == "7"
        assert aggregate.payload.valid_input_count == 2
        assert aggregate.payload.invalid_input_count == 1
        assert len(aggregate.payload.input_record_refs) == 3
        invalid = next(
            fragment for fragment in store.fragments.values()
            if fragment.parameter_refs == (target,)
            and fragment.time_range == first_window.time_range
            and fragment.quality_class is EvidenceQualityClass.INVALID
        )
        assert invalid.source_record_refs[0] in aggregate.payload.input_record_refs
        assert any(
            reference.evidence_id == invalid.evidence_id
            for reference in handoff.bundle.fragment_refs
        )
        estimate = next(
            fragment for fragment in store.fragments.values()
            if isinstance(fragment.payload, TemporalEstimatePayload)
            and fragment.parameter_refs == (target,)
            and fragment.time_range == scope.time_windows[1].time_range
        )
        assert estimate.payload.value == "8"

    asyncio.run(exercise())


def test_sample_ph_latest_fills_empty_windows_after_complete_collection(
    scope_factory, context_factory,
) -> None:
    async def exercise():
        scope = _sample_ph_scope(scope_factory, context_factory)
        assert len(scope.time_windows) == 4
        store = _PersistedPolicyStore()
        port = _sparse_continuous_port(scope)
        handoff = await _runtime(store, water_port=port).start(scope)

        sample_plan = next(
            plan for plan, _ in port.calls
            if plan.provider is ParameterProvider.USGS_SAMPLES
        )
        assert sample_plan.channel == "discrete_samples"
        assert sample_plan.affected_window_ids == tuple(
            window.window_id for window in scope.time_windows
        )
        aggregates = {
            fragment.time_range: fragment
            for fragment in store.fragments.values()
            if isinstance(fragment.payload, AggregatePayload)
            and fragment.parameter_refs == (PH_PARAMETER,)
        }
        assert set(aggregates) == {
            scope.time_windows[0].time_range,
            scope.time_windows[2].time_range,
        }
        assert aggregates[scope.time_windows[0].time_range].payload.value == "8"
        assert aggregates[scope.time_windows[2].time_range].payload.value == "9"
        assert all(
            fragment.payload.method.value == "latest"
            for fragment in aggregates.values()
        )
        estimates = {
            fragment.time_range: fragment
            for fragment in store.fragments.values()
            if isinstance(fragment.payload, TemporalEstimatePayload)
            and fragment.parameter_refs == (PH_PARAMETER,)
        }
        assert estimates[scope.time_windows[1].time_range].payload.value == "8.5"
        assert estimates[scope.time_windows[3].time_range].payload.value == "9"
        assert not handoff.block_facts

    asyncio.run(exercise())


def test_sample_ph_incomplete_collection_never_creates_gap_estimates(
    scope_factory, context_factory,
) -> None:
    async def exercise():
        scope = _sample_ph_scope(scope_factory, context_factory)
        store = _PersistedPolicyStore(collection_complete=False)
        handoff = await _runtime(
            store,
            water_port=_sparse_continuous_port(scope, complete=False),
        ).start(scope)
        assert not any(
            isinstance(fragment.payload, (AggregatePayload, TemporalEstimatePayload))
            and fragment.parameter_refs == (PH_PARAMETER,)
            for fragment in store.fragments.values()
        )
        assert handoff.block_facts

    asyncio.run(exercise())


def test_sample_ph_window_with_only_unresolved_units_can_be_estimated(
    scope_factory, context_factory,
) -> None:
    async def exercise():
        scope = _sample_ph_scope(scope_factory, context_factory)
        store = _PersistedPolicyStore()
        handoff = await _runtime(
            store,
            water_port=_sparse_continuous_port(
                scope, unit_unresolved_middle_target=True,
            ),
        ).start(scope)
        middle = scope.time_windows[1]
        invalid = next(
            fragment for fragment in store.fragments.values()
            if fragment.source == ParameterProvider.USGS_SAMPLES.value
            and fragment.parameter_refs == (PH_PARAMETER,)
            and fragment.time_range == middle.time_range
            and fragment.quality_class is EvidenceQualityClass.INVALID
        )
        assert DataQualityFlag.UNIT_UNRESOLVED in invalid.quality_flags
        assert any(
            getattr(fragment.payload, "reason", None) == "unit_unresolved"
            and invalid.source_record_refs[0] in fragment.source_record_refs
            for fragment in store.fragments.values()
        )
        estimate = next(
            fragment for fragment in store.fragments.values()
            if isinstance(fragment.payload, TemporalEstimatePayload)
            and fragment.parameter_refs == (PH_PARAMETER,)
            and fragment.time_range == middle.time_range
        )
        assert estimate.payload.value == "8.5"
        assert DataQualityFlag.UNIT_UNRESOLVED in estimate.quality_flags
        assert any(
            cell.parameter == PH_PARAMETER
            and cell.window_id == middle.window_id
            and cell.status is EvidenceCellStatus.SUPPORTED
            for cell in handoff.cells
        )
        assert not handoff.block_facts

    asyncio.run(exercise())


def test_two_sample_stations_with_only_unresolved_units_choose_one_gap_fill(
    scope_factory, context_factory,
) -> None:
    async def exercise():
        scope = _sample_ph_scope(
            scope_factory, context_factory, sample_station_count=2,
        )
        store = _PersistedPolicyStore()
        handoff = await _runtime(
            store,
            water_port=_sparse_continuous_port(
                scope, unit_unresolved_middle_target=True,
            ),
        ).start(scope)
        middle = scope.time_windows[1]
        estimates = tuple(
            fragment for fragment in store.fragments.values()
            if isinstance(fragment.payload, TemporalEstimatePayload)
            and fragment.parameter_refs == (PH_PARAMETER,)
            and fragment.time_range == middle.time_range
        )
        assert len(estimates) == 1
        assert estimates[0].location.station_id == "USGS-PH"
        assert estimates[0].payload.value == "8.5"
        assert DataQualityFlag.UNIT_UNRESOLVED in estimates[0].quality_flags
        assert any(
            fragment.location.station_id == "USGS-PH-2"
            and fragment.time_range == middle.time_range
            and DataQualityFlag.UNIT_UNRESOLVED in fragment.quality_flags
            for fragment in store.fragments.values()
        )
        assert not handoff.block_facts

    asyncio.run(exercise())


def test_empty_sample_station_fills_gap_despite_other_station_bad_unit(
    scope_factory, context_factory,
) -> None:
    async def exercise():
        scope = _sample_ph_scope(
            scope_factory, context_factory, sample_station_count=2,
        )
        store = _PersistedPolicyStore()
        port = _sparse_continuous_port(
            scope, second_sample_bad_unit_only=True,
        )
        handoff = await _runtime(store, water_port=port).start(scope)
        middle = scope.time_windows[1]
        station_a_plan = next(
            plan for plan, _ in port.calls if plan.station_id == "USGS-PH"
        )
        assert any(
            fragment.provenance.operation_id == station_a_plan.operation_id
            and fragment.time_range == middle.time_range
            and getattr(fragment.payload, "reason", None)
            == "no_records_in_completed_query"
            for fragment in store.fragments.values()
        )
        assert any(
            fragment.location.station_id == "USGS-PH-2"
            and fragment.time_range == middle.time_range
            and DataQualityFlag.UNIT_UNRESOLVED in fragment.quality_flags
            for fragment in store.fragments.values()
        )
        estimates = tuple(
            fragment for fragment in store.fragments.values()
            if isinstance(fragment.payload, TemporalEstimatePayload)
            and fragment.parameter_refs == (PH_PARAMETER,)
            and fragment.time_range == middle.time_range
        )
        assert len(estimates) == 1
        assert estimates[0].location.station_id == "USGS-PH"
        assert estimates[0].payload.value == "8.5"
        assert DataQualityFlag.UNIT_UNRESOLVED not in estimates[0].quality_flags
        assert any(
            cell.parameter == PH_PARAMETER
            and cell.window_id == middle.window_id
            and cell.status is EvidenceCellStatus.SUPPORTED
            for cell in handoff.cells
        )
        assert not handoff.block_facts

    asyncio.run(exercise())


@pytest.mark.parametrize("legacy_wdfn_source", [False, True])
def test_unresolved_unit_record_does_not_block_usable_same_series_mean(
    scope_factory, context_factory, legacy_wdfn_source,
) -> None:
    async def exercise():
        scope = scope_factory(context=context_factory(
            decision_instant=datetime(2026, 10, 2, tzinfo=timezone.utc)
        ))
        store = _PersistedPolicyStore()
        handoff = await _runtime(store, water_port=_sparse_continuous_port(
            scope,
            unit_unresolved_first_target=True,
            legacy_wdfn_source=legacy_wdfn_source,
        )).start(scope)

        target = scope.requested_parameters[0]
        first = scope.time_windows[0].time_range
        aggregate = next(
            fragment for fragment in store.fragments.values()
            if isinstance(fragment.payload, AggregatePayload)
            and fragment.parameter_refs == (target,)
            and fragment.time_range == first
        )
        assert aggregate.payload.value == "7"
        assert aggregate.payload.valid_input_count == 2
        assert aggregate.payload.invalid_input_count == 1
        assert len(aggregate.payload.input_record_refs) == 3
        assert DataQualityFlag.UNIT_UNRESOLVED in aggregate.quality_flags
        assert any(
            getattr(fragment.payload, "reason", None) == "unit_unresolved"
            and fragment.time_range == first
            for fragment in store.fragments.values()
        )
        assert any(
            isinstance(fragment.payload, TemporalEstimatePayload)
            and fragment.parameter_refs == (target,)
            and fragment.time_range == scope.time_windows[1].time_range
            for fragment in store.fragments.values()
        )
        target_cells = {
            cell.window_id: cell for cell in handoff.cells
            if cell.parameter == target
            and cell.window_id in {"window-000", "window-001"}
        }
        assert set(target_cells) == {"window-000", "window-001"}
        assert all(
            cell.status is EvidenceCellStatus.SUPPORTED
            for cell in target_cells.values()
        )
        assert not handoff.block_facts

    asyncio.run(exercise())


def test_preflight_does_not_fill_windows_from_incomplete_collection(
    scope_factory, context_factory,
) -> None:
    async def exercise():
        context = context_factory(
            decision_instant=datetime(2026, 10, 2, tzinfo=timezone.utc)
        )
        scope = scope_factory(context=context)
        store = _PersistedPolicyStore(collection_complete=False)
        await _runtime(
            store,
            water_port=_sparse_continuous_port(scope, complete=False),
        ).start(scope)
        assert not any(
            isinstance(fragment.payload, TemporalEstimatePayload)
            for fragment in store.fragments.values()
        )

    asyncio.run(exercise())


def test_preflight_uses_distant_one_sided_flow_without_fabricating_target(
    scope_factory, context_factory,
) -> None:
    async def exercise():
        scope = scope_factory(context=context_factory(
            decision_instant=datetime(2026, 10, 2, tzinfo=timezone.utc)
        ))
        store = _PersistedPolicyStore()
        await _runtime(store, water_port=_sparse_continuous_port(
            scope,
            target_empty=True,
            flow_first_only=True,
        )).start(scope)
        estimates = tuple(
            fragment for fragment in store.fragments.values()
            if isinstance(fragment.payload, TemporalEstimatePayload)
        )
        assert len(estimates) == 3
        assert {item.parameter_refs for item in estimates} == {(SUPPORT_PARAMETER,)}
        assert {item.payload.method for item in estimates} == {"nearest_hold"}
        assert {item.payload.value for item in estimates} == {"10"}
        assert sorted(
            item.payload.anchor_distance_seconds[0] for item in estimates
        ) == [21600, 43200, 64800]

    asyncio.run(exercise())


def test_long_selected_range_preflight_keeps_agent_transform_budget(
    scope_factory, context_factory,
) -> None:
    async def exercise():
        context = context_factory(
            start_date=date(2026, 9, 26),
            end_date=date(2026, 9, 29),
            decision_instant=datetime(2026, 10, 2, tzinfo=timezone.utc),
        )
        scope = scope_factory(context=context)
        assert len(scope.time_windows) == 16
        store = _PersistedPolicyStore()
        handoff = await _runtime(
            store, water_port=_water_port(scope),
        ).start(scope)
        assert not handoff.block_facts
        assert not handoff.stop_before_model
        assert sum(
            call[1].tool_name == "aggregate_evidence" for call in store.calls
        ) == 32
        assert sum(
            isinstance(fragment.payload, AggregatePayload)
            for fragment in store.fragments.values()
        ) == 32
        assert handoff.budget_usage.transform_used == 0
        assert handoff.budget_usage.transform_limit == 24

        inspection = await handoff.executor.execute_agent_tracked(
            scope,
            _optional_request(
                scope,
                "inspect_evidence_records",
                call_id="agent-inspect-after-preflight",
                options=EvidenceToolOptions(
                    source_item_id=handoff.bundle.source_record_item_refs[0]
                ),
            ),
        )
        assert inspection.result.execution_status is EvidenceToolExecutionStatus.COMPLETED
        assert handoff.executor.budget_usage(scope).transform_used == 1

    asyncio.run(exercise())


def _optional_request(scope, tool_name, *, call_id, options=None, parameter_keys=None):
    return EvidenceToolRequest(
        tool_call_id=call_id,
        session_id=scope.session_id,
        tool_name=tool_name,
        purpose=f"Optional {tool_name} evidence.",
        window_ids=tuple(item.window_id for item in scope.time_windows),
        parameter_keys=parameter_keys or scope.requested_parameters,
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
        assert [call[1].tool_name for call in full_store.calls].count(
            "collect_water_observations"
        ) == 1
        assert [call[1].tool_name for call in full_store.calls].count(
            "aggregate_evidence"
        ) == 8
        assert len(full_store.results) == len(full_store.calls)
        assert not full.block_facts
        assert len(full_store.bundles) == 1
        assert not hasattr(full, "inference_result")

        optional = FakeCollectionPort(lambda plan: complete_batch(plan))
        partial_scope = scope_factory(
            run_id="run-partial",
            session_id="session-partial",
            context=make_weather_context(),
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
                    parameter_keys=(WEATHER_PARAMETER,),
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
            assert not handoff.stop_before_model
            assert handoff.numeric_estimation_allowed
            assert handoff.stop_facts == ()
            assert model_calls == 1
            assert skipped_optional.calls == []
            assert store.calls and store.results and store.fragments and store.bundles
            assert all(
                fragment.source != "topology_ready"
                for fragment in store.fragments.values()
            )

    asyncio.run(exercise())


def test_ph_and_discharge_do_not_offer_weather_for_an_unrelated_rule(
    scope_factory, context_factory,
) -> None:
    async def exercise() -> None:
        registry = load_parameter_capability_registry()
        ph = registry.get(ParameterKey(code_system="usgs", code="00400"))
        assert ph is not None
        base = context_factory()
        rule = create_evidence_rule(
            output_parameter=SUPPORT_PARAMETER,
            input_parameters=(ph.key,),
            temporal_relation=EvidenceTemporalRelation.SAME_WINDOW,
            spatial_relation=EvidenceSpatialRelation.SAME_WATERBODY,
            maximum_inference_strength=EvidenceInferenceStrength.DIRECTIONAL,
            required_collection_attempt=False,
            rationale="An unrelated local rule must not enable weather.",
            origin=EvidenceRuleOrigin.QUERY_PARSER,
        )
        context = AnalysisContext.model_validate(base.model_copy(update={
            "query": "Compute pH at the target coordinates.",
            "params_used": (ResolvedParameterIntent(
                key=ph.key,
                canonical_name=ph.canonical_name,
                canonical_unit=ph.canonical_unit,
                source=ParameterIntentSource.EXPLICIT,
                rationale="Requested pH.",
            ),),
            "evidence_rule_plan": create_evidence_rule_plan((rule,)),
        }).model_dump(mode="python"))
        scope = scope_factory(context=context)
        store = MemoryEvidenceStore()
        weather = FakeCollectionPort(lambda plan: complete_batch(plan))
        service = _runtime(
            store,
            water_port=_water_port(scope),
            weather_port=weather,
        )
        handoff = await service.start(scope)

        assert "collect_weather_evidence" not in {
            item.name for item in service.planning_roster(scope, handoff)
        }
        with pytest.raises(EvidenceScopeViolationError):
            await service.execute_optional(
                scope,
                (_optional_request(
                    scope,
                    "collect_weather_evidence",
                    call_id="weather-not-eligible",
                ),),
                handoff,
            )
        assert weather.calls == []
        assert all(
            record.provider != "nws"
            for records in store.records.values()
            for record in records
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
            context=make_weather_context(),
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
                    parameter_keys=(WEATHER_PARAMETER,),
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

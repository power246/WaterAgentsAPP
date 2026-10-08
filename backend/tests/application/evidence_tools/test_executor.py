import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.application.evidence_policy import PolicyObservation, QualityAssessment
from app.application.evidence_tools import (
    EvidenceProducerStatus,
    EvidenceScopeViolationError,
    EvidenceToolExecutor,
    EvidenceToolLimits,
    create_evidence_tool_registry,
)
from app.domain import (
    CollectionStatus,
    CoverageStatus,
    DataQualityFlag,
    DirectRecordPayload,
    EvidenceApplicability,
    EvidenceCoverage,
    EvidenceFragment,
    EvidenceFragmentReference,
    EvidenceLocation,
    EvidenceQualityClass,
    EvidenceResultEligibility,
    EvidenceToolExecutionStatus,
    EvidenceToolOptions,
    EvidenceToolRequest,
    HistoricalSearchStrategy,
    MemoryMode,
    ParameterKey,
    ParameterProvider,
    Provenance,
    ProviderQuality,
    SourceRecordReference,
    TranscriptEvidenceRequest,
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


def request(scope, tool_name, call_id, *, options=None, parameter_keys=None):
    return EvidenceToolRequest(
        tool_call_id=call_id,
        session_id=scope.session_id,
        tool_name=tool_name,
        purpose=f"test {tool_name}",
        window_ids=(scope.time_windows[0].window_id,),
        parameter_keys=parameter_keys or (scope.requested_parameters[0],),
        options=options or EvidenceToolOptions(),
    )


def test_unknown_tool_is_rejected_before_audit_records(evidence_scope) -> None:
    async def exercise() -> None:
        store = MemoryEvidenceStore()
        runtime = executor(store)

        with pytest.raises(EvidenceScopeViolationError):
            await runtime.execute(
                evidence_scope,
                request(evidence_scope, "unknown_tool", "call-unknown"),
            )

        assert store.calls == []
        assert store.results == []

    asyncio.run(exercise())


def test_scope_violation_is_rejected_before_audit_records(evidence_scope) -> None:
    async def exercise() -> None:
        store = MemoryEvidenceStore()
        runtime = executor(store)
        invalid_request = request(
            evidence_scope,
            "collect_water_observations",
            "call-invalid-window",
        ).model_copy(update={"window_ids": ("window-not-authorized",)})

        with pytest.raises(EvidenceScopeViolationError):
            await runtime.execute(evidence_scope, invalid_request)

        assert store.calls == []
        assert store.results == []

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "case",
    (
        "stationless_local_observation",
        "stateless_transcript_search",
        "cross_tool_option",
        "missing_facility_radius",
        "missing_conversion_inputs",
        "missing_aggregation_inputs",
        "missing_inspection_source",
    ),
)
def test_descriptor_preflight_rejects_without_side_effects(
    scope_factory,
    context_factory,
    case,
) -> None:
    async def exercise() -> None:
        scope = scope_factory()
        tool_name = "collect_water_observations"
        options = EvidenceToolOptions()
        producer_statuses = None
        if case == "stationless_local_observation":
            scope = scope_factory(with_station=False)
        elif case == "stateless_transcript_search":
            scope = scope_factory(
                context=context_factory(memory_mode=MemoryMode.STATELESS)
            )
            tool_name = "search_transcript_evidence"
            options = EvidenceToolOptions(
                record_limit=5,
                historical_search_strategy=(
                    HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW
                ),
            )
            producer_statuses = {
                "search_transcript_evidence": EvidenceProducerStatus.READY
            }
        elif case == "cross_tool_option":
            options = EvidenceToolOptions(include_forecast=True)
        elif case == "missing_facility_radius":
            tool_name = "collect_facility_evidence"
        elif case == "missing_conversion_inputs":
            tool_name = "convert_evidence_unit"
        elif case == "missing_aggregation_inputs":
            tool_name = "aggregate_evidence"
        elif case == "missing_inspection_source":
            tool_name = "inspect_evidence_records"

        store = MemoryEvidenceStore()
        prepared_scopes = []
        runtime = executor(
            store,
            producer_statuses=producer_statuses,
            scope_preparer=prepared_scopes.append,
        )

        with pytest.raises(EvidenceScopeViolationError):
            await runtime.execute(
                scope,
                request(scope, tool_name, f"call-{case}", options=options),
            )

        assert prepared_scopes == []
        assert store.calls == []
        assert store.results == []

    asyncio.run(exercise())


def executor(
    store,
    *,
    limits=EvidenceToolLimits(),
    producer_statuses=None,
    monotonic=None,
    **ports,
):
    capabilities = load_parameter_capability_registry()
    return EvidenceToolExecutor(
        registry=create_evidence_tool_registry(
            capabilities,
            limits=limits,
            producer_statuses=producer_statuses,
        ),
        capability_registry=capabilities,
        writer=store,
        reader=store,
        limits=limits,
        clock=FixedClock(),
        monotonic=monotonic,
        operation_id_factory=SequentialIds(),
        **ports,
    )


def source_record(scope, index, *, hour=1):
    start = scope.time_windows[0].time_range.start
    return WaterObservationRecord(
        provider="usgs",
        channel="continuous",
        source_uri="https://example.test/public",
        retrieved_at=start + timedelta(days=1),
        observed_time=start + timedelta(hours=hour),
        location=EvidenceLocation(
            point=None,
            comid=scope.agent_comid,
            station_id="USGS-123",
            distance_to_target_km=0,
        ),
        provider_quality=ProviderQuality(approval="approved"),
        raw_unit="mg/L",
        provenance=Provenance(provider_record_id=f"record-{index:03d}"),
        parameter=scope.requested_parameters[0],
        value=str(6 + index),
        medium="water",
        statistic="instantaneous",
    )


def direct_fragment(scope, reference, record_ref):
    window = scope.time_windows[0].time_range
    return EvidenceFragment(
        evidence_id=reference.evidence_id,
        source="test_source",
        collection_status=CollectionStatus.AVAILABLE,
        target_comid=scope.agent_comid,
        location=EvidenceLocation(
            point=None,
            comid=scope.agent_comid,
            station_id="USGS-123",
            distance_to_target_km=0,
        ),
        time_range=window,
        parameter_refs=(scope.requested_parameters[0],),
        data_type="direct_record",
        applicability=EvidenceApplicability.DIRECT_MEASUREMENT,
        series_identity="series:00300:samples",
        quality_class=EvidenceQualityClass.VALID,
        coverage=EvidenceCoverage(
            status=CoverageStatus.PARTIAL,
            observed_count=1,
            covered_duration_seconds=0,
            window_duration_seconds=int((window.end - window.start).total_seconds()),
        ),
        payload=DirectRecordPayload(
            record_ref=record_ref,
            displayed_value="8",
            displayed_unit="mg/L",
        ),
        source_record_refs=(record_ref,),
        provenance=Provenance(operation_id="source-operation"),
        result_eligibility=EvidenceResultEligibility.OBSERVED_OR_INFERRED,
        rag_eligible=True,
    )


def observations(scope, refs):
    registry = load_parameter_capability_registry()
    capability = registry.get(scope.requested_parameters[0])
    assert capability is not None
    policy = registry.get_channel_policy(
        capability.key,
        ParameterProvider.USGS_SAMPLES,
        "discrete_samples",
    )
    assert policy is not None
    start = scope.time_windows[0].time_range.start
    return tuple(
        PolicyObservation(
            record_ref=reference,
            parameter=capability.key,
            provider=ParameterProvider.USGS_SAMPLES,
            channel="discrete_samples",
            location_comid=scope.agent_comid,
            station_id="USGS-123",
            medium=policy.medium_constraint,
            fraction=policy.fraction_constraint,
            method=policy.accepted_methods[0],
            statistic=policy.accepted_statistics[0],
            value_semantics=policy.value_semantics,
            observed_time=start + timedelta(hours=index + 1),
            value=str(7 + index * 2),
            unit=capability.canonical_unit,
            quality=QualityAssessment(
                quality_class=EvidenceQualityClass.VALID,
                flags=(),
                approval="approved",
                qualifiers=(),
            ),
            series_identity="series:00300:samples",
        )
        for index, reference in enumerate(refs)
    )


def test_all_five_collection_and_producer_slots_use_one_executor_interface(
    scope_factory,
    context_factory,
) -> None:
    async def exercise() -> None:
        scope = scope_factory(
            context=context_factory(memory_mode=MemoryMode.MEMORY_AUGMENTED),
            with_upstream=True,
        )
        store = MemoryEvidenceStore()
        ports = {
            name: FakeCollectionPort(lambda provider_request: complete_batch(provider_request))
            for name in ("water", "weather", "facility", "upstream", "transcript")
        }
        runtime = executor(
            store,
            producer_statuses={
                "get_upstream_results": EvidenceProducerStatus.READY,
                "search_transcript_evidence": EvidenceProducerStatus.READY,
            },
            water_port=ports["water"],
            weather_port=ports["weather"],
            facility_port=ports["facility"],
            upstream_port=ports["upstream"],
            transcript_port=ports["transcript"],
        )
        calls = (
            request(
                scope,
                "collect_water_observations",
                "call-water",
                parameter_keys=scope.supporting_parameters,
            ),
            request(scope, "collect_weather_evidence", "call-weather"),
            request(
                scope,
                "collect_facility_evidence",
                "call-facility",
                options=EvidenceToolOptions(facility_radius_miles=20.0),
            ),
            request(scope, "get_upstream_results", "call-upstream"),
            request(
                scope,
                "search_transcript_evidence",
                "call-transcript",
                options=EvidenceToolOptions(
                    record_limit=5,
                    historical_search_strategy=(
                        HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW
                    ),
                ),
            ),
        )

        results = [await runtime.execute(scope, item) for item in calls]

        assert all(
            item.execution_status is EvidenceToolExecutionStatus.COMPLETED
            for item in results
        )
        assert all(item.collection_status is CollectionStatus.EMPTY for item in results)
        assert len(store.calls) == len(calls)
        assert len(store.results) == len(calls)
        assert all(port.calls for port in ports.values())
        transcript_request = ports["transcript"].calls[0][0]
        assert isinstance(transcript_request, TranscriptEvidenceRequest)
        assert transcript_request.intent.strategy is (
            HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW
        )
        assert transcript_request.intent.limit == 5
        assert set(transcript_request.intent.model_dump(mode="json")) == {
            "strategy",
            "parameter_keys",
            "window_ids",
            "limit",
        }

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "field_name",
    ("parameter_keys", "window_ids"),
)
def test_historical_scope_values_fail_before_prepare_search_or_audit(
    scope_factory,
    context_factory,
    field_name,
) -> None:
    async def exercise() -> None:
        scope = scope_factory(
            context=context_factory(memory_mode=MemoryMode.MEMORY_AUGMENTED)
        )
        store = MemoryEvidenceStore()
        producer = FakeCollectionPort(
            lambda provider_request: complete_batch(provider_request)
        )
        prepared_scopes = []
        runtime = executor(
            store,
            producer_statuses={
                "search_transcript_evidence": EvidenceProducerStatus.READY
            },
            transcript_port=producer,
            scope_preparer=prepared_scopes.append,
        )
        valid = request(
            scope,
            "search_transcript_evidence",
            "call-transcript-out-of-scope",
            options=EvidenceToolOptions(
                historical_search_strategy=(
                    HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW
                )
            ),
        )
        if field_name == "parameter_keys":
            invalid_value = ParameterKey(code_system="usgs", code="99999")
            invalid = valid.model_copy(update={"parameter_keys": (invalid_value,)})
        else:
            invalid = valid.model_copy(
                update={"window_ids": ("window-not-authorized",)}
            )

        with pytest.raises(EvidenceScopeViolationError):
            await runtime.execute(scope, invalid)

        assert producer.calls == []
        assert prepared_scopes == []
        assert store.calls == []
        assert store.results == []

    asyncio.run(exercise())


def test_conversion_aggregation_and_inspection_reuse_policy_and_saved_facts(
    evidence_scope,
) -> None:
    async def exercise() -> None:
        store = MemoryEvidenceStore()
        runtime = executor(store)
        source_ref = SourceRecordReference(
            item_id="source-item",
            record_id="record-001",
        )
        fragment_ref = EvidenceFragmentReference(
            item_id="fragment-item",
            evidence_id="evidence-direct",
        )
        store.seed_fragment(
            fragment_ref,
            direct_fragment(evidence_scope, fragment_ref, source_ref),
        )
        capability = load_parameter_capability_registry().get(
            evidence_scope.requested_parameters[0]
        )
        assert capability is not None
        conversion = await runtime.execute(
            evidence_scope,
            request(
                evidence_scope,
                "convert_evidence_unit",
                "call-conversion",
                options=EvidenceToolOptions(
                    fragment_refs=(fragment_ref,),
                    target_unit_id=capability.unit_policy_id,
                ),
            ),
        )

        refs = (
            SourceRecordReference(item_id="source-item", record_id="record-001"),
            SourceRecordReference(item_id="source-item", record_id="record-002"),
        )
        store.seed_observations(refs, observations(evidence_scope, refs), cadence=None)
        aggregate = await runtime.execute(
            evidence_scope,
            request(
                evidence_scope,
                "aggregate_evidence",
                "call-aggregate",
                options=EvidenceToolOptions(
                    source_record_refs=refs,
                    aggregation_method="mean",
                ),
            ),
        )

        saved_records = (source_record(evidence_scope, 2, hour=2), source_record(evidence_scope, 1))
        store.seed_records(evidence_scope, "inspection-source", saved_records)
        inspection = await runtime.execute(
            evidence_scope,
            request(
                evidence_scope,
                "inspect_evidence_records",
                "call-inspection",
                options=EvidenceToolOptions(
                    source_item_id="inspection-source",
                    record_limit=1,
                ),
            ),
        )

        assert conversion.execution_status is EvidenceToolExecutionStatus.COMPLETED
        converted = store.fragments[
            (conversion.fragment_refs[0].item_id, conversion.fragment_refs[0].evidence_id)
        ]
        assert converted.payload.value == "8"
        assert converted.payload.input_evidence_ref == fragment_ref
        assert aggregate.execution_status is EvidenceToolExecutionStatus.COMPLETED
        aggregated = store.fragments[
            (aggregate.fragment_refs[0].item_id, aggregate.fragment_refs[0].evidence_id)
        ]
        assert aggregated.payload.value == "8"
        assert aggregated.payload.input_record_refs == refs
        assert inspection.source_record_refs[0].record_id == "record-001"
        page = runtime.inspection_page(evidence_scope.run_id, "call-inspection")
        assert page.records[0].value == "7"
        assert page.next_offset == 1

    asyncio.run(exercise())


def test_unavailable_producer_budget_and_response_limits_return_safe_results(
    evidence_scope,
) -> None:
    async def exercise() -> None:
        unavailable_store = MemoryEvidenceStore()
        unavailable = await executor(unavailable_store).execute(
            evidence_scope,
            request(evidence_scope, "get_upstream_results", "call-unavailable"),
        )
        assert unavailable.execution_status is EvidenceToolExecutionStatus.FAILED
        assert unavailable.safe_error.code == "evidence_producer_unavailable"
        assert len(unavailable_store.calls) == 1
        assert len(unavailable_store.results) == 1

        limit = EvidenceToolLimits(
            collection_calls=1,
            response_bytes_per_request=32,
        )
        limit_store = MemoryEvidenceStore()
        water = FakeCollectionPort(lambda plan: complete_batch(plan, response_bytes=16))
        weather = FakeCollectionPort(lambda plan: complete_batch(plan, response_bytes=16))
        limited = executor(
            limit_store,
            limits=limit,
            water_port=water,
            weather_port=weather,
        )
        first = await limited.execute(
            evidence_scope,
            request(
                evidence_scope,
                "collect_water_observations",
                "call-first",
                parameter_keys=evidence_scope.supporting_parameters,
            ),
        )
        second = await limited.execute(
            evidence_scope,
            request(evidence_scope, "collect_weather_evidence", "call-second"),
        )
        assert first.execution_status is EvidenceToolExecutionStatus.COMPLETED
        assert second.safe_error.code == "evidence_tool_budget_exceeded"
        assert weather.calls == []

        bytes_store = MemoryEvidenceStore()
        oversized = executor(
            bytes_store,
            limits=limit,
            water_port=FakeCollectionPort(
                lambda plan: complete_batch(plan, response_bytes=64)
            ),
        )
        too_large = await oversized.execute(
            evidence_scope,
            request(
                evidence_scope,
                "collect_water_observations",
                "call-large",
                parameter_keys=evidence_scope.supporting_parameters,
            ),
        )
        assert too_large.safe_error.code == "evidence_provider_response_too_large"
        assert too_large.source_record_refs == ()

    asyncio.run(exercise())


def test_deadline_after_dispatch_preserves_persisted_fragment_references(
    evidence_scope,
) -> None:
    monotonic_value = [0.0]

    class ExpiringStore(MemoryEvidenceStore):
        async def write_fragment(self, scope, *, operation_id, fragment):
            reference = await super().write_fragment(
                scope,
                operation_id=operation_id,
                fragment=fragment,
            )
            monotonic_value[0] = 2.0
            return reference

    async def exercise() -> None:
        store = ExpiringStore()
        fragment_ref = EvidenceFragmentReference(
            item_id="fragment-item",
            evidence_id="evidence-direct",
        )
        source_ref = SourceRecordReference(
            item_id="source-item",
            record_id="record-001",
        )
        store.seed_fragment(
            fragment_ref,
            direct_fragment(evidence_scope, fragment_ref, source_ref),
        )
        capability = load_parameter_capability_registry().get(
            evidence_scope.requested_parameters[0]
        )
        assert capability is not None
        runtime = executor(
            store,
            limits=EvidenceToolLimits(total_deadline_seconds=1),
            monotonic=lambda: monotonic_value[0],
        )

        result = await runtime.execute(
            evidence_scope,
            request(
                evidence_scope,
                "convert_evidence_unit",
                "deadline-after-dispatch",
                options=EvidenceToolOptions(
                    fragment_refs=(fragment_ref,),
                    target_unit_id=capability.unit_policy_id,
                ),
            ),
        )

        assert result.execution_status is EvidenceToolExecutionStatus.FAILED
        assert result.safe_error.code == "evidence_provider_timeout"
        assert len(result.fragment_refs) == 1
        assert result.counts.fragments == 1
        persisted = result.fragment_refs[0]
        assert (persisted.item_id, persisted.evidence_id) in store.fragments

    asyncio.run(exercise())


def test_page_record_transform_and_deadline_limits_stop_new_work(
    evidence_scope,
) -> None:
    async def exercise() -> None:
        page_limits = EvidenceToolLimits(pages_per_operation=1)
        page_store = MemoryEvidenceStore()
        weather = FakeCollectionPort(lambda plan: complete_batch(plan))
        page_runtime = executor(
            page_store,
            limits=page_limits,
            weather_port=weather,
        )
        page_result = await page_runtime.execute(
            evidence_scope,
            request(
                evidence_scope,
                "collect_weather_evidence",
                "call-pages",
                options=EvidenceToolOptions(include_forecast=True),
            ),
        )
        assert page_result.safe_error.code == "evidence_provider_response_too_large"
        assert len(weather.calls) == 1

        record_limits = EvidenceToolLimits(records_per_operation=1)
        record_store = MemoryEvidenceStore()
        record_runtime = executor(
            record_store,
            limits=record_limits,
            weather_port=FakeCollectionPort(
                lambda plan: complete_batch(
                    plan,
                    records=(
                        source_record(evidence_scope, 1),
                        source_record(evidence_scope, 2, hour=2),
                    ),
                )
            ),
        )
        record_result = await record_runtime.execute(
            evidence_scope,
            request(evidence_scope, "collect_weather_evidence", "call-records"),
        )
        assert record_result.safe_error.code == "evidence_provider_response_too_large"
        assert record_store.records == {}

        transform_limits = EvidenceToolLimits(transform_calls=1)
        transform_store = MemoryEvidenceStore()
        transform_store.seed_records(
            evidence_scope,
            "source-one",
            (source_record(evidence_scope, 1),),
        )
        transform_store.seed_records(
            evidence_scope,
            "source-two",
            (source_record(evidence_scope, 2),),
        )
        transform_runtime = executor(transform_store, limits=transform_limits)
        first = await transform_runtime.execute(
            evidence_scope,
            request(
                evidence_scope,
                "inspect_evidence_records",
                "transform-1",
                options=EvidenceToolOptions(source_item_id="source-one"),
            ),
        )
        second = await transform_runtime.execute(
            evidence_scope,
            request(
                evidence_scope,
                "inspect_evidence_records",
                "transform-2",
                options=EvidenceToolOptions(source_item_id="source-two"),
            ),
        )
        assert first.execution_status is EvidenceToolExecutionStatus.COMPLETED
        assert second.safe_error.code == "evidence_tool_budget_exceeded"

        deadline_limits = EvidenceToolLimits(total_deadline_seconds=0.001)
        deadline_store = MemoryEvidenceStore()
        deadline_store.seed_records(
            evidence_scope,
            "deadline-one",
            (source_record(evidence_scope, 1),),
        )
        deadline_store.seed_records(
            evidence_scope,
            "deadline-two",
            (source_record(evidence_scope, 2),),
        )
        monotonic_value = [0.0]
        deadline_runtime = executor(
            deadline_store,
            limits=deadline_limits,
            monotonic=lambda: monotonic_value[0],
        )
        await deadline_runtime.execute(
            evidence_scope,
            request(
                evidence_scope,
                "inspect_evidence_records",
                "deadline-1",
                options=EvidenceToolOptions(source_item_id="deadline-one"),
            ),
        )
        monotonic_value[0] = 1.0
        expired = await deadline_runtime.execute(
            evidence_scope,
            request(
                evidence_scope,
                "inspect_evidence_records",
                "deadline-2",
                options=EvidenceToolOptions(source_item_id="deadline-two"),
            ),
        )
        assert expired.safe_error.code == "evidence_provider_timeout"
        assert deadline_runtime.inspection_page(
            evidence_scope.run_id,
            "deadline-2",
        ) is None

    asyncio.run(exercise())

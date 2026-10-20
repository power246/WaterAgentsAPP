import asyncio
from datetime import UTC, datetime

from app.application.evidence_tools.temporal_candidate import (
    build_temporal_fragment,
    persist_temporal_candidate,
    select_temporal_candidates,
)
from app.domain import (
    AggregatePayload,
    CollectionFactPayload,
    CollectionStatus,
    CoverageStatus,
    DataQualityFlag,
    DirectRecordPayload,
    EvidenceAggregationMethod,
    EvidenceApplicability,
    EvidenceCoverage,
    EvidenceDerivation,
    EvidenceFragment,
    EvidenceFragmentReference,
    EvidenceLocation,
    EvidenceQualityClass,
    EvidenceResultEligibility,
    ParameterProvider,
    Provenance,
    SourceRecordReference,
    TemporalEstimatePayload,
)
from app.infrastructure.config import load_parameter_capability_registry
from tests.application.evidence_tools.conftest import make_context
from tests.application.evidence_tools.fakes import MemoryEvidenceStore


def _coverage(window, *, count=0):
    return EvidenceCoverage(
        status=CoverageStatus.PARTIAL if count else CoverageStatus.INSUFFICIENT,
        observed_count=count,
        covered_duration_seconds=0,
        window_duration_seconds=int(
            (window.time_range.end - window.time_range.start).total_seconds()
        ),
    )


def _reference(fragment):
    return EvidenceFragmentReference(
        item_id=f"item-{fragment.evidence_id}", evidence_id=fragment.evidence_id
    )


def _evidence(scope, *, failed_window=None, unresolved_window=None):
    registry = load_parameter_capability_registry()
    parameter = scope.requested_parameters[0]
    capability = registry.get(parameter)
    assert capability is not None
    unit = registry.unit_definitions[capability.unit_policy_id].symbol
    location = EvidenceLocation(
        point=scope.target_node.point,
        comid=scope.agent_comid,
        station_id="USGS-123",
        distance_to_target_km=0,
    )
    pairs = []
    for index, value in ((0, "7"), (2, "9")):
        window = scope.time_windows[index]
        source_ref = SourceRecordReference(
            item_id=f"records-{index}", record_id=f"record-{index}"
        )
        raw = EvidenceFragment(
            evidence_id=f"raw-{index}",
            source="usgs_waterdata",
            collection_status=CollectionStatus.AVAILABLE,
            target_comid=scope.agent_comid,
            location=location,
            time_range=window.time_range,
            parameter_refs=(parameter,),
            data_type="direct_record",
            applicability=EvidenceApplicability.DIRECT_MEASUREMENT,
            series_identity="series-123",
            quality_class=EvidenceQualityClass.VALID,
            coverage=_coverage(window, count=1),
            payload=DirectRecordPayload(
                record_ref=source_ref,
                displayed_value=value,
                displayed_unit=unit,
            ),
            source_record_refs=(source_ref,),
            provenance=Provenance(operation_id="collect-1"),
            result_eligibility=EvidenceResultEligibility.OBSERVED_OR_INFERRED,
        )
        aggregate = EvidenceFragment(
            evidence_id=f"mean-{index}",
            source="deterministic_aggregation",
            collection_status=CollectionStatus.AVAILABLE,
            target_comid=scope.agent_comid,
            location=location,
            time_range=window.time_range,
            parameter_refs=(parameter,),
            data_type="aggregate",
            applicability=EvidenceApplicability.DIRECT_MEASUREMENT,
            series_identity="series-123",
            quality_class=EvidenceQualityClass.VALID,
            coverage=_coverage(window, count=1),
            payload=AggregatePayload(
                value=value,
                unit=unit,
                method=EvidenceAggregationMethod.MEAN,
                input_record_refs=(source_ref,),
                valid_input_count=1,
                suspicious_input_count=0,
                invalid_input_count=0,
            ),
            source_record_refs=(source_ref,),
            derivation=EvidenceDerivation(
                algorithm="decimal_mean",
                algorithm_version=1,
                policy_id="continuous_observation",
                registry_digest=registry.manifest_digest,
            ),
            provenance=Provenance(operation_id=f"mean-{index}"),
            result_eligibility=EvidenceResultEligibility.INFERRED_ONLY,
        )
        pairs.extend(((_reference(raw), raw), (_reference(aggregate), aggregate)))
    for index, window in enumerate(scope.time_windows):
        source_refs = tuple(
            fragment.payload.record_ref
            for _, fragment in pairs
            if isinstance(fragment.payload, DirectRecordPayload)
            and fragment.time_range == window.time_range
        )
        status = (
            CollectionStatus.UNAVAILABLE
            if index == failed_window
            else CollectionStatus.AVAILABLE
            if source_refs
            else CollectionStatus.EMPTY
        )
        fact = EvidenceFragment(
            evidence_id=f"fact-{index}",
            source=ParameterProvider.WDFN_TIME_SERIES.value,
            collection_status=status,
            target_comid=scope.agent_comid,
            location=location,
            time_range=window.time_range,
            parameter_refs=(parameter,),
            data_type="collection_fact",
            applicability=EvidenceApplicability.CONTEXTUAL_ONLY,
            series_identity="series-123",
            quality_class=None,
            coverage=_coverage(window),
            payload=CollectionFactPayload(
                status=status,
                reason=(
                    "provider_incomplete"
                    if status is CollectionStatus.UNAVAILABLE
                    else "records_collected"
                    if status is CollectionStatus.AVAILABLE
                    else "no_records_in_completed_query"
                ),
                affected_window_ids=(window.window_id,),
                record_refs=source_refs,
            ),
            source_record_refs=source_refs,
            provenance=Provenance(operation_id="collect-1"),
            result_eligibility=EvidenceResultEligibility.EXPLANATORY_ONLY,
        )
        pairs.append((_reference(fact), fact))
    if unresolved_window is not None:
        window = scope.time_windows[unresolved_window]
        source_ref = SourceRecordReference(
            item_id="records-unresolved", record_id="record-unresolved"
        )
        context = EvidenceFragment(
            evidence_id="unit-unresolved-record",
            source="usgs_waterdata",
            collection_status=CollectionStatus.AVAILABLE,
            target_comid=scope.agent_comid,
            location=location,
            time_range=window.time_range,
            parameter_refs=(parameter,),
            data_type="context",
            applicability=EvidenceApplicability.CONTEXTUAL_ONLY,
            series_identity="series-123",
            quality_class=EvidenceQualityClass.INVALID,
            quality_flags=(DataQualityFlag.UNIT_UNRESOLVED,),
            coverage=_coverage(window, count=1),
            payload={
                "payload_type": "context",
                "summary_fields": (("reason", "unit_unresolved"),),
                "record_refs": (source_ref,),
            },
            source_record_refs=(source_ref,),
            provenance=Provenance(operation_id="collect-1"),
            result_eligibility=EvidenceResultEligibility.EXPLANATORY_ONLY,
            rag_eligible=False,
        )
        pairs.append((_reference(context), context))
        for suffix, status, reason in (
            ("available", CollectionStatus.AVAILABLE, "records_collected"),
            ("unresolved", CollectionStatus.INVALID, "unit_unresolved"),
        ):
            fact = EvidenceFragment(
                evidence_id=f"fact-{suffix}-{unresolved_window}",
                source=ParameterProvider.WDFN_TIME_SERIES.value,
                collection_status=status,
                target_comid=scope.agent_comid,
                location=location,
                time_range=window.time_range,
                parameter_refs=(parameter,),
                data_type="collection_fact",
                applicability=EvidenceApplicability.CONTEXTUAL_ONLY,
                series_identity="series-123",
                quality_class=None,
                coverage=_coverage(window),
                payload=CollectionFactPayload(
                    status=status,
                    reason=reason,
                    affected_window_ids=(window.window_id,),
                    record_refs=(source_ref,),
                ),
                source_record_refs=(source_ref,),
                provenance=Provenance(operation_id="collect-1"),
                result_eligibility=EvidenceResultEligibility.EXPLANATORY_ONLY,
            )
            pairs.append((_reference(fact), fact))
    return (
        registry,
        tuple(reference for reference, _ in pairs),
        tuple(fragment for _, fragment in pairs),
    )


def test_temporal_candidates_match_existing_two_sided_and_nearest_contract(
    scope_factory,
):
    scope = scope_factory(
        context=make_context(decision_instant=datetime(2026, 10, 2, tzinfo=UTC))
    )
    assert len(scope.time_windows) == 4
    registry, refs, fragments = _evidence(scope)
    candidates = select_temporal_candidates(scope, refs, fragments, registry)
    assert tuple(item.window_id for item in candidates) == (
        "window-001",
        "window-003",
    )
    first, last = candidates
    assert first.method == "linear_interpolation"
    assert first.anchor_refs == (refs[1], refs[3])
    assert last.method == "nearest_hold"
    assert last.anchor_refs == (refs[3],)
    assert all(
        isinstance(anchor.fragment.payload, AggregatePayload)
        for item in candidates
        for anchor in item.anchors
    )
    _, middle = build_temporal_fragment(
        scope, first, registry_digest=registry.manifest_digest
    )
    _, tail = build_temporal_fragment(
        scope, last, registry_digest=registry.manifest_digest
    )
    assert isinstance(middle.payload, TemporalEstimatePayload)
    assert middle.payload.value == "8"
    assert middle.payload.anchor_window_ids == ("window-000", "window-002")
    assert middle.payload.anchor_distance_seconds == (21600, 21600)
    assert tail.payload.value == "9"
    assert tail.payload.anchor_window_ids == ("window-002",)
    assert DataQualityFlag.TEMPORAL_ESTIMATE in middle.quality_flags
    assert middle.coverage.status is CoverageStatus.INSUFFICIENT
    assert middle.coverage.observed_count == 0


def test_technical_failure_cannot_be_selected_as_an_empty_window(scope_factory):
    scope = scope_factory(
        context=make_context(decision_instant=datetime(2026, 10, 2, tzinfo=UTC))
    )
    registry, refs, fragments = _evidence(scope, failed_window=1)
    candidates = select_temporal_candidates(scope, refs, fragments, registry)
    assert tuple(item.window_id for item in candidates) == ("window-003",)


def test_unit_unresolved_only_retains_quality_flag(scope_factory):
    scope = scope_factory(
        context=make_context(decision_instant=datetime(2026, 10, 2, tzinfo=UTC))
    )
    registry, refs, fragments = _evidence(scope, unresolved_window=1)
    candidates = select_temporal_candidates(scope, refs, fragments, registry)
    middle = next(item for item in candidates if item.window_id == "window-001")
    assert middle.unresolved_only
    _, result = build_temporal_fragment(
        scope, middle, registry_digest=registry.manifest_digest
    )
    assert DataQualityFlag.UNIT_UNRESOLVED in result.quality_flags


def test_persisted_temporal_result_is_read_back_before_publication(scope_factory):
    async def exercise():
        scope = scope_factory(
            context=make_context(decision_instant=datetime(2026, 10, 2, tzinfo=UTC))
        )
        registry, refs, fragments = _evidence(scope)
        store = MemoryEvidenceStore()
        for reference, fragment in zip(refs, fragments, strict=True):
            store.seed_fragment(reference, fragment)
        candidate = select_temporal_candidates(scope, refs, fragments, registry)[0]
        reference = await persist_temporal_candidate(
            scope,
            candidate,
            registry_digest=registry.manifest_digest,
            writer=store,
            reader=store,
        )
        persisted = await store.read_fragment(scope, reference)
        assert persisted.payload.value == "8"
        assert persisted.payload.input_evidence_refs == candidate.anchor_refs

    asyncio.run(exercise())

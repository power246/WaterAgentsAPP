from datetime import datetime, timedelta, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from app.domain import (
    AggregatePayload,
    AggregationMethod,
    CollectionCompletionProof,
    CollectionCounts,
    CollectionFactPayload,
    CollectionManifest,
    CollectionPageManifest,
    CollectionStatus,
    CollectionSummary,
    ContextPayload,
    ConversionPayload,
    CoverageStatus,
    DataQualityFlag,
    DirectRecordPayload,
    DischargeObservationRecord,
    EvidenceApplicability,
    EvidenceBundle,
    EvidenceCell,
    EvidenceCellRole,
    EvidenceCellStatus,
    EvidenceChain,
    EvidenceCoverage,
    EvidenceDerivation,
    EvidenceFragment,
    EvidenceFragmentReference,
    EvidenceLocation,
    EvidenceQualityClass,
    EvidenceReference,
    EvidenceToolCounts,
    EvidenceToolExecutionStatus,
    EvidenceToolOptions,
    EvidenceToolRequest,
    EvidenceToolResult,
    FacilityRecord,
    GeoPoint,
    HistoricalInferencePayload,
    HistoricalTranscriptRecord,
    ItemReference,
    ParameterKey,
    Provenance,
    ProviderQuality,
    QualitySummary,
    ReferenceScope,
    ResultSource,
    SafeEvidenceError,
    SourceRecord,
    SourceRecordReference,
    TimeRange,
    TimeResolution,
    TimeResolutionSource,
    UpstreamResultPayload,
    UpstreamResultRecord,
    WaterObservationRecord,
    WeatherForecastRecord,
    WeatherObservationRecord,
    WindowCompleteness,
)
from app.domain.hydrology.evidence_rule_id import EvidenceRuleId

NOW = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
OBSERVED_AT = datetime(2026, 1, 2, 10, tzinfo=timezone.utc)
WINDOW = TimeRange(
    start=datetime(2026, 1, 2, 6, tzinfo=timezone.utc),
    end=NOW,
)
TARGET = ParameterKey(code_system="usgs", code="00300")
DISCHARGE = ParameterKey(code_system="usgs", code="00060")
SOURCE_ADAPTER = TypeAdapter(SourceRecord)


def station_location() -> EvidenceLocation:
    return EvidenceLocation(
        point=GeoPoint(latitude=38.95, longitude=-77.13),
        comid="12345678",
        station_id="USGS-01646500",
        distance_to_target_km=0,
    )


def point_location() -> EvidenceLocation:
    return EvidenceLocation(
        point=GeoPoint(latitude=38.96, longitude=-77.14),
        comid="12345678",
        station_id=None,
        distance_to_target_km=1.25,
    )


def provenance(
    *,
    provider_record_id: str | None = "provider-record-1",
    page_identity: str | None = "page-1",
    operation_id: str | None = None,
) -> Provenance:
    return Provenance(
        operation_id=operation_id,
        registry_digest="sha256:registry",
        request_identity="sha256:request",
        page_identity=page_identity,
        chunk_identity=None,
        provider_series_id="series-1",
        provider_record_id=provider_record_id,
        source_timezone="America/New_York",
        source_utc_offset="-05:00",
    )


def source_base(
    *,
    record_type: str,
    location: EvidenceLocation,
    raw_unit: str | None,
    use_range: bool = False,
    source_provenance: Provenance | None = None,
) -> dict[str, object]:
    return {
        "record_type": record_type,
        "provider": "official_provider",
        "channel": "official_channel",
        "source_uri": "https://example.test/public/source",
        "retrieved_at": NOW,
        "observed_time": None if use_range else OBSERVED_AT,
        "valid_time_range": WINDOW if use_range else None,
        "location": location,
        "provider_quality": ProviderQuality(
            approval="approved",
            qualifiers=("provisional",),
        ),
        "raw_unit": raw_unit,
        "provenance": source_provenance or provenance(),
    }


def valid_source_records() -> tuple[object, ...]:
    water = WaterObservationRecord(
        **source_base(
            record_type="water_observation",
            location=station_location(),
            raw_unit="mg/L",
        ),
        parameter=TARGET,
        value="8.20",
        medium="surface water",
        fraction="unfiltered",
        method=None,
        statistic="instantaneous",
    )
    discharge = DischargeObservationRecord(
        **source_base(
            record_type="discharge_observation",
            location=station_location(),
            raw_unit="ft3/s",
            source_provenance=provenance(provider_record_id="flow-1"),
        ),
        parameter=DISCHARGE,
        value="37.40",
        medium="surface water",
        fraction=None,
        method=None,
        statistic="instantaneous",
    )
    weather = WeatherObservationRecord(
        **source_base(
            record_type="weather_observation",
            location=point_location(),
            raw_unit="mm",
            source_provenance=provenance(provider_record_id="weather-1"),
        ),
        grid_id="LWX-96,70",
        weather_variable="precipitation",
        value="2.50",
        duration_seconds=3600,
        statistic="interval_total",
    )
    forecast = WeatherForecastRecord(
        **source_base(
            record_type="weather_forecast",
            location=point_location(),
            raw_unit="degC",
            use_range=True,
            source_provenance=provenance(provider_record_id="forecast-1"),
        ),
        grid_id="LWX-96,70",
        issued_at=WINDOW.start - timedelta(hours=1),
        weather_variable="temperature",
        value="12.00",
    )
    facility = FacilityRecord(
        **source_base(
            record_type="facility",
            location=point_location(),
            raw_unit=None,
            source_provenance=provenance(provider_record_id="facility-1"),
        ),
        registry_id="echo",
        facility_id="facility-1",
        permit_id="permit-1",
        program="NPDES",
        status="active",
        public_attributes=(("name", "Public facility"),),
    )
    upstream = UpstreamResultRecord(
        **source_base(
            record_type="upstream_result",
            location=point_location(),
            raw_unit=None,
            use_range=True,
            source_provenance=provenance(provider_record_id="upstream-result-1"),
        ),
        upstream_session_id="session-upstream",
        result_ref=ItemReference(item_id="item-result"),
        parameter=TARGET,
        window_id="w000",
        result_kind="quantified",
        result_source=ResultSource.INFERRED,
        confidence=0.7,
        evidence_closure_refs=(ItemReference(item_id="item-evidence"),),
    )
    historical = HistoricalTranscriptRecord(
        **source_base(
            record_type="historical_transcript",
            location=point_location(),
            raw_unit=None,
            use_range=True,
            source_provenance=provenance(provider_record_id="historical-1"),
        ),
        original_run_id="run-old",
        original_session_id="session-old",
        original_item_ref=ItemReference(item_id="item-old"),
        content_type="inference_result",
        content_schema_version=1,
        rag_score=0.9,
        matched_by="semantic",
        original_quality=EvidenceQualityClass.VALID,
        original_confidence=0.6,
    )
    return water, discharge, weather, forecast, facility, upstream, historical


@pytest.mark.parametrize("record", valid_source_records())
def test_every_source_record_branch_round_trips_strictly(record: object) -> None:
    serialized = SOURCE_ADAPTER.dump_json(record)
    restored = SOURCE_ADAPTER.validate_json(serialized)

    assert restored == record
    assert restored.record_id == restored.provenance.provider_record_id
    assert "8.20" not in serialized.decode("utf-8")


def test_source_record_discriminator_rejects_unknown_branch() -> None:
    values = valid_source_records()[0].model_dump(mode="python")
    values["record_type"] = "unknown"
    values.pop("record_id")

    with pytest.raises(ValidationError, match="union_tag_invalid"):
        SOURCE_ADAPTER.validate_python(values)


@pytest.mark.parametrize("record", valid_source_records())
@pytest.mark.parametrize(
    "invalid_variant",
    ("missing", "extra", "time", "location", "unit", "identity"),
)
def test_every_source_record_branch_rejects_invalid_contract(
    record: object,
    invalid_variant: str,
) -> None:
    values = record.model_dump(mode="python")
    if invalid_variant != "identity":
        values.pop("record_id")
    if invalid_variant == "missing":
        values.pop("provider")
    elif invalid_variant == "extra":
        values["raw_provider_body"] = {"secret": "not allowed"}
    elif invalid_variant == "time":
        values["observed_time"] = None
        values["valid_time_range"] = None
    elif invalid_variant == "location":
        values["location"] = {
            "point": None,
            "comid": None,
            "station_id": None,
            "distance_to_target_km": None,
        }
    elif invalid_variant == "unit":
        values["raw_unit"] = "unexpected" if record.raw_unit is None else " "
    else:
        values["record_id"] = "wrong-identity"

    with pytest.raises(ValidationError):
        type(record).model_validate(values)


def test_source_record_rejects_naive_time() -> None:
    values = valid_source_records()[0].model_dump(mode="python")
    values.pop("record_id")
    values["retrieved_at"] = datetime(2026, 1, 2, 12)

    with pytest.raises(ValidationError, match="timezone"):
        WaterObservationRecord.model_validate(values)


def test_generated_source_identity_is_stable_and_preserves_channel_and_page() -> None:
    values = valid_source_records()[0].model_dump(mode="python")
    values.pop("record_id")
    values["provenance"]["provider_record_id"] = None
    first = WaterObservationRecord.model_validate(values)
    second = WaterObservationRecord.model_validate(values)
    assert first.record_id == second.record_id
    assert first.record_id.startswith("sha256:")

    changed_channel = dict(values, channel="daily")
    changed_page = dict(values)
    changed_page["provenance"] = dict(values["provenance"], page_identity="page-2")
    assert WaterObservationRecord.model_validate(changed_channel).record_id != first.record_id
    assert WaterObservationRecord.model_validate(changed_page).record_id != first.record_id


def test_reference_bearing_source_records_extract_item_ids() -> None:
    upstream = valid_source_records()[5]
    historical = valid_source_records()[6]

    assert upstream.referenced_item_ids() == ("item-result", "item-evidence")
    assert historical.referenced_item_ids() == ("item-old",)


def evidence_provenance() -> Provenance:
    return Provenance(
        operation_id="operation-1",
        registry_digest="sha256:registry",
        request_identity=None,
        page_identity=None,
        chunk_identity=None,
        provider_series_id=None,
        provider_record_id=None,
        source_timezone=None,
        source_utc_offset=None,
    )


def complete_coverage(*, count: int = 1) -> EvidenceCoverage:
    return EvidenceCoverage(
        status=CoverageStatus.COMPLETE,
        observed_count=count,
        expected_count=count,
        ratio="1.000",
        largest_gap_seconds=0,
    )


def derivation() -> EvidenceDerivation:
    return EvidenceDerivation(
        algorithm="decimal_mean",
        algorithm_version=1,
        policy_id="continuous_observation",
        registry_digest="sha256:registry",
    )


def source_ref(record_id: str = "provider-record-1") -> SourceRecordReference:
    return SourceRecordReference(item_id="item-source", record_id=record_id)


def sample_fragments() -> tuple[EvidenceFragment, ...]:
    direct_ref = source_ref()
    second_ref = source_ref("provider-record-2")
    common = {
        "source": "usgs_waterdata",
        "collection_status": CollectionStatus.AVAILABLE,
        "target_comid": "12345678",
        "location": station_location(),
        "time_range": WINDOW,
        "parameter_refs": (TARGET,),
        "quality_class": EvidenceQualityClass.VALID,
        "quality_flags": (),
        "coverage": complete_coverage(),
        "provenance": evidence_provenance(),
    }
    direct = EvidenceFragment(
        **common,
        evidence_id="ev-direct",
        data_type="water_observation",
        applicability=EvidenceApplicability.DIRECT_MEASUREMENT,
        series_identity="sha256:series",
        payload=DirectRecordPayload(
            record_ref=direct_ref,
            displayed_value="8.20",
            displayed_unit="mg/L",
        ),
        source_record_refs=(direct_ref,),
        rag_eligible=True,
    )
    aggregate = EvidenceFragment(
        **dict(common, parameter_refs=(DISCHARGE,), coverage=complete_coverage(count=2)),
        evidence_id="ev-aggregate",
        data_type="aggregate",
        applicability=EvidenceApplicability.DIRECT_MEASUREMENT,
        series_identity="sha256:flow-series",
        payload=AggregatePayload(
            value="37.40",
            unit="ft3/s",
            method=AggregationMethod.MEAN,
            input_record_refs=(direct_ref, second_ref),
            valid_input_count=1,
            suspicious_input_count=1,
            invalid_input_count=0,
        ),
        source_record_refs=(direct_ref, second_ref),
        derivation=derivation(),
    )
    conversion = EvidenceFragment(
        **common,
        evidence_id="ev-conversion",
        data_type="conversion",
        applicability=EvidenceApplicability.DIRECT_MEASUREMENT,
        payload=ConversionPayload(
            value="8.2",
            unit="mg/L",
            input_evidence_ref=EvidenceFragmentReference(
                item_id="item-fragment",
                evidence_id="ev-direct",
            ),
            conversion_id="identity-mg-l-v1",
        ),
        derivation=derivation(),
    )
    context = EvidenceFragment(
        **common,
        evidence_id="ev-context",
        data_type="facility_context",
        applicability=EvidenceApplicability.CONTEXTUAL_ONLY,
        payload=ContextPayload(
            summary_fields=(("program", "NPDES"),),
            record_refs=(direct_ref,),
        ),
        source_record_refs=(direct_ref,),
    )
    fact = EvidenceFragment(
        **dict(
            common,
            collection_status=CollectionStatus.EMPTY,
            quality_class=None,
            coverage=EvidenceCoverage(
                status=CoverageStatus.INSUFFICIENT,
                observed_count=0,
                expected_count=1,
                ratio="0",
                largest_gap_seconds=None,
            ),
        ),
        evidence_id="ev-fact",
        data_type="collection_fact",
        applicability=EvidenceApplicability.CONTEXTUAL_ONLY,
        series_identity="sha256:series",
        payload=CollectionFactPayload(
            status=CollectionStatus.EMPTY,
            reason="no_records_in_completed_query",
            affected_window_ids=("w000",),
        ),
    )
    upstream = EvidenceFragment(
        **common,
        evidence_id="ev-upstream",
        data_type="upstream_result",
        applicability=EvidenceApplicability.ALLOWED_INDIRECT,
        relation_id=EvidenceRuleId.SAME_PARAMETER_UPSTREAM,
        payload=UpstreamResultPayload(
            result_ref=ItemReference(item_id="item-upstream-result"),
            closure_refs=(ItemReference(item_id="item-upstream-evidence"),),
        ),
    )
    historical = EvidenceFragment(
        **common,
        evidence_id="ev-historical",
        data_type="historical_inference",
        applicability=EvidenceApplicability.CONTEXTUAL_ONLY,
        payload=HistoricalInferencePayload(
            original_item_ref=ItemReference(item_id="item-historical"),
            projection="Historical inferred value",
        ),
    )
    return direct, aggregate, conversion, context, fact, upstream, historical


@pytest.mark.parametrize("fragment", sample_fragments())
def test_every_typed_fragment_payload_round_trips(fragment: EvidenceFragment) -> None:
    restored = EvidenceFragment.model_validate_json(fragment.model_dump_json())

    assert restored == fragment
    assert restored.payload.payload_type == fragment.payload.payload_type


def test_fragment_reference_extraction_follows_typed_payloads() -> None:
    direct, _, conversion, _, _, upstream, historical = sample_fragments()

    assert direct.referenced_item_ids() == ("item-source",)
    assert conversion.referenced_item_ids() == ("item-fragment",)
    assert upstream.referenced_item_ids() == (
        "item-upstream-result",
        "item-upstream-evidence",
    )
    assert historical.referenced_item_ids() == ("item-historical",)


@pytest.mark.parametrize(
    ("fragment_index", "updates"),
    [
        (0, {"applicability": EvidenceApplicability.CONTEXTUAL_ONLY}),
        (0, {"collection_status": CollectionStatus.EMPTY}),
        (0, {"source_record_refs": ()}),
        (1, {"derivation": None}),
        (2, {"source_record_refs": (source_ref(),)}),
        (3, {"applicability": EvidenceApplicability.DIRECT_MEASUREMENT}),
        (4, {"quality_class": EvidenceQualityClass.VALID}),
        (4, {"rag_eligible": True}),
        (5, {"relation_id": None}),
        (6, {"quality_class": EvidenceQualityClass.INVALID, "rag_eligible": True}),
    ],
)
def test_fragment_rejects_invalid_payload_and_policy_combinations(
    fragment_index: int,
    updates: dict[str, object],
) -> None:
    values = sample_fragments()[fragment_index].model_dump(mode="python")
    values.update(updates)

    with pytest.raises(ValidationError):
        EvidenceFragment.model_validate(values)


def test_collection_fact_cannot_masquerade_as_quantified_evidence() -> None:
    fact = sample_fragments()[4]
    assert fact.quality_class is None
    assert fact.rag_eligible is False
    assert fact.applicability is EvidenceApplicability.CONTEXTUAL_ONLY
    assert not hasattr(fact.payload, "value")


def test_data_quality_flags_exclude_cross_series_conflict() -> None:
    removed_flag = "conflicting_" + "observations"
    assert removed_flag not in {flag.value for flag in DataQualityFlag}


def bundle_cells() -> tuple[EvidenceCell, ...]:
    return tuple(
        cell
        for window_id in ("w000", "w001")
        for cell in (
            EvidenceCell(
                parameter=TARGET,
                window_id=window_id,
                role=EvidenceCellRole.TARGET,
                direct_fragment_refs=("ev-direct",),
                status=EvidenceCellStatus.SUPPORTED,
            ),
            EvidenceCell(
                parameter=DISCHARGE,
                window_id=window_id,
                role=EvidenceCellRole.REQUIRED_SUPPORT,
                direct_fragment_refs=("ev-aggregate",),
                status=EvidenceCellStatus.SUPPORTED,
                reasons=("suspicious_inputs_present",),
            ),
        )
    )


def valid_bundle() -> EvidenceBundle:
    return EvidenceBundle(
        bundle_id="bundle-1",
        agent_comid="12345678",
        context_ref=ItemReference(item_id="item-context"),
        topology_ref=ItemReference(item_id="item-topology"),
        time_range=TimeRange(
            start=datetime(2026, 1, 2, 0, tzinfo=timezone.utc),
            end=NOW,
        ),
        analysis_timezone="America/New_York",
        time_resolution=TimeResolution.SIX_HOURS,
        time_resolution_source=TimeResolutionSource.FRONTEND,
        requested_parameters=(TARGET,),
        supporting_parameters=(DISCHARGE,),
        window_refs=("w000", "w001"),
        cells=bundle_cells(),
        fragment_refs=(
            EvidenceFragmentReference(
                item_id="item-target-fragment",
                evidence_id="ev-direct",
            ),
            EvidenceFragmentReference(
                item_id="item-flow-fragment",
                evidence_id="ev-aggregate",
            ),
        ),
        source_record_item_refs=("item-source",),
        tool_result_refs=(ItemReference(item_id="item-tool-result"),),
        quality_summary=QualitySummary(valid=1, suspicious=1, invalid=0),
        collection_summary=CollectionSummary(available=2),
        created_at=NOW,
    )


def test_reference_only_bundle_has_exact_cell_and_reference_closure() -> None:
    bundle = valid_bundle()
    restored = EvidenceBundle.model_validate_json(bundle.model_dump_json())

    assert restored == bundle
    assert len(bundle.cells) == 4
    assert not hasattr(bundle, "fragments")
    assert bundle.referenced_item_ids() == (
        "item-context",
        "item-topology",
        "item-target-fragment",
        "item-flow-fragment",
        "item-source",
        "item-tool-result",
    )


def test_bundle_validates_resolved_run_and_session_scope() -> None:
    bundle = valid_bundle()
    scopes = tuple(
        ReferenceScope(
            item_id=item_id,
            run_id="run-1",
            session_id=(
                None
                if item_id in {"item-context", "item-topology"}
                else "session-1"
            ),
        )
        for item_id in bundle.referenced_item_ids()
    )
    bundle.validate_reference_scope(
        run_id="run-1",
        session_id="session-1",
        resolved_scopes=scopes,
    )

    wrong_scope = list(scopes)
    wrong_scope[-1] = wrong_scope[-1].model_copy(update={"run_id": "run-other"})
    with pytest.raises(ValueError, match="same run"):
        bundle.validate_reference_scope(
            run_id="run-1",
            session_id="session-1",
            resolved_scopes=tuple(wrong_scope),
        )


@pytest.mark.parametrize(
    "mutation",
    ("missing_cell", "duplicate_cell", "missing_fragment", "orphan_fragment"),
)
def test_bundle_rejects_incomplete_or_duplicate_closure(mutation: str) -> None:
    values = valid_bundle().model_dump(mode="python")
    values["cells"] = list(values["cells"])
    values["fragment_refs"] = list(values["fragment_refs"])
    if mutation == "missing_cell":
        values["cells"].pop()
    elif mutation == "duplicate_cell":
        values["cells"].append(values["cells"][0])
    elif mutation == "missing_fragment":
        values["fragment_refs"].pop()
    else:
        values["fragment_refs"].append(
            {"item_id": "item-orphan", "evidence_id": "ev-orphan"}
        )

    with pytest.raises(ValidationError):
        EvidenceBundle.model_validate(values)


def test_cell_status_requires_usable_evidence_or_explicit_reason() -> None:
    with pytest.raises(ValidationError):
        EvidenceCell(
            parameter=TARGET,
            window_id="w000",
            role=EvidenceCellRole.TARGET,
            status=EvidenceCellStatus.SUPPORTED,
        )
    with pytest.raises(ValidationError):
        EvidenceCell(
            parameter=TARGET,
            window_id="w000",
            role=EvidenceCellRole.TARGET,
            direct_fragment_refs=("ev-direct",),
            status=EvidenceCellStatus.UNSUPPORTED,
            reasons=("incomplete_source",),
        )
    with pytest.raises(ValidationError):
        EvidenceCell(
            parameter=TARGET,
            window_id="w000",
            role=EvidenceCellRole.TARGET,
            status=EvidenceCellStatus.UNSUPPORTED,
        )

    blocked = EvidenceCell(
        parameter=TARGET,
        window_id="w000",
        role=EvidenceCellRole.TARGET,
        direct_fragment_refs=("ev-partial",),
        status=EvidenceCellStatus.BLOCKED,
        reasons=("incomplete_source",),
    )
    assert blocked.direct_fragment_refs == ("ev-partial",)


def valid_manifest() -> CollectionManifest:
    return CollectionManifest(
        manifest_id="manifest-1",
        operation_id="operation-1",
        provider="usgs_waterdata",
        channel="continuous",
        collection_status=CollectionStatus.AVAILABLE,
        request_range=WINDOW,
        requested_series=("series-1",),
        page_or_chunk_manifests=(
            CollectionPageManifest(
                page_identity="page-1",
                record_count=2,
                response_bytes=128,
                page_complete=True,
                next_page_identity=None,
            ),
        ),
        completed_ranges=(WINDOW,),
        affected_window_ids=("w000",),
        window_completeness=(
            WindowCompleteness(
                window_id="w000",
                complete=True,
                status=CollectionStatus.AVAILABLE,
            ),
        ),
        counts=CollectionCounts(requests=1, pages=1, records=2, response_bytes=128),
        completion_proof=CollectionCompletionProof(
            contract="official_next_chain",
            complete=True,
            terminal_identity="page-1",
            digest="sha256:proof",
        ),
    )


def test_collection_manifest_proves_counts_and_window_completion() -> None:
    manifest = valid_manifest()
    assert CollectionManifest.model_validate_json(manifest.model_dump_json()) == manifest

    values = manifest.model_dump(mode="python")
    values["counts"]["records"] = 3
    with pytest.raises(ValidationError, match="record count"):
        CollectionManifest.model_validate(values)


def tool_result_values(
    execution_status: EvidenceToolExecutionStatus,
    collection_status: CollectionStatus | None,
) -> dict[str, object]:
    return {
        "tool_call_id": "call-1",
        "operation_id": "operation-1",
        "tool_name": "collect_water_observations",
        "execution_status": execution_status,
        "collection_status": collection_status,
        "manifest_ref": (
            ItemReference(item_id="item-manifest")
            if collection_status is not None
            else None
        ),
        "counts": EvidenceToolCounts(),
        "safe_error": (
            None
            if execution_status is EvidenceToolExecutionStatus.COMPLETED
            else SafeEvidenceError(
                code="operation_not_completed",
                message="The operation did not complete.",
                retryable=execution_status is EvidenceToolExecutionStatus.FAILED,
            )
        ),
        "completed_at": NOW,
    }


@pytest.mark.parametrize(
    ("execution_status", "collection_status", "valid"),
    [
        (EvidenceToolExecutionStatus.COMPLETED, CollectionStatus.AVAILABLE, True),
        (EvidenceToolExecutionStatus.COMPLETED, CollectionStatus.EMPTY, True),
        (EvidenceToolExecutionStatus.COMPLETED, CollectionStatus.INVALID, True),
        (EvidenceToolExecutionStatus.COMPLETED, None, True),
        (EvidenceToolExecutionStatus.COMPLETED, CollectionStatus.UNAVAILABLE, False),
        (EvidenceToolExecutionStatus.FAILED, CollectionStatus.UNAVAILABLE, True),
        (EvidenceToolExecutionStatus.FAILED, CollectionStatus.INVALID, True),
        (EvidenceToolExecutionStatus.FAILED, CollectionStatus.AVAILABLE, False),
        (EvidenceToolExecutionStatus.FAILED, None, False),
        (EvidenceToolExecutionStatus.CANCELLED, None, True),
        (EvidenceToolExecutionStatus.CANCELLED, CollectionStatus.UNAVAILABLE, True),
        (EvidenceToolExecutionStatus.CANCELLED, CollectionStatus.INVALID, False),
    ],
)
def test_tool_execution_and_collection_status_matrix(
    execution_status: EvidenceToolExecutionStatus,
    collection_status: CollectionStatus | None,
    valid: bool,
) -> None:
    values = tool_result_values(execution_status, collection_status)
    if valid:
        result = EvidenceToolResult.model_validate(values)
        assert EvidenceToolResult.model_validate_json(result.model_dump_json()) == result
    else:
        with pytest.raises(ValidationError):
            EvidenceToolResult.model_validate(values)


def test_tool_result_reference_extraction_is_deduplicated_and_ordered() -> None:
    values = tool_result_values(
        EvidenceToolExecutionStatus.COMPLETED,
        CollectionStatus.AVAILABLE,
    )
    values["fragment_refs"] = (
        EvidenceFragmentReference(item_id="item-fragment", evidence_id="ev-1"),
    )
    values["collection_fact_refs"] = (
        EvidenceFragmentReference(item_id="item-fragment", evidence_id="ev-fact"),
    )
    values["counts"] = EvidenceToolCounts(fragments=2)
    result = EvidenceToolResult.model_validate(values)

    assert result.referenced_item_ids() == ("item-manifest", "item-fragment")


def test_tool_request_is_typed_and_rejects_server_resolved_controls() -> None:
    request = EvidenceToolRequest(
        tool_call_id="call-1",
        session_id="session-1",
        tool_name="collect_water_observations",
        purpose="local_station_preflight",
        window_ids=("w000",),
        parameter_keys=(TARGET, DISCHARGE),
        options=EvidenceToolOptions(record_limit=50),
    )
    assert request.options.record_limit == 50

    values = request.model_dump(mode="python")
    values["provider"] = "attacker-selected-provider"
    with pytest.raises(ValidationError, match="Extra inputs"):
        EvidenceToolRequest.model_validate(values)


def test_evidence_chain_remains_ordered_reference_only_envelope() -> None:
    chain = EvidenceChain(
        references=(
            EvidenceReference(
                evidence_id="evidence-2",
                role="supporting",
                summary=None,
            ),
            EvidenceReference(
                evidence_id="evidence-1",
                role="primary",
                summary="Nearest valid station observation",
            ),
        )
    )
    assert [reference.evidence_id for reference in chain.references] == [
        "evidence-2",
        "evidence-1",
    ]
    assert EvidenceChain.model_validate_json(chain.model_dump_json()) == chain


@pytest.mark.parametrize(
    "references",
    [
        (),
        (
            EvidenceReference(evidence_id="evidence-1", role="primary", summary=None),
            EvidenceReference(
                evidence_id="evidence-1",
                role="supporting",
                summary=None,
            ),
        ),
    ],
)
def test_evidence_chain_rejects_empty_or_duplicate_references(
    references: tuple[EvidenceReference, ...],
) -> None:
    with pytest.raises(ValidationError):
        EvidenceChain(references=references)

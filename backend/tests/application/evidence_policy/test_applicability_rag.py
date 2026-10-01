from datetime import datetime, timedelta, timezone

import pytest

from app.application.evidence_policy import (
    IndirectEvidenceContext,
    PolicyObservation,
    QualityAssessment,
    apply_rag_policy,
    is_rag_candidate,
    resolve_applicability,
)
from app.domain.evidence import (
    CollectionStatus,
    CoverageStatus,
    DirectRecordPayload,
    EvidenceApplicability,
    EvidenceCoverage,
    EvidenceFragment,
    EvidenceLocation,
    EvidenceQualityClass,
    EvidenceResultEligibility,
    Provenance,
    SourceRecordReference,
)
from app.domain.hydrology import (
    EvidenceRuleId,
    ParameterKey,
    ParameterProvider,
)
from app.domain.time import TimeRange
from app.infrastructure.config import load_parameter_capability_registry


REGISTRY = load_parameter_capability_registry()
START = datetime(2026, 9, 29, tzinfo=timezone.utc)
WINDOW = TimeRange(start=START, end=START + timedelta(hours=6))
COMID = "12345678"


def capability(code: str):
    result = REGISTRY.get(ParameterKey(code_system="usgs", code=code))
    assert result is not None
    return result


def direct_observation(
    code: str = "00300",
    *,
    comid: str | None = COMID,
    quality_class: EvidenceQualityClass = EvidenceQualityClass.VALID,
) -> PolicyObservation:
    current = capability(code)
    policy = REGISTRY.get_channel_policy(
        current.key,
        ParameterProvider.WDFN_TIME_SERIES,
        "continuous",
    )
    assert policy is not None
    return PolicyObservation(
        record_ref=SourceRecordReference(
            item_id="item-source",
            record_id="record-001",
        ),
        parameter=current.key,
        provider=ParameterProvider.WDFN_TIME_SERIES,
        channel="continuous",
        location_comid=comid,
        station_id="USGS-001",
        medium=policy.medium_constraint,
        fraction=policy.fraction_constraint,
        method=policy.accepted_methods[0],
        statistic=policy.accepted_statistics[0],
        value_semantics=policy.value_semantics,
        observed_time=START + timedelta(hours=1),
        valid_time_range=None,
        value="8",
        unit=current.canonical_unit,
        quality=QualityAssessment(
            quality_class=quality_class,
            flags=(),
            approval="final",
            qualifiers=(),
        ),
        series_identity=f"series:{code}",
    )


def test_direct_applicability_requires_all_policy_gates() -> None:
    current = capability("00300")
    record = direct_observation()

    assert (
        resolve_applicability(
            REGISTRY,
            current,
            COMID,
            WINDOW,
            observation=record,
            collection_complete=True,
            reference_complete=True,
        )
        is EvidenceApplicability.DIRECT_MEASUREMENT
    )

    for updates in (
        {"location_comid": "other"},
        {"parameter": ParameterKey(code_system="usgs", code="00060")},
        {"method": "laboratory"},
        {"statistic": "maximum"},
        {"medium": "Air"},
        {"fraction": "Total"},
        {"observed_time": WINDOW.end + timedelta(hours=1)},
        {"quality": record.quality.model_copy(
            update={"quality_class": EvidenceQualityClass.INVALID}
        )},
    ):
        assert (
            resolve_applicability(
                REGISTRY,
                current,
                COMID,
                WINDOW,
                observation=record.model_copy(update=updates),
                collection_complete=True,
                reference_complete=True,
            )
            is EvidenceApplicability.CONTEXTUAL_ONLY
        )

    assert (
        resolve_applicability(
            REGISTRY,
            current,
            COMID,
            WINDOW,
            observation=record,
            collection_complete=False,
            reference_complete=True,
        )
        is EvidenceApplicability.CONTEXTUAL_ONLY
    )
    assert (
        resolve_applicability(
            REGISTRY,
            current,
            COMID,
            WINDOW,
            observation=record,
            collection_complete=True,
            reference_complete=False,
        )
        is EvidenceApplicability.CONTEXTUAL_ONLY
    )


def test_confirmed_station_segment_can_supply_direct_location_association() -> None:
    result = resolve_applicability(
        REGISTRY,
        capability("00300"),
        COMID,
        WINDOW,
        observation=direct_observation(comid=None),
        station_segment_confirmed=True,
        collection_complete=True,
        reference_complete=True,
    )

    assert result is EvidenceApplicability.DIRECT_MEASUREMENT


def test_registered_upstream_relation_is_indirect_but_not_direct() -> None:
    current = capability("00300")
    context = IndirectEvidenceContext(
        relation_id=EvidenceRuleId.SAME_PARAMETER_UPSTREAM,
        input_parameters=(current.key,),
        input_windows=(WINDOW,),
        reference_complete=True,
        upstream_result_complete=True,
    )

    assert (
        resolve_applicability(
            REGISTRY,
            current,
            COMID,
            WINDOW,
            indirect_context=context,
        )
        is EvidenceApplicability.ALLOWED_INDIRECT
    )
    assert (
        resolve_applicability(
            REGISTRY,
            current,
            COMID,
            WINDOW,
            indirect_context=context.model_copy(
                update={"upstream_result_complete": False}
            ),
        )
        is EvidenceApplicability.CONTEXTUAL_ONLY
    )


def test_relation_registry_and_relation_specific_conditions_cannot_be_overridden() -> None:
    dissolved_oxygen = capability("00300")
    unsupported = IndirectEvidenceContext(
        relation_id=EvidenceRuleId.DISCHARGE_GAGE_HEIGHT_ESTIMATE,
        input_parameters=(ParameterKey(code_system="usgs", code="00065"),),
        input_windows=(WINDOW,),
        reference_complete=True,
        relationship_registered=True,
        model_version="rating-v1",
    )
    adjacent_too_far = IndirectEvidenceContext(
        relation_id=EvidenceRuleId.SAME_LOCATION_ADJACENT_TIME,
        input_parameters=(dissolved_oxygen.key,),
        input_windows=(
            TimeRange(
                start=WINDOW.end + timedelta(days=2),
                end=WINDOW.end + timedelta(days=2, hours=6),
            ),
        ),
        same_location=True,
        reference_complete=True,
    )

    assert (
        resolve_applicability(
            REGISTRY,
            dissolved_oxygen,
            COMID,
            WINDOW,
            indirect_context=unsupported,
        )
        is EvidenceApplicability.CONTEXTUAL_ONLY
    )
    assert (
        resolve_applicability(
            REGISTRY,
            dissolved_oxygen,
            COMID,
            WINDOW,
            indirect_context=adjacent_too_far,
        )
        is EvidenceApplicability.CONTEXTUAL_ONLY
    )


def test_registered_discharge_rating_relation_requires_versioned_relationship() -> None:
    discharge = capability("00060")
    context = IndirectEvidenceContext(
        relation_id=EvidenceRuleId.DISCHARGE_GAGE_HEIGHT_ESTIMATE,
        input_parameters=(ParameterKey(code_system="usgs", code="00065"),),
        input_windows=(WINDOW,),
        reference_complete=True,
        relationship_registered=True,
        model_version="rating-v1",
    )

    assert (
        resolve_applicability(
            REGISTRY,
            discharge,
            COMID,
            WINDOW,
            indirect_context=context,
        )
        is EvidenceApplicability.ALLOWED_INDIRECT
    )
    assert (
        resolve_applicability(
            REGISTRY,
            discharge,
            COMID,
            WINDOW,
            indirect_context=context.model_copy(update={"model_version": None}),
        )
        is EvidenceApplicability.CONTEXTUAL_ONLY
    )


def test_registered_directional_relation_requires_all_same_window_inputs() -> None:
    turbidity = capability("61028")
    context = IndirectEvidenceContext(
        relation_id=EvidenceRuleId.TURBIDITY_PRECIPITATION_DISCHARGE_DIRECTIONAL,
        input_parameters=(
            ParameterKey(code_system="usgs", code="00045"),
            ParameterKey(code_system="usgs", code="00060"),
        ),
        input_windows=(WINDOW, WINDOW),
        reference_complete=True,
    )

    assert (
        resolve_applicability(
            REGISTRY,
            turbidity,
            COMID,
            WINDOW,
            indirect_context=context,
        )
        is EvidenceApplicability.ALLOWED_INDIRECT
    )


def fragment(
    *,
    quality_class: EvidenceQualityClass = EvidenceQualityClass.VALID,
    applicability: EvidenceApplicability = EvidenceApplicability.DIRECT_MEASUREMENT,
) -> EvidenceFragment:
    reference = SourceRecordReference(
        item_id="item-source",
        record_id="record-001",
    )
    return EvidenceFragment(
        evidence_id="ev-001",
        source="usgs_waterdata",
        collection_status=CollectionStatus.AVAILABLE,
        target_comid=COMID,
        location=EvidenceLocation(
            point=None,
            comid=COMID,
            station_id="USGS-001",
            distance_to_target_km=None,
        ),
        time_range=WINDOW,
        parameter_refs=(ParameterKey(code_system="usgs", code="00300"),),
        data_type="water_observation",
        applicability=applicability,
        series_identity="series:00300",
        quality_class=quality_class,
        coverage=EvidenceCoverage(
            status=CoverageStatus.PARTIAL,
            observed_count=1,
            expected_count=None,
            ratio=None,
            covered_duration_seconds=0,
            window_duration_seconds=21600,
            largest_gap_seconds=None,
        ),
        payload=DirectRecordPayload(
            record_ref=reference,
            displayed_value="8",
            displayed_unit="mg/l",
        ),
        source_record_refs=(reference,),
        provenance=Provenance(
            operation_id="operation-001",
            registry_digest=REGISTRY.manifest_digest,
        ),
        result_eligibility=(
            EvidenceResultEligibility.EXPLANATORY_ONLY
            if quality_class is EvidenceQualityClass.INVALID
            else EvidenceResultEligibility.OBSERVED_OR_INFERRED
        ),
    )


def test_rag_policy_is_fixed_candidate_policy_and_preserves_applicability() -> None:
    source = fragment()
    eligible = apply_rag_policy(
        source,
        operation_complete=True,
        reference_complete=True,
        redacted=True,
    )

    assert eligible.rag_eligible is True
    assert eligible.applicability is source.applicability
    assert source.rag_eligible is False


@pytest.mark.parametrize(
    ("operation_complete", "reference_complete", "redacted"),
    [
        (False, True, True),
        (True, False, True),
        (True, True, False),
    ],
)
def test_incomplete_unconfirmed_or_unredacted_content_is_not_a_rag_candidate(
    operation_complete: bool,
    reference_complete: bool,
    redacted: bool,
) -> None:
    assert not is_rag_candidate(
        fragment(),
        operation_complete=operation_complete,
        reference_complete=reference_complete,
        redacted=redacted,
    )


def test_invalid_and_non_fragment_content_are_not_rag_candidates() -> None:
    assert not is_rag_candidate(
        fragment(quality_class=EvidenceQualityClass.INVALID),
        operation_complete=True,
        reference_complete=True,
        redacted=True,
    )
    assert not is_rag_candidate(
        {"kind": "bundle"},
        operation_complete=True,
        reference_complete=True,
        redacted=True,
    )


def test_historical_or_facility_content_defaults_to_contextual() -> None:
    assert (
        resolve_applicability(
            REGISTRY,
            capability("00300"),
            COMID,
            WINDOW,
        )
        is EvidenceApplicability.CONTEXTUAL_ONLY
    )

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from app.domain import (
    CollectionStatus,
    DataQualityFlag,
    EvidenceBundle,
    EvidenceChain,
    EvidenceFragment,
    EvidenceLocation,
    EvidenceReference,
    GeoPoint,
    ParameterKey,
    Provenance,
    TimeRange,
    TimeResolution,
    TimeResolutionSource,
)
from app.domain.common import JsonValue


def sample_time_range() -> TimeRange:
    return TimeRange(
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )


def sample_location(*, comid: str = "001234") -> EvidenceLocation:
    return EvidenceLocation(
        point=None,
        comid=comid,
        station_id=None,
        distance_to_target_km=None,
    )


def sample_provenance(
    *,
    source_record_ids: tuple[str, ...] = ("0001",),
    metadata: dict[str, JsonValue] | None = None,
) -> Provenance:
    return Provenance(
        provider="usgs",
        retrieved_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        source_uri="nwis-record:0001",
        source_record_ids=source_record_ids,
        source_timezone="America/Chicago",
        source_utc_offset="-06:00",
        metadata={} if metadata is None else metadata,
    )


def sample_fragment(
    *,
    evidence_id: str = "evidence-1",
    collection_status: CollectionStatus = CollectionStatus.AVAILABLE,
    target_comid: str = "001234",
    value_or_summary: JsonValue = None,
    provenance: Provenance | None = None,
) -> EvidenceFragment:
    return EvidenceFragment(
        evidence_id=evidence_id,
        source="station_observation",
        collection_status=collection_status,
        target_comid=target_comid,
        location=sample_location(comid=target_comid),
        time_range=sample_time_range(),
        parameter_refs=(ParameterKey(code_system="usgs", code="00010"),),
        data_type="water_temperature",
        value_or_summary=value_or_summary,
        quality_flags=(),
        confidence_hint=None,
        provenance=sample_provenance() if provenance is None else provenance,
    )


def assert_json_round_trip(model) -> None:
    restored = type(model).model_validate_json(model.model_dump_json())
    assert restored == model


@pytest.mark.parametrize("status", list(CollectionStatus))
def test_collection_status_round_trips(status: CollectionStatus) -> None:
    adapter = TypeAdapter(CollectionStatus)

    serialized = adapter.dump_json(status)

    assert adapter.validate_json(serialized) is status


def test_data_quality_flags_have_stable_values() -> None:
    assert [flag.value for flag in DataQualityFlag] == [
        "missing",
        "partial_coverage",
        "possible_freezing",
        "anomalous",
        "estimated",
        "historical_inference",
        "stale",
        "provider_flagged",
        "unit_unresolved",
        "conflicting_observations",
    ]


@pytest.mark.parametrize(
    ("point", "comid", "station_id"),
    [
        (GeoPoint(latitude=0, longitude=0), None, None),
        (None, "001234", None),
        (None, None, "04024000"),
    ],
)
def test_evidence_location_accepts_each_identity_and_round_trips(
    point: GeoPoint | None,
    comid: str | None,
    station_id: str | None,
) -> None:
    location = EvidenceLocation(
        point=point,
        comid=comid,
        station_id=station_id,
        distance_to_target_km=0,
    )

    assert_json_round_trip(location)


@pytest.mark.parametrize(
    "values",
    [
        {
            "point": None,
            "comid": None,
            "station_id": None,
            "distance_to_target_km": None,
        },
        {
            "point": None,
            "comid": "001234",
            "station_id": None,
            "distance_to_target_km": -0.1,
        },
        {
            "point": None,
            "comid": "001234",
            "station_id": None,
            "distance_to_target_km": float("nan"),
        },
    ],
)
def test_evidence_location_rejects_missing_identity_or_invalid_distance(
    values: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        EvidenceLocation.model_validate(values)


def test_provenance_normalizes_utc_and_preserves_source_identity() -> None:
    provenance = Provenance(
        provider="usgs",
        retrieved_at=datetime(
            2026,
            1,
            2,
            6,
            tzinfo=timezone(timedelta(hours=-6)),
        ),
        source_uri="nwis-record:0001",
        source_record_ids=("0001", "A-02"),
        source_timezone="America/Chicago",
        source_utc_offset="-06:00",
        metadata={"provider_flags": ["provisional"]},
    )

    assert provenance.retrieved_at == datetime(
        2026,
        1,
        2,
        12,
        tzinfo=timezone.utc,
    )
    assert provenance.source_record_ids == ("0001", "A-02")
    assert provenance.source_timezone == "America/Chicago"
    assert provenance.source_utc_offset == "-06:00"
    assert_json_round_trip(provenance)


def test_provenance_rejects_naive_retrieval_time() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        Provenance(
            provider="usgs",
            retrieved_at=datetime(2026, 1, 2),
            source_uri=None,
            source_record_ids=(),
            source_timezone=None,
            source_utc_offset=None,
        )


def test_evidence_fragment_full_envelope_round_trips() -> None:
    fragment = EvidenceFragment(
        evidence_id="evidence-1",
        source="station_observation",
        collection_status=CollectionStatus.AVAILABLE,
        target_comid="001234",
        location=EvidenceLocation(
            point=GeoPoint(latitude=44.95, longitude=-93.09),
            comid="009999",
            station_id="04024000",
            distance_to_target_km=1.25,
        ),
        time_range=sample_time_range(),
        parameter_refs=(ParameterKey(code_system="usgs", code="00010"),),
        data_type="water_temperature",
        value_or_summary={"value": 7.4, "unit": "degC"},
        quality_flags=(DataQualityFlag.ESTIMATED, DataQualityFlag.STALE),
        confidence_hint=0.8,
        provenance=sample_provenance(),
    )

    assert fragment.rag_eligible is False
    assert fragment.parameter_refs[0].code_system == "usgs"
    assert_json_round_trip(fragment)


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (CollectionStatus.EMPTY, None),
        (CollectionStatus.UNAVAILABLE, {"reason": "provider_timeout"}),
        (CollectionStatus.INVALID, {"reason": "unit_unresolved"}),
    ],
)
def test_non_available_status_accepts_null_or_structured_reason(
    status: CollectionStatus,
    payload: JsonValue,
) -> None:
    fragment = sample_fragment(
        collection_status=status,
        value_or_summary=payload,
    )

    assert_json_round_trip(fragment)


def test_empty_and_unavailable_remain_distinct_after_serialization() -> None:
    empty = sample_fragment(collection_status=CollectionStatus.EMPTY)
    unavailable = sample_fragment(collection_status=CollectionStatus.UNAVAILABLE)

    restored_empty = EvidenceFragment.model_validate_json(empty.model_dump_json())
    restored_unavailable = EvidenceFragment.model_validate_json(
        unavailable.model_dump_json()
    )

    assert restored_empty.collection_status is CollectionStatus.EMPTY
    assert restored_unavailable.collection_status is CollectionStatus.UNAVAILABLE
    assert restored_empty != restored_unavailable


@pytest.mark.parametrize(
    "confidence_hint",
    [-0.01, 1.01, float("nan"), float("inf")],
)
def test_evidence_fragment_rejects_invalid_confidence(
    confidence_hint: float,
) -> None:
    values = sample_fragment().model_dump()
    values["confidence_hint"] = confidence_hint

    with pytest.raises(ValidationError):
        EvidenceFragment.model_validate(values)


def test_evidence_fragment_rejects_non_json_payload() -> None:
    with pytest.raises(ValidationError):
        sample_fragment(value_or_summary={"unsupported": object()})


def test_evidence_payload_and_metadata_are_defensively_copied() -> None:
    payload = {"series": [{"value": 7.4}]}
    metadata = {"provider": {"flags": ["provisional"]}}
    fragment = sample_fragment(
        value_or_summary=payload,
        provenance=sample_provenance(metadata=metadata),
    )

    payload["series"][0]["value"] = 99
    metadata["provider"]["flags"].append("changed")
    assert fragment.value_or_summary == {"series": [{"value": 7.4}]}
    assert fragment.provenance.metadata == {
        "provider": {"flags": ["provisional"]}
    }

    dumped = fragment.model_dump()
    dumped["value_or_summary"]["series"][0]["value"] = -1
    dumped["provenance"]["metadata"]["provider"]["flags"].append("dumped")
    assert fragment.value_or_summary == {"series": [{"value": 7.4}]}
    assert fragment.provenance.metadata == {
        "provider": {"flags": ["provisional"]}
    }


def test_evidence_bundle_defaults_and_empty_evidence_round_trip() -> None:
    bundle = EvidenceBundle(
        bundle_id="bundle-1",
        agent_comid="001234",
        time_range=sample_time_range(),
        analysis_timezone="America/Chicago",
        time_resolution_source=TimeResolutionSource.DEFAULT,
        requested_parameters=(
            ParameterKey(code_system="usgs", code="00010"),
        ),
    )

    assert bundle.time_resolution is TimeResolution.TWELVE_HOURS
    assert bundle.fragments == ()
    assert_json_round_trip(bundle)


def test_evidence_bundle_allows_upstream_fragment() -> None:
    fragment = sample_fragment(target_comid="009999")
    bundle = EvidenceBundle(
        bundle_id="bundle-1",
        agent_comid="001234",
        time_range=sample_time_range(),
        analysis_timezone="Europe/Berlin",
        time_resolution=TimeResolution.SIX_HOURS,
        time_resolution_source=TimeResolutionSource.FRONTEND,
        requested_parameters=(
            ParameterKey(code_system="usgs", code="00010"),
        ),
        fragments=(fragment,),
    )

    assert bundle.fragments[0].target_comid == "009999"
    assert_json_round_trip(bundle)


@pytest.mark.parametrize(
    "requested_parameters",
    [
        (),
        (
            ParameterKey(code_system="usgs", code="00010"),
            ParameterKey(code_system="usgs", code="00010"),
        ),
    ],
)
def test_evidence_bundle_rejects_missing_or_duplicate_parameters(
    requested_parameters: tuple[ParameterKey, ...],
) -> None:
    with pytest.raises(ValidationError):
        EvidenceBundle(
            bundle_id="bundle-1",
            agent_comid="001234",
            time_range=sample_time_range(),
            analysis_timezone="America/Chicago",
            time_resolution_source=TimeResolutionSource.DEFAULT,
            requested_parameters=requested_parameters,
        )


def test_evidence_bundle_keeps_code_system_in_parameter_identity() -> None:
    bundle = EvidenceBundle(
        bundle_id="bundle-1",
        agent_comid="001234",
        time_range=sample_time_range(),
        analysis_timezone="America/Chicago",
        time_resolution_source=TimeResolutionSource.DEFAULT,
        requested_parameters=(
            ParameterKey(code_system="usgs", code="00010"),
            ParameterKey(code_system="epa", code="00010"),
        ),
    )

    assert len(bundle.requested_parameters) == 2


def test_evidence_bundle_rejects_duplicate_fragment_ids() -> None:
    with pytest.raises(ValidationError, match="evidence IDs"):
        EvidenceBundle(
            bundle_id="bundle-1",
            agent_comid="001234",
            time_range=sample_time_range(),
            analysis_timezone="America/Chicago",
            time_resolution_source=TimeResolutionSource.DEFAULT,
            requested_parameters=(
                ParameterKey(code_system="usgs", code="00010"),
            ),
            fragments=(sample_fragment(), sample_fragment()),
        )


def test_evidence_bundle_rejects_invalid_analysis_timezone() -> None:
    with pytest.raises(ValidationError, match="valid IANA timezone"):
        EvidenceBundle(
            bundle_id="bundle-1",
            agent_comid="001234",
            time_range=sample_time_range(),
            analysis_timezone="Central Standard Time",
            time_resolution_source=TimeResolutionSource.DEFAULT,
            requested_parameters=(
                ParameterKey(code_system="usgs", code="00010"),
            ),
        )


def test_evidence_id_uniqueness_is_scoped_to_each_bundle() -> None:
    fragment = sample_fragment(
        evidence_id="session-local-id",
        provenance=sample_provenance(source_record_ids=("00001", "00002")),
    )
    bundle_values = {
        "agent_comid": "001234",
        "time_range": sample_time_range(),
        "analysis_timezone": "America/Chicago",
        "time_resolution_source": TimeResolutionSource.DEFAULT,
        "requested_parameters": (
            ParameterKey(code_system="usgs", code="00010"),
        ),
        "fragments": (fragment,),
    }

    first = EvidenceBundle(bundle_id="bundle-1", **bundle_values)
    second = EvidenceBundle(bundle_id="bundle-2", **bundle_values)

    assert first.fragments[0].evidence_id == second.fragments[0].evidence_id
    assert first.fragments[0].provenance.source_record_ids == ("00001", "00002")


def test_evidence_chain_is_ordered_reference_only_envelope() -> None:
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
    assert set(chain.model_dump()["references"][0]) == {
        "evidence_id",
        "role",
        "summary",
    }
    assert_json_round_trip(chain)


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

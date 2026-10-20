"""Select and materialize historical gap fills from saved local observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal, localcontext
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from app.application.evidence_tools.errors import EvidenceScopeViolationError
from app.application.evidence_tools.models import EvidenceToolReader, EvidenceToolWriter
from app.application.evidence_tools.scope import EvidenceSessionScope, EvidenceWindow
from app.domain import (
    AggregatePayload,
    CollectionFactPayload,
    CollectionStatus,
    ContextPayload,
    CoverageStatus,
    DataQualityFlag,
    DirectRecordPayload,
    EvidenceAggregationMethod,
    EvidenceApplicability,
    EvidenceCoverage,
    EvidenceDerivation,
    EvidenceFragment,
    EvidenceFragmentReference,
    EvidenceQualityClass,
    EvidenceResultEligibility,
    InferenceMode,
    ParameterCapabilityRegistry,
    ParameterKey,
    ParameterProvider,
    Provenance,
    TemporalEstimatePayload,
)
from app.domain.common import normalize_canonical_decimal

_DISCHARGE = ParameterKey(code_system="usgs", code="00060")
_PREFLIGHT_SERIES = {
    "usgs_waterdata": (
        ParameterProvider.WDFN_TIME_SERIES,
        "continuous",
        EvidenceAggregationMethod.MEAN,
    ),
    ParameterProvider.WDFN_TIME_SERIES.value: (
        ParameterProvider.WDFN_TIME_SERIES,
        "continuous",
        EvidenceAggregationMethod.MEAN,
    ),
    ParameterProvider.USGS_SAMPLES.value: (
        ParameterProvider.USGS_SAMPLES,
        "discrete_samples",
        EvidenceAggregationMethod.LATEST,
    ),
}


@dataclass(frozen=True, slots=True)
class TemporalAnchor:
    window_id: str
    midpoint: datetime
    reference: EvidenceFragmentReference
    fragment: EvidenceFragment


@dataclass(frozen=True, slots=True)
class TemporalCandidate:
    parameter: ParameterKey
    window: EvidenceWindow
    station_id: str
    series_identity: str
    source: str
    method: str
    anchors: tuple[TemporalAnchor, ...]
    eligibility_fact_refs: tuple[EvidenceFragmentReference, ...]
    window_record_refs: tuple[EvidenceFragmentReference, ...]
    anchor_origin_refs: tuple[EvidenceFragmentReference, ...]
    direct_window_refs: tuple[EvidenceFragmentReference, ...]
    unresolved_only: bool
    unit: str
    precision: int

    @property
    def window_id(self) -> str:
        return self.window.window_id

    @property
    def anchor_refs(self) -> tuple[EvidenceFragmentReference, ...]:
        return tuple(anchor.reference for anchor in self.anchors)


def _decimal_seconds(start: datetime, end: datetime) -> Decimal:
    delta = end - start
    return Decimal(delta.days * 86400 + delta.seconds) + Decimal(
        delta.microseconds
    ) / Decimal(1_000_000)


def select_temporal_candidates(
    scope: EvidenceSessionScope,
    references: tuple[EvidenceFragmentReference, ...],
    fragments: tuple[EvidenceFragment, ...],
    capability_registry: ParameterCapabilityRegistry,
) -> tuple[TemporalCandidate, ...]:
    """Find only windows proven naturally empty or unit-unresolved by saved facts."""
    pairs = tuple(zip(references, fragments, strict=True))
    windows = {window.time_range: window for window in scope.time_windows}
    direct_sources = {
        fragment.payload.record_ref: fragment.source
        for _, fragment in pairs
        if isinstance(fragment.payload, DirectRecordPayload)
    }
    facts = tuple(
        (reference, fragment)
        for reference, fragment in pairs
        if isinstance(fragment.payload, CollectionFactPayload)
    )
    groups: dict[tuple[ParameterKey, str, str, str], list[TemporalAnchor]] = {}
    for reference, fragment in pairs:
        window = windows.get(fragment.time_range)
        anchor_sources = (
            {
                direct_sources[record_ref]
                for record_ref in fragment.payload.input_record_refs
                if record_ref in direct_sources
            }
            if isinstance(fragment.payload, AggregatePayload)
            else set()
        )
        anchor_source = next(iter(anchor_sources)) if len(anchor_sources) == 1 else None
        if (
            window is None
            or window.window_kind is not InferenceMode.GAP_FILLING
            or not isinstance(fragment.payload, AggregatePayload)
            or fragment.source != "deterministic_aggregation"
            or anchor_source not in _PREFLIGHT_SERIES
            or fragment.payload.method is not _PREFLIGHT_SERIES[anchor_source][2]
            or fragment.applicability is not EvidenceApplicability.DIRECT_MEASUREMENT
            or fragment.result_eligibility
            is not EvidenceResultEligibility.INFERRED_ONLY
            or fragment.quality_class
            not in {EvidenceQualityClass.VALID, EvidenceQualityClass.SUSPICIOUS}
            or fragment.coverage.status
            not in {CoverageStatus.COMPLETE, CoverageStatus.PARTIAL}
            or fragment.target_comid != scope.agent_comid
            or len(fragment.parameter_refs) != 1
            or fragment.location.station_id is None
            or fragment.series_identity is None
        ):
            continue
        key = (
            fragment.parameter_refs[0],
            fragment.location.station_id,
            fragment.series_identity,
            anchor_source,
        )
        groups.setdefault(key, []).append(
            TemporalAnchor(
                window_id=window.window_id,
                midpoint=window.time_range.start
                + (window.time_range.end - window.time_range.start) / 2,
                reference=reference,
                fragment=fragment,
            )
        )

    candidates: list[TemporalCandidate] = []
    ordered_groups = sorted(
        groups.items(),
        key=lambda item: (
            item[0][0] != _DISCHARGE,
            item[0][0].code_system,
            item[0][0].code,
            item[0][1],
            item[0][2],
            item[0][3],
        ),
    )
    for (parameter, station_id, series_identity, source), anchors in ordered_groups:
        anchors.sort(key=lambda anchor: (anchor.midpoint, anchor.window_id))
        capability = capability_registry.get(parameter)
        if capability is None:
            continue
        unit = capability_registry.unit_definitions[capability.unit_policy_id]
        if any(anchor.fragment.payload.unit != unit.symbol for anchor in anchors):
            continue
        anchor_origins = tuple(
            (reference, fragment)
            for reference, fragment in pairs
            if isinstance(fragment.payload, DirectRecordPayload)
            and fragment.parameter_refs == (parameter,)
            and fragment.source == source
            and fragment.location.station_id == station_id
            and fragment.series_identity == series_identity
        )
        anchor_operations = {
            fragment.provenance.operation_id for _, fragment in anchor_origins
        }
        if not anchor_operations:
            continue
        for window in scope.time_windows:
            direct_in_window = tuple(
                reference
                for reference, fragment in pairs
                if fragment.parameter_refs == (parameter,)
                and fragment.time_range == window.time_range
                and fragment.applicability is EvidenceApplicability.DIRECT_MEASUREMENT
                and fragment.quality_class
                in {EvidenceQualityClass.VALID, EvidenceQualityClass.SUSPICIOUS}
            )
            if (
                window.window_kind is not InferenceMode.GAP_FILLING
                or any(anchor.window_id == window.window_id for anchor in anchors)
                or direct_in_window
            ):
                continue
            all_window_facts = tuple(
                (reference, fact)
                for reference, fact in facts
                if fact.parameter_refs == (parameter,)
                and window.window_id in fact.payload.affected_window_ids
            )
            fact_source = _PREFLIGHT_SERIES[source][0].value
            window_facts = tuple(
                fact
                for _, fact in all_window_facts
                if fact.source == fact_source
                and fact.provenance.operation_id in anchor_operations
            )
            window_records = tuple(
                (reference, fragment)
                for reference, fragment in pairs
                if isinstance(fragment.payload, (DirectRecordPayload, ContextPayload))
                if fragment.parameter_refs == (parameter,)
                and fragment.time_range == window.time_range
                and fragment.source == source
                and fragment.provenance.operation_id in anchor_operations
                and len(fragment.source_record_refs) == 1
            )
            attributed_refs = {
                record_ref
                for _, fragment in window_records
                for record_ref in fragment.source_record_refs
            }
            candidate_records = tuple(
                fragment
                for _, fragment in window_records
                if fragment.location.station_id == station_id
                and fragment.series_identity == series_identity
            )
            candidate_refs = {
                record_ref
                for fragment in candidate_records
                for record_ref in fragment.source_record_refs
            }
            candidate_invalid_refs = {
                record_ref
                for fragment in candidate_records
                if fragment.quality_class is EvidenceQualityClass.INVALID
                and DataQualityFlag.UNIT_UNRESOLVED in fragment.quality_flags
                for record_ref in fragment.source_record_refs
            }
            available_refs = {
                record_ref
                for fact in window_facts
                if fact.collection_status is CollectionStatus.AVAILABLE
                for record_ref in fact.payload.record_refs
            }
            unresolved_fact_refs = {
                record_ref
                for fact in window_facts
                if fact.collection_status is CollectionStatus.INVALID
                and fact.payload.reason == "unit_unresolved"
                for record_ref in fact.payload.record_refs
            }
            records_attributed = (
                bool(window_facts)
                and available_refs == attributed_refs
                and candidate_refs <= available_refs
                and unresolved_fact_refs <= available_refs
                and all(
                    fact.payload.record_refs
                    for fact in window_facts
                    if fact.collection_status
                    in {CollectionStatus.AVAILABLE, CollectionStatus.INVALID}
                )
            )
            natural_empty = records_attributed and not candidate_refs
            unresolved_only = (
                records_attributed
                and bool(candidate_refs)
                and candidate_refs == candidate_invalid_refs
                and candidate_refs <= unresolved_fact_refs
            )
            if any(
                fact.collection_status is CollectionStatus.UNAVAILABLE
                or (
                    fact.collection_status is CollectionStatus.INVALID
                    and (
                        fact.payload.reason != "unit_unresolved"
                        or not fact.payload.record_refs
                    )
                )
                for _, fact in all_window_facts
            ) or not (natural_empty or unresolved_only):
                continue
            midpoint = (
                window.time_range.start
                + (window.time_range.end - window.time_range.start) / 2
            )
            before = tuple(anchor for anchor in anchors if anchor.midpoint < midpoint)
            after = tuple(anchor for anchor in anchors if anchor.midpoint > midpoint)
            if before and after:
                selected = (before[-1], after[0])
                method = "linear_interpolation"
            else:
                selected = (
                    min(
                        anchors,
                        key=lambda anchor: (
                            abs(_decimal_seconds(anchor.midpoint, midpoint)),
                            anchor.midpoint,
                        ),
                    ),
                )
                method = "nearest_hold"
            candidates.append(
                TemporalCandidate(
                    parameter=parameter,
                    window=window,
                    station_id=station_id,
                    series_identity=series_identity,
                    source=source,
                    method=method,
                    anchors=selected,
                    eligibility_fact_refs=tuple(
                        reference for reference, _ in all_window_facts
                    ),
                    window_record_refs=tuple(
                        reference for reference, _ in window_records
                    ),
                    anchor_origin_refs=tuple(
                        reference for reference, _ in anchor_origins
                    ),
                    direct_window_refs=direct_in_window,
                    unresolved_only=unresolved_only,
                    unit=unit.symbol,
                    precision=unit.precision,
                )
            )
    return tuple(candidates)


def build_temporal_fragment(
    scope: EvidenceSessionScope,
    candidate: TemporalCandidate,
    *,
    registry_digest: str,
) -> tuple[str, EvidenceFragment]:
    """Build the unchanged typed temporal estimate from one selected candidate."""
    window = candidate.window
    parameter = candidate.parameter
    selected = candidate.anchors
    midpoint = (
        window.time_range.start + (window.time_range.end - window.time_range.start) / 2
    )
    if candidate.method == "linear_interpolation":
        if len(selected) != 2:
            raise EvidenceScopeViolationError()
        with localcontext() as context:
            context.prec = 34
            left = Decimal(selected[0].fragment.payload.value)
            right = Decimal(selected[1].fragment.payload.value)
            elapsed = _decimal_seconds(selected[0].midpoint, midpoint)
            span = _decimal_seconds(selected[0].midpoint, selected[1].midpoint)
            value = left + (right - left) * elapsed / span
            value = value.quantize(
                Decimal(1).scaleb(-candidate.precision),
                rounding=ROUND_HALF_EVEN,
            )
    elif candidate.method == "nearest_hold":
        if len(selected) != 1:
            raise EvidenceScopeViolationError()
        value = Decimal(selected[0].fragment.payload.value)
    else:
        raise EvidenceScopeViolationError()
    distances = tuple(
        int(
            abs(_decimal_seconds(anchor.midpoint, midpoint)).to_integral_value(
                rounding=ROUND_CEILING
            )
        )
        for anchor in selected
    )
    identity = "|".join(
        (
            scope.session_id,
            window.window_id,
            parameter.code_system,
            parameter.code,
            candidate.station_id,
            candidate.series_identity,
            candidate.method,
            *(anchor.reference.evidence_id for anchor in selected),
        )
    )
    digest = sha256(identity.encode()).hexdigest()
    operation_id = str(uuid5(NAMESPACE_URL, f"m5:temporal-gap-fill:{identity}"))
    seconds = int((window.time_range.end - window.time_range.start).total_seconds())
    fragment = EvidenceFragment(
        evidence_id=f"evidence:sha256:{digest}",
        source="m5_temporal_gap_fill",
        collection_status=CollectionStatus.AVAILABLE,
        target_comid=scope.agent_comid,
        location=selected[0].fragment.location,
        time_range=window.time_range,
        parameter_refs=(parameter,),
        data_type="temporal_estimate",
        applicability=EvidenceApplicability.ALLOWED_INDIRECT,
        relation_id="temporal_gap_fill_v1",
        series_identity=candidate.series_identity,
        quality_class=EvidenceQualityClass.SUSPICIOUS,
        quality_flags=tuple(
            sorted(
                {
                    DataQualityFlag.TEMPORAL_ESTIMATE,
                    *(
                        (DataQualityFlag.UNIT_UNRESOLVED,)
                        if candidate.unresolved_only
                        else ()
                    ),
                    *(
                        flag
                        for anchor in selected
                        for flag in anchor.fragment.quality_flags
                    ),
                },
                key=lambda flag: flag.value,
            )
        ),
        coverage=EvidenceCoverage(
            status=CoverageStatus.INSUFFICIENT,
            observed_count=0,
            covered_duration_seconds=0,
            window_duration_seconds=seconds,
        ),
        payload=TemporalEstimatePayload(
            value=normalize_canonical_decimal(value),
            unit=candidate.unit,
            method=candidate.method,
            input_evidence_refs=tuple(anchor.reference for anchor in selected),
            anchor_window_ids=tuple(anchor.window_id for anchor in selected),
            anchor_distance_seconds=distances,
        ),
        derivation=EvidenceDerivation(
            algorithm="temporal_gap_fill",
            algorithm_version=1,
            policy_id="temporal_gap_fill_v1",
            registry_digest=registry_digest,
        ),
        provenance=Provenance(
            operation_id=operation_id,
            registry_digest=registry_digest,
        ),
        result_eligibility=EvidenceResultEligibility.INFERRED_ONLY,
    )
    return operation_id, fragment


async def persist_temporal_candidate(
    scope: EvidenceSessionScope,
    candidate: TemporalCandidate,
    *,
    registry_digest: str,
    writer: EvidenceToolWriter,
    reader: EvidenceToolReader,
) -> EvidenceFragmentReference:
    operation_id, fragment = build_temporal_fragment(
        scope, candidate, registry_digest=registry_digest
    )
    scope.cancel_token.checkpoint()
    reference = await writer.write_fragment(
        scope, operation_id=operation_id, fragment=fragment
    )
    if await reader.read_fragment(scope, reference) != fragment:
        raise EvidenceScopeViolationError()
    return reference

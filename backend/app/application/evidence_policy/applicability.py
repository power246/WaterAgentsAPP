from app.application.evidence_policy.models import (
    IndirectEvidenceContext,
    PolicyObservation,
)
from app.application.evidence_policy.temporal import temporal_relation_matches
from app.domain.evidence.evidence_applicability import EvidenceApplicability
from app.domain.evidence.evidence_quality_class import EvidenceQualityClass
from app.application.query_understanding import (
    EvidenceSpatialRelation,
)
from app.domain.hydrology.parameter_capability import ParameterCapability
from app.domain.hydrology.parameter_capability_registry import (
    ParameterCapabilityRegistry,
)
from app.domain.time.time_range import TimeRange


def _direct_measurement_matches(
    registry: ParameterCapabilityRegistry,
    capability: ParameterCapability,
    observation: PolicyObservation,
    target_comid: str,
    target_window: TimeRange,
    *,
    station_segment_confirmed: bool,
    collection_complete: bool,
    reference_complete: bool,
) -> bool:
    policy = registry.get_channel_policy(
        capability.key,
        observation.provider,
        observation.channel,
    )
    if policy is None or not policy.direct_observation:
        return False
    time_matches = (
        observation.observed_time is not None
        and target_window.contains(observation.observed_time)
    ) or (
        observation.valid_time_range is not None
        and observation.valid_time_range.overlaps(target_window)
    )
    return all(
        (
            observation.parameter == capability.key,
            observation.location_comid == target_comid or station_segment_confirmed,
            time_matches,
            observation.method in policy.accepted_methods,
            observation.statistic in policy.accepted_statistics,
            observation.value_semantics is policy.value_semantics,
            observation.medium == policy.medium_constraint,
            observation.fraction == policy.fraction_constraint,
            observation.quality.quality_class
            in {EvidenceQualityClass.VALID, EvidenceQualityClass.SUSPICIOUS},
            collection_complete,
            reference_complete,
        )
    )


def _indirect_relation_matches(
    capability: ParameterCapability,
    context: IndirectEvidenceContext,
    target_window: TimeRange,
) -> bool:
    rule = context.rule
    if not context.reference_complete or rule.output_parameter != capability.key:
        return False
    if set(context.input_parameters) != set(rule.input_parameters):
        return False
    temporal_matches = all(
        temporal_relation_matches(
            rule.temporal_relation,
            source_window,
            target_window,
            analysis_timezone=context.analysis_timezone,
        )
        for source_window in context.input_windows
    )
    if not temporal_matches:
        return False
    if rule.spatial_relation is EvidenceSpatialRelation.SAME_WATERBODY:
        return context.same_location
    if rule.spatial_relation is EvidenceSpatialRelation.UPSTREAM_TO_DOWNSTREAM:
        return context.upstream_result_complete
    return False


def resolve_applicability(
    registry: ParameterCapabilityRegistry,
    capability: ParameterCapability,
    target_comid: str,
    target_window: TimeRange,
    *,
    observation: PolicyObservation | None = None,
    station_segment_confirmed: bool = False,
    collection_complete: bool = False,
    reference_complete: bool = False,
    indirect_context: IndirectEvidenceContext | None = None,
) -> EvidenceApplicability:
    if indirect_context is not None:
        if _indirect_relation_matches(
            capability,
            indirect_context,
            target_window,
        ):
            return EvidenceApplicability.ALLOWED_INDIRECT
        return EvidenceApplicability.CONTEXTUAL_ONLY
    if observation is not None and _direct_measurement_matches(
        registry,
        capability,
        observation,
        target_comid,
        target_window,
        station_segment_confirmed=station_segment_confirmed,
        collection_complete=collection_complete,
        reference_complete=reference_complete,
    ):
        return EvidenceApplicability.DIRECT_MEASUREMENT
    return EvidenceApplicability.CONTEXTUAL_ONLY

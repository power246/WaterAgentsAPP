from datetime import timedelta

from app.application.evidence_policy.models import (
    IndirectEvidenceContext,
    PolicyObservation,
)
from app.domain.evidence.evidence_applicability import EvidenceApplicability
from app.domain.evidence.evidence_quality_class import EvidenceQualityClass
from app.domain.hydrology.evidence_rule_id import EvidenceRuleId
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


def _fixed_relation_inputs_match(
    context: IndirectEvidenceContext,
    expected_inputs: tuple[object, ...],
) -> bool:
    return set(context.input_parameters) == set(expected_inputs)


def _indirect_relation_matches(
    registry: ParameterCapabilityRegistry,
    capability: ParameterCapability,
    context: IndirectEvidenceContext,
    target_window: TimeRange,
) -> bool:
    relation = registry.evidence_relations.get(context.relation_id)
    if (
        relation is None
        or context.relation_id not in capability.allowed_indirect_evidence_rules
        or not context.reference_complete
    ):
        return False
    if relation.same_parameter:
        if any(key != capability.key for key in context.input_parameters):
            return False
    elif (
        capability.key not in relation.output_parameter_keys
        or not _fixed_relation_inputs_match(context, relation.input_parameter_keys)
    ):
        return False

    if context.relation_id is EvidenceRuleId.SAME_PARAMETER_UPSTREAM:
        return context.upstream_result_complete and all(
            source_window == target_window for source_window in context.input_windows
        )
    if context.relation_id is EvidenceRuleId.SAME_LOCATION_ADJACENT_TIME:
        if not context.same_location or len(context.input_windows) != 1:
            return False
        source_window = context.input_windows[0]
        return (
            source_window != target_window
            and source_window.start >= target_window.start - timedelta(days=1)
            and source_window.end <= target_window.end + timedelta(days=1)
        )
    if context.relation_id is EvidenceRuleId.TURBIDITY_PRECIPITATION_DISCHARGE_DIRECTIONAL:
        return all(
            source_window == target_window for source_window in context.input_windows
        )
    if context.relation_id in {
        EvidenceRuleId.DISCHARGE_GAGE_HEIGHT_ESTIMATE,
        EvidenceRuleId.DISCHARGE_BASIN_ESTIMATE,
    }:
        return (
            context.relationship_registered
            and context.model_version is not None
            and all(
                source_window.overlaps(target_window)
                for source_window in context.input_windows
            )
        )
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
            registry,
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

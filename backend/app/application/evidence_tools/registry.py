from collections.abc import Mapping

from app.application.evidence_tools.models import (
    EvidencePolicyClass,
    EvidenceProducerStatus,
    EvidenceToolDescriptor,
    EvidenceToolLimits,
)
from app.application.evidence_tools.scope import EvidenceSessionScope
from app.domain import (
    EvidenceToolRequest,
    EvidenceToolResult,
    InferenceMode,
    MemoryMode,
    ParameterCapabilityRegistry,
)


_ALL_MODES = tuple(InferenceMode)
_PRODUCER_SLOTS = frozenset(
    {"get_upstream_results", "search_transcript_evidence"}
)


class EvidenceToolRegistry:
    def __init__(self, descriptors: tuple[EvidenceToolDescriptor, ...]) -> None:
        by_name = {descriptor.name: descriptor for descriptor in descriptors}
        if len(by_name) != 8 or len(by_name) != len(descriptors):
            raise ValueError("evidence tool registry requires eight unique tools")
        self._descriptors = descriptors
        self._by_name = by_name

    @property
    def descriptors(self) -> tuple[EvidenceToolDescriptor, ...]:
        return self._descriptors

    def get(self, name: str) -> EvidenceToolDescriptor | None:
        return self._by_name.get(name)

    def roster(
        self,
        scope: EvidenceSessionScope,
        *,
        production: bool = True,
    ) -> tuple[EvidenceToolDescriptor, ...]:
        visible: list[EvidenceToolDescriptor] = []
        for descriptor in self._descriptors:
            if scope.mode not in descriptor.allowed_modes:
                continue
            if descriptor.name == "collect_water_observations" and not scope.station_refs:
                continue
            if (
                descriptor.name == "search_transcript_evidence"
                and scope.memory_mode is MemoryMode.STATELESS
            ):
                continue
            if (
                production
                and descriptor.producer_status is EvidenceProducerStatus.UNAVAILABLE
            ):
                continue
            visible.append(descriptor)
        return tuple(visible)


def create_evidence_tool_registry(
    capability_registry: ParameterCapabilityRegistry,
    *,
    limits: EvidenceToolLimits = EvidenceToolLimits(),
    producer_statuses: Mapping[str, EvidenceProducerStatus] | None = None,
) -> EvidenceToolRegistry:
    statuses = dict(producer_statuses or {})
    unknown = set(statuses) - _PRODUCER_SLOTS
    if unknown:
        raise ValueError("producer status override names an unknown producer slot")

    aggregation_methods = tuple(
        sorted(
            {
                method.value
                for profile in capability_registry.aggregation_profiles.values()
                for method in profile.allowed_methods
            }
        )
    )
    target_units = tuple(
        sorted(
            {
                unit_id
                for capability in capability_registry.capabilities
                for unit_id in capability.accepted_source_units
            }
        )
    )

    def descriptor(
        name: str,
        purpose: str,
        policy_class: EvidencePolicyClass,
        scope_requirements: tuple[str, ...],
        *,
        producer_status: EvidenceProducerStatus = EvidenceProducerStatus.READY,
        allowed_option_fields: tuple[str, ...] = (),
        methods: tuple[str, ...] = (),
        units: tuple[str, ...] = (),
        max_records_per_call: int | None = None,
    ) -> EvidenceToolDescriptor:
        maximum_calls = (
            limits.collection_calls
            if policy_class is not EvidencePolicyClass.DETERMINISTIC
            else limits.transform_calls
        )
        return EvidenceToolDescriptor(
            name=name,
            purpose=purpose,
            request_model=EvidenceToolRequest,
            result_model=EvidenceToolResult,
            policy_class=policy_class,
            allowed_modes=_ALL_MODES,
            scope_requirements=scope_requirements,
            max_calls_per_session=maximum_calls,
            max_records_per_call=(
                limits.records_per_operation
                if max_records_per_call is None
                else max_records_per_call
            ),
            max_result_preview_records=limits.preview_max_records,
            max_result_preview_bytes=limits.preview_max_bytes,
            producer_status=producer_status,
            allowed_option_fields=allowed_option_fields,
            allowed_aggregation_methods=methods,
            allowed_target_unit_ids=units,
        )

    return EvidenceToolRegistry(
        (
            descriptor(
                "collect_water_observations",
                "Collect authorized local water observations and discharge.",
                EvidencePolicyClass.CONDITIONAL_MANDATORY,
                ("station_refs", "time_windows", "parameter_capabilities"),
            ),
            descriptor(
                "collect_weather_evidence",
                "Collect bounded contextual weather observations or forecasts.",
                EvidencePolicyClass.OPTIONAL,
                ("target_point", "time_windows"),
                allowed_option_fields=("include_forecast",),
            ),
            descriptor(
                "collect_facility_evidence",
                "Collect facilities along the authorized waterbody corridor.",
                EvidencePolicyClass.OPTIONAL,
                ("geometry_ref", "time_windows"),
                allowed_option_fields=("facility_radius_miles",),
            ),
            descriptor(
                "get_upstream_results",
                "Read completed direct-upstream results from this run.",
                EvidencePolicyClass.PRODUCER_SLOT,
                ("direct_upstream_comids",),
                producer_status=statuses.get(
                    "get_upstream_results",
                    EvidenceProducerStatus.UNAVAILABLE,
                ),
            ),
            descriptor(
                "search_transcript_evidence",
                "Search eligible historical transcript evidence.",
                EvidencePolicyClass.PRODUCER_SLOT,
                ("memory_mode",),
                producer_status=statuses.get(
                    "search_transcript_evidence",
                    EvidenceProducerStatus.UNAVAILABLE,
                ),
                allowed_option_fields=("record_limit",),
                max_records_per_call=limits.inspection_max_page_size,
            ),
            descriptor(
                "convert_evidence_unit",
                "Convert an existing evidence fragment using registered units.",
                EvidencePolicyClass.DETERMINISTIC,
                ("saved_fragment", "parameter_capabilities"),
                allowed_option_fields=("fragment_refs", "target_unit_id"),
                units=target_units,
                max_records_per_call=1,
            ),
            descriptor(
                "aggregate_evidence",
                "Aggregate one saved series using an allowed policy method.",
                EvidencePolicyClass.DETERMINISTIC,
                ("saved_records", "parameter_capabilities", "time_windows"),
                allowed_option_fields=("source_record_refs", "aggregation_method"),
                methods=aggregation_methods,
            ),
            descriptor(
                "inspect_evidence_records",
                "Read a stable bounded page of already saved source records.",
                EvidencePolicyClass.DETERMINISTIC,
                ("saved_records",),
                allowed_option_fields=("source_item_id", "page_offset", "record_limit"),
                max_records_per_call=limits.inspection_max_page_size,
            ),
        )
    )

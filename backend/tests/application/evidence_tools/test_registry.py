from app.application.evidence_tools import (
    EvidencePolicyClass,
    EvidenceProducerStatus,
    create_evidence_tool_registry,
)
from app.domain import EvidenceToolRequest, EvidenceToolResult, MemoryMode
from app.infrastructure.config import load_parameter_capability_registry


TOOL_NAMES = (
    "collect_water_observations",
    "collect_weather_evidence",
    "collect_facility_evidence",
    "get_upstream_results",
    "search_transcript_evidence",
    "convert_evidence_unit",
    "aggregate_evidence",
    "inspect_evidence_records",
)


def names(roster) -> tuple[str, ...]:
    return tuple(item.name for item in roster)


def test_registry_has_exactly_eight_typed_bounded_descriptors() -> None:
    registry = create_evidence_tool_registry(load_parameter_capability_registry())

    assert names(registry.descriptors) == TOOL_NAMES
    assert all(item.request_model is EvidenceToolRequest for item in registry.descriptors)
    assert all(item.result_model is EvidenceToolResult for item in registry.descriptors)
    assert all(item.allowed_modes for item in registry.descriptors)
    assert all(item.scope_requirements for item in registry.descriptors)
    assert all(item.max_calls_per_session > 0 for item in registry.descriptors)
    assert registry.get("collect_water_observations").max_records_per_call == 10_000
    assert registry.get("search_transcript_evidence").max_records_per_call == 200
    assert registry.get("convert_evidence_unit").max_records_per_call == 1
    assert registry.get("inspect_evidence_records").max_records_per_call == 200
    assert all(item.max_result_preview_records == 32 for item in registry.descriptors)
    assert all(item.max_result_preview_bytes == 16_384 for item in registry.descriptors)
    assert registry.get("aggregate_evidence").policy_class is EvidencePolicyClass.DETERMINISTIC
    assert "mean" in registry.get("aggregate_evidence").allowed_aggregation_methods
    assert registry.get("convert_evidence_unit").allowed_target_unit_ids


def test_production_roster_filters_scope_and_unavailable_producer_slots(
    scope_factory,
    context_factory,
) -> None:
    registry = create_evidence_tool_registry(load_parameter_capability_registry())
    stationless = scope_factory(with_station=False)
    stateful = scope_factory(
        context=context_factory(memory_mode=MemoryMode.MEMORY_AUGMENTED)
    )

    assert "collect_water_observations" not in names(registry.roster(stationless))
    assert "search_transcript_evidence" not in names(registry.roster(stateful))
    assert "get_upstream_results" not in names(registry.roster(stateful))
    assert all(
        registry.is_scope_authorized(stationless, descriptor)
        == (descriptor.name in names(registry.roster(stationless, production=False)))
        for descriptor in registry.descriptors
    )
    assert names(registry.descriptors) == TOOL_NAMES


def test_ready_producers_are_visible_only_when_the_scope_allows_them(
    scope_factory,
    context_factory,
) -> None:
    registry = create_evidence_tool_registry(
        load_parameter_capability_registry(),
        producer_statuses={
            "get_upstream_results": EvidenceProducerStatus.READY,
            "search_transcript_evidence": EvidenceProducerStatus.READY,
        },
    )
    stateful = scope_factory(
        context=context_factory(memory_mode=MemoryMode.MEMORY_AUGMENTED),
        with_upstream=True,
    )
    stateless = scope_factory(with_upstream=True)

    assert "get_upstream_results" in names(registry.roster(stateful))
    assert "search_transcript_evidence" in names(registry.roster(stateful))
    assert "search_transcript_evidence" not in names(registry.roster(stateless))
    assert all(
        stateful.mode in descriptor.allowed_modes
        for descriptor in registry.roster(stateful)
    )

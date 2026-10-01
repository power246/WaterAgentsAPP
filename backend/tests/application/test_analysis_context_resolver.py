import asyncio
from datetime import date, datetime, timezone

import pytest

from app.application.query_understanding import (
    AnalysisContextResolutionStatus,
    AnalysisContextResolver,
    ContextDependencyError,
    ClarificationIssue,
    ClarificationSubject,
    DateResolver,
    EvidenceInferenceStrength,
    EvidenceSpatialRelation,
    EvidenceTemporalRelation,
    LocationResolver,
    ParameterConcept,
    ParameterIntentSource,
    ParameterResolver,
    ParsedIntentV1,
    ParsedEvidenceRuleV1,
    SupportingIntentReason,
    TargetSource,
)
from app.application.transcripts.models import AnalysisRequestV1
from app.domain import (
    AdministrativeLocation,
    GeoPoint,
    GeocodingCandidate,
    MemoryMode,
    ModelSelection,
    ParameterCatalogCandidate,
    RiverSegment,
    TimeResolutionPreference,
)
from app.infrastructure.config import load_parameter_capability_registry
from app.ports import PortUnavailableError


POINT = GeoPoint(latitude=38.9, longitude=-77.0)
SEGMENT_POINT = GeoPoint(latitude=38.91, longitude=-77.01)
PARSER_MODEL = ModelSelection(provider="openai", model="parser-model")
WATERBODY_MODEL = ModelSelection(provider="openai", model="waterbody-model")


class CountingClock:
    def __init__(self) -> None:
        self.calls = 0

    def now(self) -> datetime:
        self.calls += 1
        return datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)


class FakeGeocoder:
    def __init__(self) -> None:
        self.calls = 0

    async def resolve_text(
        self,
        location_text: str,
    ) -> tuple[GeocodingCandidate, ...]:
        self.calls += 1
        return ()

    async def reverse(self, point: GeoPoint) -> AdministrativeLocation | None:
        self.calls += 1
        return AdministrativeLocation(
            country_code="US",
            subdivision_code="US-DC",
            display_name="Potomac reach",
            provider_identity="fake:dc",
        )


class FakeComidLookup:
    async def lookup_position(self, point: GeoPoint) -> str | None:
        return "12345678"


class FakeTopology:
    async def get_segment(self, comid: str) -> RiverSegment | None:
        return RiverSegment(comid=comid, location=SEGMENT_POINT)

    async def get_direct_upstreams(
        self,
        comid: str,
        max_distance_km: float,
    ) -> tuple[RiverSegment, ...]:
        return ()


class FakeParameterCatalog:
    async def find_candidates(
        self,
        concept: str,
    ) -> tuple[ParameterCatalogCandidate, ...]:
        return ()


class FailingGeocoder(FakeGeocoder):
    async def reverse(self, point: GeoPoint) -> AdministrativeLocation | None:
        del point
        raise PortUnavailableError(
            "geocoding failed",
            provider="google_geocoding",
            operation="reverse",
        )


class FailingParameterCatalog:
    async def find_candidates(
        self,
        concept: str,
    ) -> tuple[ParameterCatalogCandidate, ...]:
        del concept
        raise PortUnavailableError(
            "catalog failed",
            provider="usgs_waterdata",
            operation="find_parameter_candidates",
        )


def request(*, explicit_auto: bool = True) -> AnalysisRequestV1:
    fields = {
        "query": "Will dissolved oxygen be unsafe in this reach?",
        "parser_model": PARSER_MODEL,
        "waterbody_model": WATERBODY_MODEL,
        "memory_mode": MemoryMode.MEMORY_AUGMENTED,
    }
    if explicit_auto:
        fields["time_resolution"] = TimeResolutionPreference.AUTO
    return AnalysisRequestV1(**fields)


def parsed_intent(
    *,
    ambiguities: tuple[ClarificationIssue, ...] = (),
    concept: str = "dissolved oxygen",
    evidence_rules: tuple[ParsedEvidenceRuleV1, ...] = (),
) -> ParsedIntentV1:
    return ParsedIntentV1(
        coordinate_mention=POINT,
        start_date=date(2026, 9, 25),
        end_date=date(2026, 9, 28),
        parameters=(
            ParameterConcept(
                concept=concept,
                rationale="This is inferred from the user's safety question.",
                explicitly_named=False,
            ),
            ParameterConcept(
                concept=concept,
                rationale="The user explicitly named dissolved oxygen.",
                explicitly_named=True,
            ),
        ),
        evidence_rules=evidence_rules,
        ambiguities=ambiguities,
    )


def resolver():
    clock = CountingClock()
    geocoder = FakeGeocoder()
    registry = load_parameter_capability_registry()
    return (
        AnalysisContextResolver(
            clock=clock,
            location_resolver=LocationResolver(
                geocoder,
                FakeComidLookup(),
                FakeTopology(),
            ),
            date_resolver=DateResolver(),
            parameter_resolver=ParameterResolver(registry, FakeParameterCatalog()),
            capability_registry=registry,
        ),
        clock,
        geocoder,
    )


def run(coroutine):
    return asyncio.run(coroutine)


def test_query_only_inputs_produce_one_fully_validated_analysis_context() -> None:
    context_resolver, clock, _ = resolver()

    result = run(
        context_resolver.resolve(
            request(),
            parsed_intent(),
            analysis_timezone="America/New_York",
        )
    )

    assert result.status is AnalysisContextResolutionStatus.SUCCESS
    assert result.context is not None
    context = result.context
    assert clock.calls == 1
    assert context.decision_instant == datetime(
        2026, 9, 25, 8, 0, tzinfo=timezone.utc
    )
    assert context.target.source is TargetSource.QUERY
    assert context.target.comid == "12345678"
    assert context.params_used[0].source is ParameterIntentSource.EXPLICIT
    assert context.params_used[0].rationale == (
        "The user explicitly named dissolved oxygen."
    )
    assert context.supporting_params[0].reason is (
        SupportingIntentReason.CAPABILITY_REQUIRED
    )
    assert context.parser_model == PARSER_MODEL
    assert context.waterbody_model == WATERBODY_MODEL
    assert context.memory_mode is MemoryMode.MEMORY_AUGMENTED
    assert context.time_resolution_source.value == "auto"


def test_omitted_api_resolution_becomes_12h_default_in_context() -> None:
    context_resolver, _, _ = resolver()

    result = run(
        context_resolver.resolve(
            request(explicit_auto=False),
            parsed_intent(),
            analysis_timezone="America/New_York",
        )
    )

    assert result.context is not None
    assert result.context.time_resolution.value == "12h"
    assert result.context.time_resolution_source.value == "default"


def test_parser_evidence_rules_resolve_to_stable_inherited_baseline() -> None:
    context_resolver, _, _ = resolver()
    rule = ParsedEvidenceRuleV1(
        output_parameter="dissolved oxygen",
        input_parameters=("precipitation", "discharge"),
        temporal_relation=EvidenceTemporalRelation.SAME_WINDOW,
        spatial_relation=EvidenceSpatialRelation.SAME_WATERBODY,
        maximum_inference_strength=EvidenceInferenceStrength.DIRECTIONAL,
        required_collection_attempt=True,
        rationale="Rainfall and flow provide directional context for oxygen.",
    )

    first = run(
        context_resolver.resolve(
            request(),
            parsed_intent(evidence_rules=(rule,)),
            analysis_timezone="America/New_York",
        )
    )
    second = run(
        context_resolver.resolve(
            request(),
            parsed_intent(evidence_rules=(rule,)),
            analysis_timezone="America/New_York",
        )
    )

    assert first.context is not None and second.context is not None
    plan = first.context.evidence_rule_plan
    assert plan.plan_digest == second.context.evidence_rule_plan.plan_digest
    assert plan.appended_rules == ()
    assert len(plan.baseline_rules) == 1
    assert tuple(key.code for key in plan.baseline_rules[0].input_parameters) == (
        "00045",
        "00060",
    )


def test_ambiguous_or_unsupported_rule_concept_fails_closed() -> None:
    context_resolver, _, _ = resolver()

    for concept, expected in (
        ("turbidity", AnalysisContextResolutionStatus.PARAMETER_AMBIGUOUS),
        ("invented signal", AnalysisContextResolutionStatus.PARAMETER_UNSUPPORTED),
    ):
        rule = ParsedEvidenceRuleV1(
            output_parameter="dissolved oxygen",
            input_parameters=(concept,),
            temporal_relation=EvidenceTemporalRelation.SAME_WINDOW,
            spatial_relation=EvidenceSpatialRelation.SAME_WATERBODY,
            maximum_inference_strength=EvidenceInferenceStrength.DIRECTIONAL,
            required_collection_attempt=True,
            rationale="Test failure semantics.",
        )
        result = run(
            context_resolver.resolve(
                request(),
                parsed_intent(evidence_rules=(rule,)),
                analysis_timezone="America/New_York",
            )
        )
        assert result.status is expected
        assert result.context is None


def test_clarification_failure_exposes_no_partial_context_or_port_defaults() -> None:
    context_resolver, clock, geocoder = resolver()
    intent = parsed_intent(
        ambiguities=(
            ClarificationIssue(
                subject=ClarificationSubject.LOCATION,
                message="Two waterways have the same name.",
            ),
        )
    )

    result = run(
        context_resolver.resolve(
            request(),
            intent,
            analysis_timezone="America/New_York",
        )
    )

    assert result.status is (
        AnalysisContextResolutionStatus.QUERY_CLARIFICATION_REQUIRED
    )
    assert result.context is None
    assert clock.calls == 1
    assert geocoder.calls == 0


def test_parameter_failure_exposes_no_partial_context() -> None:
    context_resolver, _, _ = resolver()
    intent = ParsedIntentV1(
        coordinate_mention=POINT,
        start_date=date(2026, 9, 25),
        end_date=date(2026, 9, 25),
        parameters=(
            ParameterConcept(
                concept="invented unsupported pollutant",
                rationale="The user requested it.",
                explicitly_named=True,
            ),
        ),
    )

    result = run(
        context_resolver.resolve(
            request(),
            intent,
            analysis_timezone="America/New_York",
        )
    )

    assert result.status is AnalysisContextResolutionStatus.PARAMETER_UNSUPPORTED
    assert result.context is None


def test_location_port_failure_keeps_location_error_semantics() -> None:
    registry = load_parameter_capability_registry()
    context_resolver = AnalysisContextResolver(
        clock=CountingClock(),
        location_resolver=LocationResolver(
            FailingGeocoder(),
            FakeComidLookup(),
            FakeTopology(),
        ),
        date_resolver=DateResolver(),
        parameter_resolver=ParameterResolver(registry, FakeParameterCatalog()),
        capability_registry=registry,
    )

    async def exercise() -> None:
        with pytest.raises(ContextDependencyError) as captured:
            await context_resolver.resolve(
                request(),
                parsed_intent(),
                analysis_timezone="America/New_York",
            )
        assert captured.value.code == "location_resolution_unavailable"
        assert captured.value.failed_stage == "location_resolution"

    run(exercise())


def test_parameter_catalog_failure_is_not_reported_as_geocoding() -> None:
    registry = load_parameter_capability_registry()
    context_resolver = AnalysisContextResolver(
        clock=CountingClock(),
        location_resolver=LocationResolver(
            FakeGeocoder(),
            FakeComidLookup(),
            FakeTopology(),
        ),
        date_resolver=DateResolver(),
        parameter_resolver=ParameterResolver(
            registry,
            FailingParameterCatalog(),
        ),
        capability_registry=registry,
    )

    async def exercise() -> None:
        with pytest.raises(ContextDependencyError) as captured:
            await context_resolver.resolve(
                request(),
                parsed_intent(concept="not in local registry"),
                analysis_timezone="America/New_York",
            )
        assert captured.value.code == "parameter_catalog_unavailable"
        assert captured.value.failed_stage == "parameter_resolution"

    run(exercise())

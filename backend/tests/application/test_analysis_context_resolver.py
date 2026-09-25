import asyncio
from datetime import date, datetime, timezone

from app.application.query_understanding import (
    AnalysisContextResolutionStatus,
    AnalysisContextResolver,
    ClarificationIssue,
    ClarificationSubject,
    DateResolver,
    LocationResolver,
    ParameterConcept,
    ParameterIntentSource,
    ParameterResolver,
    ParsedIntentV1,
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

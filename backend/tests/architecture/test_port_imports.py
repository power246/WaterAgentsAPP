import ast
import asyncio
import inspect
import sys
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import get_args, get_origin

import pytest

from app.domain import (
    AdministrativeLocation,
    GeoPoint,
    GeocodingCandidate,
    MonitoringStation,
    MonitoringLocationMetadata,
    ParameterCatalogCandidate,
    ParameterKey,
    FacilityEvidenceRequest,
    HistoricalTranscriptRecord,
    ProviderCollectionBatch,
    RiverSegment,
    SamplePeriodOfRecord,
    StationAvailability,
    StationAvailabilityRequest,
    TimeSeriesMetadata,
    TranscriptEvidenceRequest,
    UpstreamResultRecord,
    UpstreamResultRequest,
    UsgsParameterMetadata,
    WaterObservationRequest,
    WeatherEvidenceRequest,
    WeatherForecastRecord,
    WeatherObservationRecord,
    WaterObservationRecord,
    DischargeObservationRecord,
    FacilityRecord,
    ModelSelection,
)
from app.application.query_understanding import (
    ParserPromptMessage,
    QueryParserInputV1,
)
from app.application.reasoning.model_models import (
    WaterbodyJsonSchema,
    WaterbodyModelEvent,
    WaterbodyModelRequest,
    WaterbodyToolDescriptor,
)
from app.ports import (
    CachePort,
    ClockPort,
    ComidLookupPort,
    EventPublisherPort,
    EmbeddingCancellationPort,
    EmbeddingPort,
    FacilityEvidencePort,
    GeocodingPort,
    HistoricalSourceReaderPort,
    InvalidPortResponseError,
    KeyStorePort,
    LLMPort,
    ParameterCatalogPort,
    PortAuthenticationError,
    PortConfigurationError,
    PortError,
    PortHttpError,
    PortRateLimitError,
    PortResponseLimitError,
    PortTimeoutError,
    PortUnavailableError,
    RAGIndexPort,
    RAGJobStorePort,
    StationMetadataPort,
    StationInventoryPort,
    TopologyDataPort,
    TranscriptReadStorePort,
    TranscriptEvidencePort,
    TranscriptStorePort,
    UpstreamResultPort,
    WaterObservationPort,
    WeatherEvidencePort,
    UnsupportedPortVersionError,
)
from app.ports.waterbody_model import WaterbodyModelPort

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
PORTS_ROOT = APP_ROOT / "ports"


def _is_allowed_port_import(source_path: Path, import_name: str) -> bool:
    root = import_name.split(".", maxsplit=1)[0]
    return (
        root in sys.stdlib_module_names
        or import_name == "app.domain"
        or import_name.startswith("app.domain.")
        or import_name == "app.ports"
        or import_name.startswith("app.ports.")
        or (
            source_path.name == "waterbody_model.py"
            and import_name == "app.application.reasoning.model_models"
        )
    )


def _imported_modules(source_path: Path) -> tuple[str, ...]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=source_path)
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return tuple(modules)


def test_all_port_contracts_are_public_protocols() -> None:
    protocols = (
        CachePort,
        ClockPort,
        ComidLookupPort,
        EventPublisherPort,
        EmbeddingCancellationPort,
        EmbeddingPort,
        FacilityEvidencePort,
        GeocodingPort,
        HistoricalSourceReaderPort,
        KeyStorePort,
        LLMPort,
        ParameterCatalogPort,
        RAGIndexPort,
        RAGJobStorePort,
        StationMetadataPort,
        StationInventoryPort,
        TopologyDataPort,
        TranscriptReadStorePort,
        TranscriptStorePort,
        TranscriptEvidencePort,
        UpstreamResultPort,
        WaterObservationPort,
        WaterbodyModelPort,
        WeatherEvidencePort,
    )

    assert all(getattr(protocol, "_is_protocol", False) for protocol in protocols)


def test_port_method_sync_and_async_semantics() -> None:
    assert not inspect.iscoroutinefunction(ClockPort.now)
    assert not inspect.iscoroutinefunction(EmbeddingCancellationPort.is_requested)
    assert not inspect.iscoroutinefunction(WaterbodyModelPort.stream)
    async_methods = (
        GeocodingPort.resolve_text,
        GeocodingPort.reverse,
        ComidLookupPort.lookup_position,
        TopologyDataPort.get_segment,
        TopologyDataPort.get_direct_upstreams,
        TopologyDataPort.get_monitoring_sites,
        ParameterCatalogPort.find_candidates,
        StationMetadataPort.get_parameter_codes,
        StationMetadataPort.get_monitoring_locations,
        StationMetadataPort.get_time_series_metadata,
        StationMetadataPort.get_sample_periods,
        StationInventoryPort.check_availability,
        WaterObservationPort.collect,
        WeatherEvidencePort.collect,
        FacilityEvidencePort.collect,
        UpstreamResultPort.collect,
        TranscriptEvidencePort.collect,
        LLMPort.generate,
        TranscriptStorePort.get_creation,
        TranscriptStorePort.create_run,
        TranscriptStorePort.verify_creation,
        TranscriptStorePort.write_batch,
        TranscriptStorePort.verify_batch,
        TranscriptReadStorePort.list_runs,
        TranscriptReadStorePort.read_run,
        EmbeddingPort.embed,
        HistoricalSourceReaderPort.read_case,
        RAGIndexPort.write_ready_projection,
        RAGIndexPort.search_candidates,
        RAGIndexPort.read_active_generation,
        RAGIndexPort.create_building_generation,
        RAGIndexPort.activate_generation,
        RAGIndexPort.retire_generation,
        RAGIndexPort.delete_generation,
        RAGJobStorePort.enqueue,
        RAGJobStorePort.claim,
        RAGJobStorePort.save,
        RAGJobStorePort.counts,
        EventPublisherPort.publish,
        CachePort.get,
        CachePort.set,
        CachePort.delete,
        CachePort.clear_namespace,
        KeyStorePort.get,
        KeyStorePort.set,
        KeyStorePort.delete,
    )

    assert all(inspect.iscoroutinefunction(method) for method in async_methods)


def test_waterbody_model_stream_signature_is_provider_neutral() -> None:
    stream = inspect.signature(WaterbodyModelPort.stream)

    assert tuple(stream.parameters) == ("self", "request")
    assert stream.parameters["request"].annotation is WaterbodyModelRequest
    assert get_origin(stream.return_annotation) is AsyncIterator
    assert get_args(stream.return_annotation) == (WaterbodyModelEvent,)


@pytest.mark.parametrize(
    ("method", "parameters"),
    [
        (ClockPort.now, ("self",)),
        (GeocodingPort.resolve_text, ("self", "location_text", "deadline")),
        (GeocodingPort.reverse, ("self", "point", "deadline")),
        (ComidLookupPort.lookup_position, ("self", "point", "deadline")),
        (TopologyDataPort.get_segment, ("self", "comid", "deadline")),
        (
            TopologyDataPort.get_direct_upstreams,
            ("self", "comid", "max_distance_km", "deadline"),
        ),
        (
            TopologyDataPort.get_monitoring_sites,
            ("self", "comid", "deadline"),
        ),
        (ParameterCatalogPort.find_candidates, ("self", "concept")),
        (
            StationMetadataPort.get_parameter_codes,
            ("self", "parameters", "deadline"),
        ),
        (
            StationMetadataPort.get_monitoring_locations,
            ("self", "station_ids", "deadline"),
        ),
        (
            StationMetadataPort.get_time_series_metadata,
            ("self", "station_ids", "parameters", "deadline"),
        ),
        (
            StationMetadataPort.get_sample_periods,
            ("self", "station_id", "deadline"),
        ),
        (
            StationInventoryPort.check_availability,
            ("self", "request", "deadline"),
        ),
        (WaterObservationPort.collect, ("self", "request", "deadline")),
        (WeatherEvidencePort.collect, ("self", "request", "deadline")),
        (FacilityEvidencePort.collect, ("self", "request", "deadline")),
        (UpstreamResultPort.collect, ("self", "request", "deadline")),
        (TranscriptEvidencePort.collect, ("self", "request", "deadline")),
        (TranscriptStorePort.get_creation, ("self", "idempotency_key")),
        (TranscriptStorePort.create_run, ("self", "candidate")),
        (TranscriptStorePort.verify_creation, ("self", "candidate")),
        (TranscriptStorePort.write_batch, ("self", "batch")),
        (TranscriptStorePort.verify_batch, ("self", "batch")),
        (
            TranscriptReadStorePort.list_runs,
            ("self", "limit", "after_created_at", "after_run_id"),
        ),
        (TranscriptReadStorePort.read_run, ("self", "run_id")),
        (
            EmbeddingPort.embed,
            ("self", "profile", "texts", "deadline", "cancel_token"),
        ),
        (HistoricalSourceReaderPort.read_case, ("self", "case_ref")),
        (
            RAGIndexPort.write_ready_projection,
            ("self", "job", "case", "fragments"),
        ),
        (RAGIndexPort.search_candidates, ("self", "query")),
        (RAGIndexPort.read_active_generation, ("self",)),
        (
            RAGIndexPort.create_building_generation,
            (
                "self",
                "profile",
                "source_cutoff_created_at",
                "source_cutoff_item_id",
            ),
        ),
        (
            RAGIndexPort.activate_generation,
            ("self", "generation_id", "activated_at"),
        ),
        (
            RAGIndexPort.retire_generation,
            ("self", "generation_id", "retired_at"),
        ),
        (RAGIndexPort.delete_generation, ("self", "generation_id")),
        (RAGJobStorePort.enqueue, ("self", "job")),
        (
            RAGJobStorePort.claim,
            ("self", "generation_id", "lease_owner", "lease_until", "limit"),
        ),
        (RAGJobStorePort.save, ("self", "job")),
        (RAGJobStorePort.counts, ("self", "generation_id")),
        (CachePort.get, ("self", "namespace", "key")),
        (CachePort.set, ("self", "namespace", "key", "value")),
        (CachePort.delete, ("self", "namespace", "key")),
        (CachePort.clear_namespace, ("self", "namespace")),
    ],
)
def test_port_method_signatures(method, parameters: tuple[str, ...]) -> None:
    assert tuple(inspect.signature(method).parameters) == parameters


def test_stable_ports_use_domain_types() -> None:
    geocoding = inspect.signature(GeocodingPort.resolve_text)
    reverse_geocoding = inspect.signature(GeocodingPort.reverse)
    comid_lookup = inspect.signature(ComidLookupPort.lookup_position)
    topology = inspect.signature(TopologyDataPort.get_segment)
    upstreams = inspect.signature(TopologyDataPort.get_direct_upstreams)
    associated_sites = inspect.signature(TopologyDataPort.get_monitoring_sites)
    parameter_catalog = inspect.signature(ParameterCatalogPort.find_candidates)
    parameter_metadata = inspect.signature(StationMetadataPort.get_parameter_codes)
    locations = inspect.signature(StationMetadataPort.get_monitoring_locations)
    series = inspect.signature(StationMetadataPort.get_time_series_metadata)
    samples = inspect.signature(StationMetadataPort.get_sample_periods)
    availability = inspect.signature(StationInventoryPort.check_availability)
    water = inspect.signature(WaterObservationPort.collect)
    weather = inspect.signature(WeatherEvidencePort.collect)
    facility = inspect.signature(FacilityEvidencePort.collect)
    upstream = inspect.signature(UpstreamResultPort.collect)
    transcript = inspect.signature(TranscriptEvidencePort.collect)

    assert geocoding.parameters["location_text"].annotation is str
    assert geocoding.return_annotation == tuple[GeocodingCandidate, ...]
    assert reverse_geocoding.parameters["point"].annotation is GeoPoint
    assert reverse_geocoding.return_annotation == AdministrativeLocation | None
    assert comid_lookup.parameters["point"].annotation is GeoPoint
    assert comid_lookup.return_annotation == str | None
    assert topology.return_annotation == RiverSegment | None
    assert upstreams.return_annotation == tuple[RiverSegment, ...]
    assert associated_sites.return_annotation == tuple[MonitoringStation, ...]
    assert parameter_catalog.parameters["concept"].annotation is str
    assert parameter_catalog.return_annotation == tuple[ParameterCatalogCandidate, ...]
    assert parameter_metadata.return_annotation == tuple[UsgsParameterMetadata, ...]
    assert locations.return_annotation == tuple[MonitoringLocationMetadata, ...]
    assert series.return_annotation == tuple[TimeSeriesMetadata, ...]
    assert samples.return_annotation == tuple[SamplePeriodOfRecord, ...]
    assert availability.parameters["request"].annotation is StationAvailabilityRequest
    assert availability.return_annotation == tuple[StationAvailability, ...]
    assert water.parameters["request"].annotation is WaterObservationRequest
    assert water.return_annotation == ProviderCollectionBatch[
        WaterObservationRecord | DischargeObservationRecord
    ]
    assert weather.parameters["request"].annotation is WeatherEvidenceRequest
    assert weather.return_annotation == ProviderCollectionBatch[
        WeatherObservationRecord | WeatherForecastRecord
    ]
    assert facility.parameters["request"].annotation is FacilityEvidenceRequest
    assert facility.return_annotation == ProviderCollectionBatch[FacilityRecord]
    assert upstream.parameters["request"].annotation is UpstreamResultRequest
    assert upstream.return_annotation == ProviderCollectionBatch[UpstreamResultRecord]
    assert transcript.parameters["request"].annotation is TranscriptEvidenceRequest
    assert transcript.return_annotation == ProviderCollectionBatch[
        HistoricalTranscriptRecord
    ]


def test_deferred_ports_are_generic_without_guessing_dtos() -> None:
    generic_ports = (
        LLMPort[int, str],
        TranscriptStorePort[str, int, float, bytes],
        TranscriptReadStorePort[str, int],
        EventPublisherPort[str],
        CachePort[str],
        KeyStorePort[str],
    )

    assert all(get_origin(port) is not None for port in generic_ports)


@pytest.mark.parametrize(
    "error_type",
    [
        PortUnavailableError,
        PortTimeoutError,
        InvalidPortResponseError,
        PortAuthenticationError,
        PortConfigurationError,
        PortHttpError,
        PortRateLimitError,
        PortResponseLimitError,
        UnsupportedPortVersionError,
    ],
)
def test_port_errors_share_a_stable_base(error_type: type[PortError]) -> None:
    error = error_type("provider failed")

    assert isinstance(error, PortError)
    assert str(error) == "provider failed"


def test_not_found_contracts_use_none_or_empty_tuple() -> None:
    class EmptyGeocoder:
        async def resolve_text(
            self,
            location_text: str,
        ) -> tuple[GeocodingCandidate, ...]:
            return ()

        async def reverse(self, point: GeoPoint) -> AdministrativeLocation | None:
            return None

    class EmptyTopology:
        async def get_segment(self, comid: str) -> RiverSegment | None:
            return None

        async def get_direct_upstreams(
            self,
            comid: str,
            max_distance_km: float,
        ) -> tuple[RiverSegment, ...]:
            return ()

        async def get_monitoring_sites(
            self,
            comid: str,
        ) -> tuple[MonitoringStation, ...]:
            return ()

    async def exercise() -> None:
        geocoder: GeocodingPort = EmptyGeocoder()
        topology: TopologyDataPort = EmptyTopology()

        assert await geocoder.resolve_text("unknown") == ()
        assert await geocoder.reverse(GeoPoint(latitude=1.0, longitude=1.0)) is None
        assert await topology.get_segment("0001") is None
        assert await topology.get_direct_upstreams("0001", 10) == ()
        assert await topology.get_monitoring_sites("0001") == ()

    asyncio.run(exercise())


def test_clock_contract_is_synchronous_and_returns_utc_time() -> None:
    class FixedClock:
        def now(self) -> datetime:
            return datetime(2026, 1, 1, tzinfo=timezone.utc)

    clock: ClockPort = FixedClock()

    assert clock.now() == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_query_protocols_document_empty_and_unavailable_as_distinct() -> None:
    protocols = (
        GeocodingPort,
        ComidLookupPort,
        TopologyDataPort,
        StationMetadataPort,
        StationInventoryPort,
        ParameterCatalogPort,
        WaterObservationPort,
        WeatherEvidencePort,
        FacilityEvidencePort,
        UpstreamResultPort,
        TranscriptEvidencePort,
    )

    for protocol in protocols:
        documentation = inspect.getdoc(protocol) or ""
        assert "unavailab" in documentation.lower()
        assert "none" in documentation.lower() or "empty" in documentation.lower()


def test_port_modules_have_no_forbidden_dependencies_or_placeholder_payloads() -> None:
    forbidden_names = {"Any", "EvidenceFragment"}
    violations: list[str] = []

    for source_path in PORTS_ROOT.glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=source_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if not _is_allowed_port_import(source_path, alias.name):
                        violations.append(f"{source_path.name}: imports {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if not _is_allowed_port_import(source_path, node.module):
                    violations.append(f"{source_path.name}: imports {node.module}")
            elif isinstance(node, ast.Name) and node.id in forbidden_names:
                violations.append(f"{source_path.name}: uses {node.id}")
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "dict"
            ):
                violations.append(f"{source_path.name}: uses a raw dict type")

    assert not violations, "\n".join(violations)


def test_waterbody_port_application_dto_exception_is_narrow() -> None:
    waterbody_port = PORTS_ROOT / "waterbody_model.py"
    another_port = PORTS_ROOT / "llm.py"

    assert _is_allowed_port_import(
        waterbody_port,
        "app.application.reasoning.model_models",
    )
    assert not _is_allowed_port_import(
        waterbody_port,
        "app.application.query_understanding.parser_models",
    )
    assert not _is_allowed_port_import(
        another_port,
        "app.application.reasoning.model_models",
    )
    assert not _is_allowed_port_import(waterbody_port, "app.infrastructure.http")


def test_query_parser_and_waterbody_model_boundaries_remain_separate() -> None:
    parser_root = APP_ROOT / "application" / "query_understanding"
    violations = [
        f"{source_path.relative_to(APP_ROOT)} imports {module}"
        for source_path in sorted(parser_root.rglob("*.py"))
        for module in _imported_modules(source_path)
        if module == "app.application.reasoning"
        or module.startswith("app.application.reasoning.")
        or module == "app.ports.waterbody_model"
        or module.startswith("app.ports.waterbody_model.")
    ]

    waterbody_port = PORTS_ROOT / "waterbody_model.py"
    if waterbody_port.is_file():
        violations.extend(
            f"{waterbody_port.relative_to(APP_ROOT)} imports {module}"
            for module in _imported_modules(waterbody_port)
            if module == "app.application.query_understanding"
            or module.startswith("app.application.query_understanding.")
        )

    assert not violations, "\n".join(violations)


def test_query_parser_contract_remains_tool_free() -> None:
    messages = (
        ParserPromptMessage(role="system", text="instructions"),
        ParserPromptMessage(role="user", text="request"),
    )
    model = ModelSelection(provider="openai", model="parser")

    assert QueryParserInputV1(model=model, messages=messages).tools == ()
    with pytest.raises(ValueError, match="tools must be empty"):
        QueryParserInputV1(
            model=model,
            messages=messages,
            tools=("collect_water_observations",),
        )

    waterbody_descriptor = WaterbodyToolDescriptor(
        name="collect_water_observations",
        description="Collect authorized water observations.",
        input_schema=WaterbodyJsonSchema(
            name="collect_water_observations",
            schema_text='{"type":"object","additionalProperties":false}',
        ),
    )
    with pytest.raises(ValueError):
        QueryParserInputV1(
            model=model,
            messages=messages,
            tools=(waterbody_descriptor,),
        )


def test_port_package_contains_only_protocols_and_errors() -> None:
    violations: list[str] = []

    for source_path in PORTS_ROOT.glob("*.py"):
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=source_path)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            if source_path.name == "errors.py":
                continue
            base_names = {
                base.id
                for base in node.bases
                if isinstance(base, ast.Name)
            }
            base_names.update(
                base.value.id
                for base in node.bases
                if isinstance(base, ast.Subscript)
                and isinstance(base.value, ast.Name)
            )
            if "Protocol" not in base_names:
                violations.append(f"{source_path.name}: {node.name} is not a Protocol")

    assert not violations, "\n".join(violations)

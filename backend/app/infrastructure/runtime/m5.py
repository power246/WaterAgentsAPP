from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from app.application.analysis_runs.workflow import WorkflowOperations
from app.application.evidence_tools import (
    EvidenceAcquisitionService,
    EvidenceProducerStatus,
    EvidenceSessionScope,
    EvidenceToolExecutor,
    EvidenceToolLimits,
    EvidenceToolRegistry,
    EvidenceTranscriptStore,
    RunLocalSingleFlight,
    RunLocalUpstreamResultRegistry,
    create_evidence_tool_registry,
)
from app.application.transcripts.reader import TranscriptReader
from app.domain import ParameterCapabilityRegistry, WaterbodyGeometry
from app.infrastructure.adapters import (
    EpaEchoFacilityAdapter,
    NwsWeatherAdapter,
    UsgsObservationAdapter,
)
from app.infrastructure.config import AppSettings, SettingsConfigurationError
from app.infrastructure.http import ProviderHttpClient
from app.ports import (
    FacilityEvidencePort,
    TranscriptEvidencePort,
    UpstreamResultPort,
    WaterObservationPort,
    WeatherEvidencePort,
)
from app.ports.clock import ClockPort


class TrustedGeometryCatalog:
    """Resolve only geometries carried by an already sealed evidence scope."""

    def __init__(self) -> None:
        self._geometries: dict[str, WaterbodyGeometry] = {}
        self._by_run: dict[str, set[str]] = {}

    def register_scope(self, scope: EvidenceSessionScope) -> None:
        geometry_ref = scope.geometry_ref
        if geometry_ref is None:
            return
        geometry = scope.topology.geometries.get(geometry_ref)
        if geometry is None:
            raise ValueError("sealed evidence scope geometry is unavailable")
        existing = self._geometries.get(geometry_ref)
        if existing is not None and existing != geometry:
            raise ValueError("trusted geometry identity collision")
        self._geometries[geometry_ref] = geometry
        self._by_run.setdefault(scope.run_id, set()).add(geometry_ref)

    def resolve(self, geometry_ref: str) -> WaterbodyGeometry | None:
        return self._geometries.get(geometry_ref)

    def cleanup_run(self, run_id: str) -> None:
        for geometry_ref in self._by_run.pop(run_id, set()):
            if not any(
                geometry_ref in references
                for references in self._by_run.values()
            ):
                self._geometries.pop(geometry_ref, None)


@dataclass(frozen=True, slots=True)
class M5Runtime:
    registry: EvidenceToolRegistry
    transcript: EvidenceTranscriptStore
    executor: EvidenceToolExecutor
    acquisition: EvidenceAcquisitionService
    geometry_catalog: TrustedGeometryCatalog | None = None
    upstream_results: RunLocalUpstreamResultRegistry | None = None

    async def cleanup_run(self, run_id: UUID | str) -> None:
        identity = str(run_id)
        await self.executor.cleanup_run(identity)
        if self.geometry_catalog is not None:
            self.geometry_catalog.cleanup_run(identity)
        if self.upstream_results is not None:
            await self.upstream_results.cleanup_run(run_id)


def evidence_tool_limits(settings: AppSettings) -> EvidenceToolLimits:
    return EvidenceToolLimits(
        provider_timeout_seconds=settings.evidence_provider_timeout_seconds,
        total_deadline_seconds=settings.evidence_total_deadline_seconds,
        collection_calls=settings.evidence_max_collection_calls,
        transform_calls=settings.evidence_max_transform_calls,
        pages_per_operation=settings.evidence_max_pages,
        records_per_operation=settings.evidence_max_records,
        response_bytes_per_request=settings.evidence_max_response_bytes,
        inspection_default_page_size=settings.evidence_inspection_page_size,
        inspection_max_page_size=settings.evidence_inspection_max_page_size,
        preview_max_records=settings.evidence_preview_max_records,
        preview_max_bytes=settings.evidence_preview_max_bytes,
        facility_max_centers=settings.evidence_facility_max_centers,
    )


def compose_m5_runtime(
    settings: AppSettings,
    *,
    operations: WorkflowOperations,
    transcript_reader: TranscriptReader,
    capability_registry: ParameterCapabilityRegistry,
    water_port: WaterObservationPort,
    weather_port: WeatherEvidencePort,
    facility_port: FacilityEvidencePort,
    clock: ClockPort,
    upstream_port: UpstreamResultPort | None = None,
    transcript_port: TranscriptEvidencePort | None = None,
    operation_id_factory: Callable[[], str] | None = None,
    scope_preparer: Callable[[EvidenceSessionScope], None] | None = None,
    geometry_catalog: TrustedGeometryCatalog | None = None,
    upstream_results: RunLocalUpstreamResultRegistry | None = None,
) -> M5Runtime:
    required = {
        "operations": operations,
        "transcript_reader": transcript_reader,
        "capability_registry": capability_registry,
        "water_port": water_port,
        "weather_port": weather_port,
        "facility_port": facility_port,
        "clock": clock,
    }
    missing = sorted(name for name, value in required.items() if value is None)
    if missing:
        raise SettingsConfigurationError(
            "M5 runtime requires: " + ", ".join(missing)
        )

    limits = evidence_tool_limits(settings)
    statuses: dict[str, EvidenceProducerStatus] = {}
    if upstream_port is not None:
        statuses["get_upstream_results"] = EvidenceProducerStatus.READY
    if transcript_port is not None:
        statuses["search_transcript_evidence"] = EvidenceProducerStatus.READY
    registry = create_evidence_tool_registry(
        capability_registry,
        limits=limits,
        producer_statuses=statuses,
    )
    transcript = EvidenceTranscriptStore(
        operations=operations,
        reader=transcript_reader,
        capability_registry=capability_registry,
    )
    single_flight = RunLocalSingleFlight()
    executor = EvidenceToolExecutor(
        registry=registry,
        capability_registry=capability_registry,
        writer=transcript,
        reader=transcript,
        water_port=water_port,
        weather_port=weather_port,
        facility_port=facility_port,
        upstream_port=upstream_port,
        transcript_port=transcript_port,
        limits=limits,
        clock=clock,
        operation_id_factory=operation_id_factory,
        single_flight=single_flight,
        scope_preparer=scope_preparer,
    )
    acquisition = EvidenceAcquisitionService(
        registry=registry,
        executor=executor,
        reader=transcript,
        writer=transcript,
        capability_registry=capability_registry,
        clock=clock,
    )
    return M5Runtime(
        registry=registry,
        transcript=transcript,
        executor=executor,
        acquisition=acquisition,
        geometry_catalog=geometry_catalog,
        upstream_results=upstream_results,
    )


@asynccontextmanager
async def build_m5_runtime(
    settings: AppSettings,
    *,
    operations: WorkflowOperations,
    transcript_reader: TranscriptReader,
    capability_registry: ParameterCapabilityRegistry,
    clock: ClockPort,
    upstream_results: RunLocalUpstreamResultRegistry | None = None,
) -> AsyncIterator[M5Runtime]:
    geometry_catalog = TrustedGeometryCatalog()
    api_key = (
        settings.usgs_api_key.get_secret_value()
        if settings.usgs_api_key is not None
        else None
    )
    async with ProviderHttpClient(
        timeout_seconds=settings.evidence_provider_timeout_seconds,
        max_response_bytes=settings.evidence_max_response_bytes,
        retry_delay_seconds=settings.provider_http_retry_delay_seconds,
        attempts=settings.evidence_provider_attempts,
    ) as provider_http:
        water = UsgsObservationAdapter(
            provider_http,
            api_key=api_key,
            page_size=min(
                settings.provider_http_page_size,
                settings.evidence_max_records,
            ),
            max_pages=settings.evidence_max_pages,
            max_records=settings.evidence_max_records,
            clock=clock.now,
        )
        weather = NwsWeatherAdapter(
            provider_http,
            user_agent_project=settings.nws_user_agent_project,
            user_agent_contact=settings.nws_user_agent_contact,
            page_size=min(settings.provider_http_page_size, 500),
            max_pages=settings.evidence_max_pages,
            max_records=settings.evidence_max_records,
            clock=clock.now,
        )
        facility = EpaEchoFacilityAdapter(
            provider_http,
            geometry_resolver=geometry_catalog.resolve,
            response_set=min(settings.provider_http_page_size, 1000),
            max_pages=settings.evidence_max_pages,
            max_records=settings.evidence_max_records,
            clock=clock.now,
        )
        yield compose_m5_runtime(
            settings,
            operations=operations,
            transcript_reader=transcript_reader,
            capability_registry=capability_registry,
            water_port=water,
            weather_port=weather,
            facility_port=facility,
            clock=clock,
            upstream_port=upstream_results,
            scope_preparer=geometry_catalog.register_scope,
            geometry_catalog=geometry_catalog,
            upstream_results=upstream_results,
        )

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from uuid import UUID

from pydantic import SecretStr

from app.application.analysis_runs.query_topology_workflow import (
    QueryTopologyWorkflow,
    TopologyBuildingStage,
)
from app.application.analysis_runs.workflow import AnalysisWorkflow
from app.application.hydrology_topology import HydrologyGraphBuilder
from app.application.query_understanding import (
    AnalysisContextResolver,
    DateResolver,
    LocationResolver,
    ParameterResolver,
    QueryContextResolver,
    QueryParserPort,
    QueryParserPromptBuilder,
    QueryParserWorkflow,
)
from app.application.station_availability import StationAvailabilityService
from app.domain import ModelRole, TopologyLimits
from app.domain.hydrology import ParameterCapabilityRegistry
from app.infrastructure.adapters import (
    GoogleGeocodingAdapter,
    NldiAdapter,
    OpenAIResponsesParserAdapter,
    UsgsWaterDataAdapter,
)
from app.infrastructure.config import AppSettings, SettingsConfigurationError
from app.infrastructure.http import ProviderHttpClient
from app.ports.clock import ClockPort
from app.ports.settings_store import SettingsStorePort


@dataclass(frozen=True)
class RuntimeComponents:
    workflow: AnalysisWorkflow | None
    query_topology_workflow: QueryTopologyWorkflow | None = None


def compose_m4_workflow(
    settings: AppSettings,
    *,
    parser: QueryParserPort,
    context_resolver: QueryContextResolver,
    topology_builder: TopologyBuildingStage,
    parameter_capabilities: ParameterCapabilityRegistry,
    error_id_factory: Callable[[], UUID],
    private_values: tuple[str, ...] = (),
    unit_policy_store: SettingsStorePort | None = None,
) -> QueryTopologyWorkflow:
    parser_workflow = QueryParserWorkflow(
        parser,
        context_resolver,
        QueryParserPromptBuilder(parameter_capabilities),
        error_id_factory=error_id_factory,
        timeout_seconds=settings.query_parser_timeout_seconds,
        private_values=private_values,
        capability_registry=parameter_capabilities,
        unit_policy_store=unit_policy_store,
    )
    return QueryTopologyWorkflow(parser_workflow, topology_builder)


@asynccontextmanager
async def build_m4_runtime(
    settings: AppSettings,
    *,
    clock: ClockPort,
    parameter_capabilities: ParameterCapabilityRegistry,
    error_id_factory: Callable[[], UUID],
    provider_http: ProviderHttpClient | None = None,
    unit_policy_store: SettingsStorePort | None = None,
) -> AsyncIterator[RuntimeComponents]:
    parser_providers = {
        capability.provider.casefold()
        for capability in settings.model_catalog
        if ModelRole.QUERY_PARSER in capability.roles
    }
    if not parser_providers:
        raise SettingsConfigurationError(
            "model_catalog must configure at least one query_parser model"
        )
    if not any(
        ModelRole.WATERBODY in capability.roles
        for capability in settings.model_catalog
    ):
        raise SettingsConfigurationError(
            "model_catalog must configure at least one waterbody model"
        )
    unsupported = parser_providers - {"openai"}
    if unsupported:
        raise SettingsConfigurationError(
            "unsupported query_parser provider configuration: "
            + ", ".join(sorted(unsupported))
        )
    openai_key = _required_secret(settings.openai_api_key, "openai_api_key")
    google_key = _required_secret(
        settings.google_geocoding_api_key,
        "google_geocoding_api_key",
    )
    usgs_key = (
        settings.usgs_api_key.get_secret_value()
        if settings.usgs_api_key is not None
        else None
    )

    async with AsyncExitStack() as stack:
        parser_http = provider_http or await stack.enter_async_context(
            _http_client(settings, timeout=settings.query_parser_timeout_seconds)
        )
        geocoding_http = provider_http or await stack.enter_async_context(
            _http_client(settings, timeout=settings.geocoding_timeout_seconds)
        )
        metadata_http = provider_http or await stack.enter_async_context(
            _http_client(settings, timeout=settings.metadata_timeout_seconds)
        )
        parser = OpenAIResponsesParserAdapter(
            parser_http,
            api_key=openai_key,
            base_url=settings.openai_base_url,
        )
        geocoding = GoogleGeocodingAdapter(
            geocoding_http,
            api_key=google_key,
        )
        nldi = NldiAdapter(metadata_http)
        usgs = UsgsWaterDataAdapter(
            metadata_http,
            api_key=usgs_key,
            page_size=settings.provider_http_page_size,
            max_pages=settings.provider_http_max_pages,
            max_records=settings.provider_http_max_records,
        )
        context_resolver = AnalysisContextResolver(
            clock=clock,
            location_resolver=LocationResolver(geocoding, nldi, nldi),
            date_resolver=DateResolver(),
            parameter_resolver=ParameterResolver(
                parameter_capabilities,
                usgs,
                max_target_parameters=settings.max_target_parameters,
            ),
            capability_registry=parameter_capabilities,
        )
        station_inventory = StationAvailabilityService(
            topology=nldi,
            metadata=usgs,
            capability_registry=parameter_capabilities,
            clock=clock,
        )
        topology_builder = HydrologyGraphBuilder(
            topology=nldi,
            station_inventory=station_inventory,
            limits=TopologyLimits(
                max_distance_km=settings.topology_max_distance_km,
                max_hops=settings.topology_max_hops,
                max_nodes=settings.topology_max_nodes,
            ),
            max_concurrency=settings.topology_max_concurrency,
            stage_timeout_seconds=settings.topology_stage_timeout_seconds,
            prediction_baseline_hours=settings.prediction_baseline_hours,
        )
        private_values = tuple(
            value for value in (openai_key, google_key, usgs_key) if value
        )
        workflow = compose_m4_workflow(
            settings,
            parser=parser,
            unit_policy_store=unit_policy_store,
            context_resolver=context_resolver,
            topology_builder=topology_builder,
            parameter_capabilities=parameter_capabilities,
            error_id_factory=error_id_factory,
            private_values=private_values,
        )
        yield RuntimeComponents(
            workflow=workflow,
            query_topology_workflow=workflow,
        )


def _http_client(
    settings: AppSettings,
    *,
    timeout: float,
) -> ProviderHttpClient:
    return ProviderHttpClient(
        timeout_seconds=timeout,
        max_response_bytes=settings.provider_http_max_response_bytes,
        retry_delay_seconds=settings.provider_http_retry_delay_seconds,
    )


def _required_secret(value: SecretStr | None, field_name: str) -> str:
    if value is None:
        raise SettingsConfigurationError(f"{field_name} is required")
    secret = value.get_secret_value()
    if not secret:
        raise SettingsConfigurationError(f"{field_name} is required")
    return secret

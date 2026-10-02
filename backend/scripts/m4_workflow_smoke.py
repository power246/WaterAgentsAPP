from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from enum import Enum
from uuid import uuid4

from app.application.analysis_runs.models import RunStatus
from app.application.hydrology_topology import HydrologyGraphBuilder
from app.application.query_understanding import (
    AnalysisContextResolver,
    DateResolver,
    LocationResolver,
    ParameterResolver,
    ParserFinishReason,
    QueryParserResponse,
)
from app.application.station_availability import StationAvailabilityService
from app.application.transcripts.registry import create_production_registry
from app.domain import (
    AdministrativeLocation,
    GeoPoint,
    GeocodingCandidate,
    HydrologyGraph,
    ModelCapability,
    ModelRole,
    ModelSelection,
    MonitoringLocationMetadata,
    MonitoringStation,
    ParameterKey,
    RiverSegment,
    SamplePeriodOfRecord,
    TimeSeriesKind,
    TimeSeriesMetadata,
    TopologyCoverage,
    TopologyLimits,
    UsgsParameterMetadata,
)
from app.infrastructure.config import (
    AppSettings,
    ModelCatalog,
    load_parameter_capability_registry,
)
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.infrastructure.runtime import compose_m4_workflow
from app.main import create_app
from app.ports import PortUnavailableError
try:
    from scripts.m6_fixture_runtime import build_fixture_m6_runtime
except ModuleNotFoundError:  # Direct `python scripts/m4_workflow_smoke.py` execution.
    from m6_fixture_runtime import build_fixture_m6_runtime

NOW = datetime(2026, 9, 27, 12, tzinfo=UTC)
TARGET_POINT = GeoPoint(latitude=38.9, longitude=-77.0)
DO = ParameterKey(code_system="usgs", code="00300")
DISCHARGE = ParameterKey(code_system="usgs", code="00060")
PARSER_MODEL = ModelSelection(provider="fixture", model="parser-v1")
WATERBODY_MODEL = ModelSelection(provider="fixture", model="waterbody-v1")
MODEL_CATALOG = ModelCatalog(
    (
        ModelCapability(
            **PARSER_MODEL.model_dump(),
            roles=(ModelRole.QUERY_PARSER,),
        ),
        ModelCapability(
            **WATERBODY_MODEL.model_dump(),
            roles=(ModelRole.WATERBODY,),
            context_window_tokens=128_000,
            max_output_tokens=32_768,
        ),
    )
)


class FixtureProfile(str, Enum):
    FULL = "full"
    PARTIAL = "partial"
    NONE = "none"
    FAILURE = "failure"


class FixedClock:
    def now(self) -> datetime:
        return NOW


class FixtureParser:
    def __init__(self, *, query_fallback: bool) -> None:
        self.query_fallback = query_fallback
        self.calls = 0

    async def generate(self, request, on_delta):
        assert request.tools == ()
        self.calls += 1
        payload = {
            "location_mention": "Fixture River" if self.query_fallback else None,
            "coordinate_mention": None,
            "start_date": "2026-09-26" if self.query_fallback else None,
            "end_date": "2026-09-28" if self.query_fallback else None,
            "parameters": [
                {
                    "concept": "dissolved oxygen",
                    "rationale": "Dissolved oxygen is explicitly requested.",
                    "explicitly_named": True,
                }
            ],
            "ambiguities": [],
        }
        text = json.dumps(payload, separators=(",", ":"))
        midpoint = len(text) // 2
        await on_delta(text[:midpoint])
        await on_delta(text[midpoint:])
        return QueryParserResponse(text=text, finish_reason=ParserFinishReason.STOP)


class FixtureGeocoder:
    def __init__(self) -> None:
        self.requests = 0

    @staticmethod
    def _location() -> AdministrativeLocation:
        return AdministrativeLocation(
            country_code="US",
            subdivision_code="US-DC",
            display_name="Fixture River, District of Columbia",
            provider_identity="fixture-geocoder-v1",
        )

    async def resolve_text(self, query: str, *, deadline=None):
        del deadline
        self.requests += 1
        assert query == "Fixture River"
        return (
            GeocodingCandidate(
                point=TARGET_POINT,
                administrative_location=self._location(),
            ),
        )

    async def reverse(self, point: GeoPoint, *, deadline=None):
        del point, deadline
        self.requests += 1
        return self._location()


class FixtureNldi:
    def __init__(self, profile: FixtureProfile) -> None:
        self.profile = profile
        self.requests = 0

    async def lookup_position(self, point: GeoPoint, *, deadline=None):
        del deadline
        self.requests += 1
        assert point == TARGET_POINT
        return "100"

    async def get_segment(self, comid: str, *, deadline=None):
        del deadline
        self.requests += 1
        if comid not in {"100", "200", "300"}:
            return None
        offset = (int(comid) - 100) / 1000
        return RiverSegment(
            comid=comid,
            location=GeoPoint(
                latitude=TARGET_POINT.latitude + offset,
                longitude=TARGET_POINT.longitude,
            ),
        )

    async def get_direct_upstreams(
        self,
        comid: str,
        max_distance_km: float,
        *,
        deadline=None,
    ):
        del max_distance_km, deadline
        self.requests += 1
        upstream = {"100": "200", "200": "300"}.get(comid)
        if upstream is None:
            return ()
        return (
            RiverSegment(
                comid=upstream,
                location=GeoPoint(
                    latitude=TARGET_POINT.latitude + int(upstream) / 10000,
                    longitude=TARGET_POINT.longitude,
                ),
                distance_to_downstream_km=2.5,
                downstream_comids=(comid,),
            ),
        )

    async def get_monitoring_sites(self, comid: str, *, deadline=None):
        del deadline
        self.requests += 1
        station_id = None
        if self.profile in {FixtureProfile.FULL, FixtureProfile.FAILURE}:
            station_id = "USGS-100" if comid == "100" else None
        elif self.profile is FixtureProfile.PARTIAL:
            station_id = f"USGS-{comid}" if comid in {"200", "300"} else None
        if station_id is None:
            return ()
        return (
            MonitoringStation(
                station_id=station_id,
                source="fixture-nldi-v1",
                name=f"Fixture station {comid}",
                location=None,
                associated_comid=comid,
            ),
        )


class FixtureParameterCatalog:
    async def find_candidates(self, concept: str):
        del concept
        return ()


class FixtureUsgsMetadata:
    def __init__(self, profile: FixtureProfile, registry) -> None:
        self.profile = profile
        self.registry = registry
        self.requests = 0

    def _routed_keys(self, station_id: str) -> tuple[set[ParameterKey], set[ParameterKey]]:
        if self.profile is FixtureProfile.FULL and station_id == "USGS-100":
            return {DO}, {DISCHARGE}
        if self.profile is FixtureProfile.PARTIAL and station_id == "USGS-200":
            return {DO}, set()
        return set(), set()

    async def get_parameter_codes(self, parameters, *, deadline=None):
        del deadline
        self.requests += 1
        if self.profile is FixtureProfile.FAILURE:
            raise PortUnavailableError(
                "fixture WDFN unavailable",
                provider="fixture-wdfn-v1",
                operation="get_parameter_codes",
            )
        available = set().union(
            *(self._routed_keys(station_id)[0] for station_id in self._station_ids())
        )
        return tuple(
            UsgsParameterMetadata(
                key=key,
                name=self.registry.get(key).official_name,
                unit=self.registry.get(key).official_unit,
                provider_identity=f"fixture-wdfn-parameter-{key.code}",
            )
            for key in parameters
            if key in available
        )

    async def get_monitoring_locations(self, station_ids, *, deadline=None):
        del deadline
        self.requests += 1
        return tuple(
            MonitoringLocationMetadata(
                station_id=station_id,
                agency_code="USGS",
                name=f"Fixture station {station_id}",
                location=TARGET_POINT,
                provider_identity=f"fixture-wdfn-location-{station_id}",
            )
            for station_id in station_ids
        )

    async def get_time_series_metadata(
        self,
        station_ids,
        parameters,
        *,
        deadline=None,
    ):
        del deadline
        self.requests += 1
        records = []
        for station_id in station_ids:
            wdfn, _ = self._routed_keys(station_id)
            for key in parameters:
                if key not in wdfn:
                    continue
                records.append(
                    TimeSeriesMetadata(
                        series_id=f"fixture-series-{station_id}-{key.code}",
                        station_id=station_id,
                        parameter=key,
                        unit=self.registry.get(key).canonical_unit,
                        begin=datetime(2026, 1, 1, tzinfo=UTC),
                        end=datetime(2026, 12, 31, tzinfo=UTC),
                        kind=TimeSeriesKind.CONTINUOUS,
                        provider_identity=f"fixture-wdfn-series-{station_id}-{key.code}",
                    )
                )
        return tuple(records)

    async def get_sample_periods(self, station_id: str, *, deadline=None):
        del deadline
        self.requests += 1
        _, samples = self._routed_keys(station_id)
        return tuple(
            SamplePeriodOfRecord(
                station_id=station_id,
                characteristic=self.registry.get(key).official_name,
                first_activity=date(2026, 1, 1),
                most_recent_activity=date(2026, 12, 31),
                result_count=10,
                activity_count=5,
                provider_identity=f"fixture-samples-{station_id}-{key.code}",
            )
            for key in sorted(samples, key=lambda item: item.code)
        )

    def _station_ids(self) -> tuple[str, ...]:
        if self.profile is FixtureProfile.FULL:
            return ("USGS-100",)
        if self.profile is FixtureProfile.PARTIAL:
            return ("USGS-200", "USGS-300")
        if self.profile is FixtureProfile.FAILURE:
            return ("USGS-100",)
        return ()


def request_bytes(*, frontend: bool, query_fallback: bool) -> bytes:
    body: dict[str, object] = {
        "query": (
            "Assess dissolved oxygen at the fixture river for the requested dates."
            if query_fallback
            else "Assess dissolved oxygen for the selected target and dates."
        ),
        "parser_model": PARSER_MODEL.model_dump(mode="json"),
        "waterbody_model": WATERBODY_MODEL.model_dump(mode="json"),
        "memory_mode": "stateless",
    }
    if frontend:
        body.update(
            {
                "target": {"coordinates": TARGET_POINT.model_dump(mode="json")},
                "date_range": {
                    "start_date": "2026-09-27",
                    "end_date": "2026-09-27",
                },
                "time_resolution": "auto",
            }
        )
    return json.dumps(body, separators=(",", ":")).encode()


def build_runtime(profile: FixtureProfile, *, query_fallback: bool):
    registry = load_parameter_capability_registry()
    geocoder = FixtureGeocoder()
    nldi = FixtureNldi(profile)
    metadata = FixtureUsgsMetadata(profile, registry)
    parser = FixtureParser(query_fallback=query_fallback)
    context_resolver = AnalysisContextResolver(
        clock=FixedClock(),
        location_resolver=LocationResolver(geocoder, nldi, nldi),
        date_resolver=DateResolver(),
        parameter_resolver=ParameterResolver(
            registry,
            FixtureParameterCatalog(),
        ),
        capability_registry=registry,
    )
    station_inventory = StationAvailabilityService(
        topology=nldi,
        metadata=metadata,
        capability_registry=registry,
        clock=FixedClock(),
    )
    topology_builder = HydrologyGraphBuilder(
        topology=nldi,
        station_inventory=station_inventory,
        limits=TopologyLimits(max_distance_km=100, max_hops=12, max_nodes=128),
        max_concurrency=8,
        stage_timeout_seconds=5,
    )
    store = InMemoryTranscriptStore()
    settings = AppSettings(
        analysis_timezone="America/New_York",
        log_level="warning",
    )
    application = create_app(
        settings,
        clock=FixedClock(),
        workflow=compose_m4_workflow(
            settings,
            parser=parser,
            context_resolver=context_resolver,
            topology_builder=topology_builder,
            parameter_capabilities=registry,
            error_id_factory=uuid4,
        ),
        transcript_store=store,
        model_catalog=MODEL_CATALOG,
        parameter_capability_registry=registry,
        m6_runtime_builder=build_fixture_m6_runtime,
    )
    return application, store, parser, geocoder, nldi, metadata


async def wait_terminal(coordinator, run_id):
    stream = await coordinator.subscribe_run(run_id)
    try:
        async for snapshot in stream:
            if snapshot is None:
                break
            if snapshot.status in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
                RunStatus.COMPLETED_WITH_PERSISTENCE_ERROR,
            } and snapshot.analysis_phase.value == "terminal":
                return snapshot
    finally:
        await stream.aclose()
    raise AssertionError("run stream closed without a terminal snapshot")


def parsed_content(stored, content_type: str):
    registry = create_production_registry()
    bodies = {content.item_id: content.body for content in stored.contents}
    matches = [item for item in stored.items if item.content_type == content_type]
    assert len(matches) == 1
    item = matches[0]
    body = item.inline_body if item.inline_body is not None else bodies[item.item_id]
    parsed = registry.parse(item, body)
    assert parsed.body is not None
    return parsed.body


def failed_error(stored):
    registry = create_production_registry()
    external = {content.item_id: content.body for content in stored.contents}
    for item in reversed(stored.items):
        if item.content_type != "lifecycle_event":
            continue
        body = item.inline_body if item.inline_body is not None else external[item.item_id]
        parsed = registry.parse(item, body)
        if parsed.body is not None and parsed.body.type.value == "run_failed":
            return parsed.body.payload.error
    raise AssertionError("failed run did not persist a run_failed lifecycle event")


async def run_case(
    profile: FixtureProfile,
    *,
    frontend: bool,
    query_fallback: bool,
) -> dict[str, object]:
    application, store, parser, geocoder, nldi, metadata = build_runtime(
        profile,
        query_fallback=query_fallback,
    )
    async with application.router.lifespan_context(application):
        coordinator = application.state.run_coordinator
        payload = request_bytes(frontend=frontend, query_fallback=query_fallback)
        idempotency_key = uuid4()
        created = await coordinator.create_run(
            payload,
            idempotency_key=idempotency_key,
        )
        await wait_terminal(coordinator, created.run_id)
        snapshot = await coordinator.get_snapshot(created.run_id)
        replay = await coordinator.create_run(
            payload,
            idempotency_key=idempotency_key,
        )
        assert replay.run_id == created.run_id
        assert parser.calls == 1
        stored = await store.read_run(created.run_id)
        assert stored is not None
        context = parsed_content(stored, "query_understood")

        result: dict[str, object] = {
            "status": snapshot.status.value,
            "target_source": context.target.source.value,
            "mode": context.mode.value,
            "parser_requests": parser.calls,
            "geocoder_requests": geocoder.requests,
            "nldi_requests": nldi.requests,
            "metadata_requests": metadata.requests,
            "persistence_reload": "passed",
        }
        if snapshot.status is RunStatus.COMPLETED:
            graph = parsed_content(stored, "topology_ready")
            assert isinstance(graph, HydrologyGraph)
            result.update(
                {
                    "coverage": graph.coverage.value,
                    "inlets": [node.comid for node in graph.nodes if node.is_inlet],
                    "providers": sorted(
                        {
                            reference.provider.value
                            for node in graph.nodes
                            for reference in node.station_refs
                        }
                    ),
                }
            )
        else:
            error = failed_error(stored)
            result["error_code"] = error.code
        return result


async def run_smoke() -> dict[str, object]:
    async with asyncio.timeout(20):
        frontend = await run_case(
            FixtureProfile.FULL,
            frontend=True,
            query_fallback=False,
        )
        assert frontend["status"] == "completed"
        assert frontend["target_source"] == "frontend"
        assert frontend["coverage"] == TopologyCoverage.COMPLETE.value
        assert frontend["providers"] == ["usgs_samples", "wdfn_time_series"]

        mixed = await run_case(
            FixtureProfile.PARTIAL,
            frontend=False,
            query_fallback=True,
        )
        assert mixed["status"] == "completed"
        assert mixed["target_source"] == "query"
        assert mixed["mode"] == "mixed"
        assert mixed["coverage"] == TopologyCoverage.PARTIAL.value
        assert mixed["inlets"] == ["200"]

        no_source = await run_case(
            FixtureProfile.NONE,
            frontend=True,
            query_fallback=False,
        )
        assert no_source["status"] == "completed"
        assert no_source["coverage"] == TopologyCoverage.NONE.value
        assert no_source["inlets"] == []

        technical_failure = await run_case(
            FixtureProfile.FAILURE,
            frontend=True,
            query_fallback=False,
        )
        assert technical_failure["status"] == "failed"
        assert technical_failure["error_code"] == "station_availability_failed"

    return {
        "fixture_versions": {
            "model": "fixture-parser-v1",
            "geocoding": "fixture-geocoder-v1",
            "nldi": "fixture-nldi-v1",
            "wdfn": "fixture-wdfn-v1",
            "samples": "fixture-samples-v1",
        },
        "frontend_target_date": frontend,
        "query_fallback_mixed_partial": mixed,
        "no_source": no_source,
        "technical_failure": technical_failure,
    }


def main() -> None:
    print(json.dumps(asyncio.run(run_smoke()), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()

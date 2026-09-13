import ast
import asyncio
import inspect
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import get_origin

import pytest

from app.domain import GeoPoint, MonitoringStation, ParameterKey, RiverSegment
from app.ports import (
    CachePort,
    ClockPort,
    ComidLookupPort,
    EventPublisherPort,
    FacilitySearchPort,
    GeocodingPort,
    InvalidPortResponseError,
    KeyStorePort,
    LLMPort,
    MonitoringStationPort,
    PortError,
    PortTimeoutError,
    PortUnavailableError,
    RAGIndexPort,
    TopologyDataPort,
    TranscriptStorePort,
    WaterObservationPort,
    WeatherForecastPort,
    WeatherObservationPort,
)

PORTS_ROOT = Path(__file__).resolve().parents[2] / "app" / "ports"


def test_all_port_contracts_are_public_protocols() -> None:
    protocols = (
        CachePort,
        ClockPort,
        ComidLookupPort,
        EventPublisherPort,
        FacilitySearchPort,
        GeocodingPort,
        KeyStorePort,
        LLMPort,
        MonitoringStationPort,
        RAGIndexPort,
        TopologyDataPort,
        TranscriptStorePort,
        WaterObservationPort,
        WeatherForecastPort,
        WeatherObservationPort,
    )

    assert all(getattr(protocol, "_is_protocol", False) for protocol in protocols)


def test_port_method_sync_and_async_semantics() -> None:
    assert not inspect.iscoroutinefunction(ClockPort.now)
    async_methods = (
        GeocodingPort.resolve,
        ComidLookupPort.lookup_comid,
        TopologyDataPort.get_segment,
        TopologyDataPort.get_direct_upstreams,
        MonitoringStationPort.find_for_segment,
        WaterObservationPort.fetch,
        WeatherObservationPort.fetch,
        WeatherForecastPort.fetch,
        FacilitySearchPort.search,
        LLMPort.generate,
        TranscriptStorePort.append,
        TranscriptStorePort.list_for_run,
        RAGIndexPort.upsert,
        RAGIndexPort.search,
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


@pytest.mark.parametrize(
    ("method", "parameters"),
    [
        (ClockPort.now, ("self",)),
        (GeocodingPort.resolve, ("self", "location_text")),
        (ComidLookupPort.lookup_comid, ("self", "point")),
        (TopologyDataPort.get_segment, ("self", "comid")),
        (
            TopologyDataPort.get_direct_upstreams,
            ("self", "comid", "max_distance_km"),
        ),
        (
            MonitoringStationPort.find_for_segment,
            ("self", "comid", "parameters"),
        ),
        (TranscriptStorePort.append, ("self", "run_id", "item")),
        (TranscriptStorePort.list_for_run, ("self", "run_id")),
        (RAGIndexPort.upsert, ("self", "item")),
        (RAGIndexPort.search, ("self", "query")),
        (CachePort.get, ("self", "namespace", "key")),
        (CachePort.set, ("self", "namespace", "key", "value")),
        (CachePort.delete, ("self", "namespace", "key")),
        (CachePort.clear_namespace, ("self", "namespace")),
    ],
)
def test_port_method_signatures(method, parameters: tuple[str, ...]) -> None:
    assert tuple(inspect.signature(method).parameters) == parameters


def test_stable_ports_use_domain_types() -> None:
    geocoding = inspect.signature(GeocodingPort.resolve)
    comid_lookup = inspect.signature(ComidLookupPort.lookup_comid)
    topology = inspect.signature(TopologyDataPort.get_segment)
    upstreams = inspect.signature(TopologyDataPort.get_direct_upstreams)
    stations = inspect.signature(MonitoringStationPort.find_for_segment)

    assert geocoding.parameters["location_text"].annotation is str
    assert geocoding.return_annotation == GeoPoint | None
    assert comid_lookup.parameters["point"].annotation is GeoPoint
    assert comid_lookup.return_annotation == str | None
    assert topology.return_annotation == RiverSegment | None
    assert upstreams.return_annotation == tuple[RiverSegment, ...]
    assert stations.parameters["parameters"].annotation == tuple[ParameterKey, ...]
    assert stations.return_annotation == tuple[MonitoringStation, ...]


def test_deferred_ports_are_generic_without_guessing_dtos() -> None:
    generic_ports = (
        WaterObservationPort[int, str],
        WeatherObservationPort[int, str],
        WeatherForecastPort[int, str],
        FacilitySearchPort[int, str],
        LLMPort[int, str],
        TranscriptStorePort[str],
        RAGIndexPort[str, int, float],
        EventPublisherPort[str],
        CachePort[str],
        KeyStorePort[str],
    )

    assert all(get_origin(port) is not None for port in generic_ports)


@pytest.mark.parametrize(
    "error_type",
    [PortUnavailableError, PortTimeoutError, InvalidPortResponseError],
)
def test_port_errors_share_a_stable_base(error_type: type[PortError]) -> None:
    error = error_type("provider failed")

    assert isinstance(error, PortError)
    assert str(error) == "provider failed"


def test_successful_empty_results_are_distinct_from_unavailability() -> None:
    class EmptyWaterProvider:
        async def fetch(self, request: int) -> tuple[str, ...]:
            return ()

    class UnavailableWaterProvider:
        async def fetch(self, request: int) -> tuple[str, ...]:
            raise PortUnavailableError("water provider unavailable")

    async def exercise() -> None:
        empty: WaterObservationPort[int, str] = EmptyWaterProvider()
        unavailable: WaterObservationPort[int, str] = UnavailableWaterProvider()

        assert await empty.fetch(1) == ()
        with pytest.raises(PortUnavailableError):
            await unavailable.fetch(1)

    asyncio.run(exercise())


def test_not_found_contracts_use_none_or_empty_tuple() -> None:
    class EmptyGeocoder:
        async def resolve(self, location_text: str) -> GeoPoint | None:
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

    async def exercise() -> None:
        geocoder: GeocodingPort = EmptyGeocoder()
        topology: TopologyDataPort = EmptyTopology()

        assert await geocoder.resolve("unknown") is None
        assert await topology.get_segment("0001") is None
        assert await topology.get_direct_upstreams("0001", 10) == ()

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
        MonitoringStationPort,
        WaterObservationPort,
        WeatherObservationPort,
        WeatherForecastPort,
        FacilitySearchPort,
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
                    if alias.name.split(".", maxsplit=1)[0] not in sys.stdlib_module_names:
                        violations.append(f"{source_path.name}: imports {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".", maxsplit=1)[0]
                if (
                    root not in sys.stdlib_module_names
                    and not node.module.startswith("app.domain")
                    and not node.module.startswith("app.ports")
                ):
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

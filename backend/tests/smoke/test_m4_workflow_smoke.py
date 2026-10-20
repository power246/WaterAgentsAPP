import asyncio
import json
from pathlib import Path

from app.application.hydrology_topology import (
    build_facility_corridor_plan,
    describe_facility_corridor,
)
from app.domain import GeoPoint, HydrologyGraph
from scripts.m4_workflow_smoke import run_smoke

FIXTURES = Path(__file__).with_name("fixtures")


def omit_new_optional_fields(value):
    if isinstance(value, dict):
        return {
            key: omit_new_optional_fields(item)
            for key, item in value.items()
            if not (key in {"window_index", "covered_window_ids"} and item is None)
        }
    if isinstance(value, list):
        return [omit_new_optional_fields(item) for item in value]
    return value


def test_fixed_m4_workflow_smoke() -> None:
    result = asyncio.run(run_smoke())

    assert result["frontend_target_date"]["persistence_reload"] == "passed"
    assert result["query_fallback_mixed_partial"]["mode"] == "mixed"
    assert result["no_source"]["coverage"] == "none"
    assert result["technical_failure"]["error_code"] == (
        "station_availability_failed"
    )


def test_topology_ready_geometry_round_trip_and_corridor_planning() -> None:
    fixture_text = (FIXTURES / "topology_ready_v1_geometry.json").read_text(
        encoding="utf-8"
    )
    restored = HydrologyGraph.model_validate_json(fixture_text)
    exported = restored.model_dump_json()
    reloaded = HydrologyGraph.model_validate_json(exported)

    descriptor = describe_facility_corridor(reloaded)
    plan = build_facility_corridor_plan(
        reloaded,
        agent_radius_miles=20,
    )

    assert reloaded == restored
    assert omit_new_optional_fields(json.loads(exported)) == json.loads(fixture_text)
    assert exported.count('"full_coordinates"') == 1
    assert restored.nodes[0].geometry_ref == descriptor.geometry_ref
    assert plan.geometry_ref == descriptor.geometry_ref
    assert plan.centers[0] == GeoPoint(latitude=38.8, longitude=-77.2)
    assert plan.centers[-1] == GeoPoint(latitude=39.0, longitude=-77.0)

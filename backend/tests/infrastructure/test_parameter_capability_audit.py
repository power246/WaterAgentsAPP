import asyncio
import json

import httpx

from app.infrastructure.adapters import UsgsWaterDataAdapter
from app.infrastructure.config import load_parameter_capability_registry
from app.infrastructure.http import ProviderHttpClient
from scripts.audit_parameter_capabilities import (
    audit_parameter_capabilities,
    audit_parameter_capabilities_bytes,
    compare_official_parameter_catalog,
)


def official_payload(*, drift_code: str | None = None) -> dict[str, object]:
    registry = load_parameter_capability_registry()
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": capability.key.code,
                "properties": {
                    "id": capability.key.code,
                    "parameter_name": (
                        "Changed official name"
                        if capability.key.code == drift_code
                        else capability.official_name
                    ),
                    "unit_of_measure": capability.official_unit,
                },
                "geometry": None,
            }
            for capability in registry.capabilities
        ],
        "links": [],
    }


def compare(payload: dict[str, object]) -> tuple[dict[str, object], int]:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json=payload, request=request)

    async def exercise() -> dict[str, object]:
        async with ProviderHttpClient(
            timeout_seconds=1,
            max_response_bytes=2_000_000,
            retry_delay_seconds=0,
            transport=httpx.MockTransport(handler),
        ) as client:
            return await compare_official_parameter_catalog(
                UsgsWaterDataAdapter(
                    client,
                    ogc_base_url="https://provider.test/ogcapi/v1",
                )
            )

    return asyncio.run(exercise()), requests


def test_offline_audit_and_official_refresh_match_all_154_capabilities() -> None:
    offline = audit_parameter_capabilities()
    official, requests = compare(official_payload())

    assert offline["capability_count"] == 154
    assert offline["unique_key_count"] == 154
    assert offline["required_support_count"] == 1
    assert offline["capability_coverage"] == "154/154"
    assert offline["policy_complete_count"] == 154
    assert offline["policy_reference_status"] == "valid"
    assert offline["unit_definition_count"] == 45
    assert offline["quality_profile_count"] == 2
    assert offline["aggregation_profile_count"] == 10
    assert offline["coverage_profile_count"] == 5
    assert len(offline["manifest_digest_sha256"]) == 64
    assert official == {
        "api_version": "USGS Water Data OGC API v1",
        "status": "no_drift",
        "requested_count": 154,
        "received_count": 154,
        "missing_keys": [],
        "unexpected_keys": [],
        "name_drift": [],
        "unit_drift": [],
        "manifest_updated": False,
    }
    assert requests == 1


def test_official_refresh_reports_drift_without_updating_manifest() -> None:
    before = audit_parameter_capabilities()
    official, _ = compare(official_payload(drift_code="00300"))
    after = audit_parameter_capabilities()

    assert official["status"] == "drift"
    assert official["name_drift"] == [
        {
            "key": "usgs:00300",
            "manifest": "Dissolved oxygen",
            "official": "Changed official name",
        }
    ]
    assert official["manifest_updated"] is False
    assert before == after


def test_audit_report_bytes_are_stable_across_repeated_runs_and_cwd(
    monkeypatch,
    tmp_path,
) -> None:
    first = audit_parameter_capabilities_bytes()
    monkeypatch.chdir(tmp_path)
    load_parameter_capability_registry.cache_clear()
    second = audit_parameter_capabilities_bytes()

    assert first == second
    assert first.endswith(b"\n")
    assert b"\\\\" not in first
    assert json.loads(first)["capability_coverage"] == "154/154"

import ast
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.application.evidence_policy import (
    IndirectEvidenceContext,
    resolve_applicability,
)
from app.domain import (
    EvidenceApplicability,
    GeoPoint,
    InferenceMode,
    ParameterKey,
    TimeRange,
    WeatherEvidenceRequest,
)
from app.domain.hydrology import EvidenceRuleId
from app.infrastructure.adapters import NwsWeatherAdapter
from app.infrastructure.config import load_parameter_capability_registry
from app.infrastructure.http import ProviderHttpClient

FIXTURES = Path(__file__).parents[2] / "infrastructure" / "fixtures"
APPLICATION_ROOT = Path(__file__).parents[3] / "app" / "application"
NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
WINDOW = TimeRange(
    start=datetime(2026, 9, 30, 9, tzinfo=timezone.utc),
    end=datetime(2026, 9, 30, 17, tzinfo=timezone.utc),
)


def fixture(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_weather_records_do_not_declare_relation_or_applicability() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = (
            fixture("nws_point.json")
            if request.url.path.startswith("/points/")
            else fixture("nws_grid_forecast.json")
        )
        return httpx.Response(200, json=payload, request=request)

    async def exercise() -> None:
        http = ProviderHttpClient(
            timeout_seconds=1,
            max_response_bytes=200_000,
            retry_delay_seconds=0,
            transport=httpx.MockTransport(handler),
        )
        async with http:
            batch = await NwsWeatherAdapter(
                http,
                user_agent_project="WaterAgents",
                user_agent_contact="maintainer@example.test",
                clock=lambda: NOW,
            ).collect(
                WeatherEvidenceRequest(
                    operation_id="weather-policy-boundary",
                    evidence_kind="forecast",
                    location=GeoPoint(latitude=38.9, longitude=-77.0),
                    variables=("temperature",),
                    request_range=WINDOW,
                    affected_window_ids=("w000",),
                )
            )
        assert batch.records
        assert all(not hasattr(record, "relation_id") for record in batch.records)
        assert all(not hasattr(record, "applicability") for record in batch.records)

    asyncio.run(exercise())


def test_prediction_mode_has_no_implicit_nws_application_wiring() -> None:
    assert InferenceMode.PREDICTION.value == "prediction"
    offenders: list[str] = []
    for path in APPLICATION_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, ast.Name) and node.id == "NwsWeatherAdapter"
            for node in ast.walk(tree)
        ):
            offenders.append(str(path.relative_to(APPLICATION_ROOT)))
    assert offenders == []


def test_unregistered_weather_relation_remains_contextual() -> None:
    registry = load_parameter_capability_registry()
    target = registry.get(ParameterKey(code_system="usgs", code="00300"))
    assert target is not None
    context = IndirectEvidenceContext(
        relation_id=EvidenceRuleId.TURBIDITY_PRECIPITATION_DISCHARGE_DIRECTIONAL,
        input_parameters=(
            ParameterKey(code_system="usgs", code="00045"),
            ParameterKey(code_system="usgs", code="00060"),
        ),
        input_windows=(WINDOW, WINDOW),
        reference_complete=True,
    )

    assert (
        resolve_applicability(
            registry,
            target,
            "12345678",
            WINDOW,
            indirect_context=context,
        )
        is EvidenceApplicability.CONTEXTUAL_ONLY
    )

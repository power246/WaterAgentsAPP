"""Opt-in live contract smoke for one M5 evidence provider.

Set WATERAGENTS_M5_PROVIDER_SMOKE=1 and the provider-specific M5_SMOKE
variables before invoking this script. Output is a secret-free JSON summary.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from app.application.evidence_tools import map_evidence_error
from app.domain import (
    FacilityEvidenceRequest,
    GeoPoint,
    ParameterKey,
    ParameterProvider,
    TimeRange,
    WaterObservationRequest,
    WaterbodyGeometry,
    WeatherEvidenceRequest,
)
from app.infrastructure.adapters import (
    EpaEchoFacilityAdapter,
    NwsWeatherAdapter,
    UsgsObservationAdapter,
)
from app.infrastructure.adapters.epa_echo_facilities import DEFAULT_BASE_URL as EPA_URL
from app.infrastructure.adapters.nws_weather import DEFAULT_BASE_URL as NWS_URL
from app.infrastructure.adapters.usgs_observations import (
    DEFAULT_OGC_BASE_URL as USGS_URL,
)
from app.infrastructure.config import AppSettings
from app.infrastructure.http import ProviderHttpClient


OPT_IN = "WATERAGENTS_M5_PROVIDER_SMOKE"
CONTRACTS = {
    "usgs": "usgs_waterdata_ogc_v1",
    "nws": "nws_api_linked_data_v1",
    "epa": "epa_echo_cwa_v1",
}
ORIGINS = {"usgs": USGS_URL, "nws": NWS_URL, "epa": EPA_URL}


def _environment(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _not_run(provider: str, reason: str) -> dict[str, object]:
    return {
        "provider": provider,
        "endpoint_contract": CONTRACTS[provider],
        "status": "not_run",
        "reason": reason,
        "requests": 0,
        "pages": 0,
        "records": 0,
    }


def _time_range() -> TimeRange:
    start = _required("WATERAGENTS_M5_SMOKE_START")
    end = _required("WATERAGENTS_M5_SMOKE_END")
    return TimeRange(start=_instant(start), end=_instant(end))


def _instant(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("smoke instants must include a UTC offset")
    return parsed


def _required(name: str) -> str:
    value = _environment(name)
    if value is None:
        raise ValueError(f"missing {name}")
    return value


async def _network_preflight(provider: str) -> None:
    host = urlsplit(ORIGINS[provider]).hostname
    if host is None:
        raise ValueError("provider contract origin is invalid")
    await asyncio.get_running_loop().getaddrinfo(host, 443)


async def _run_provider(provider: str) -> dict[str, object]:
    settings = AppSettings()
    await _network_preflight(provider)
    async with ProviderHttpClient(
        timeout_seconds=settings.evidence_provider_timeout_seconds,
        max_response_bytes=settings.evidence_max_response_bytes,
        retry_delay_seconds=settings.provider_http_retry_delay_seconds,
        attempts=settings.evidence_provider_attempts,
    ) as http:
        if provider == "usgs":
            request = WaterObservationRequest(
                operation_id="m5-provider-smoke-usgs",
                provider=ParameterProvider.WDFN_TIME_SERIES,
                channel=_environment("WATERAGENTS_M5_SMOKE_USGS_CHANNEL")
                or "continuous",
                station_id=_required("WATERAGENTS_M5_SMOKE_USGS_STATION_ID"),
                series_refs=(
                    _required("WATERAGENTS_M5_SMOKE_USGS_SERIES_REF"),
                ),
                parameter_keys=(
                    ParameterKey(
                        code_system="usgs",
                        code=_required("WATERAGENTS_M5_SMOKE_USGS_PARAMETER_CODE"),
                    ),
                ),
                request_range=_time_range(),
                affected_window_ids=("provider-smoke-window",),
            )
            api_key = (
                settings.usgs_api_key.get_secret_value()
                if settings.usgs_api_key is not None
                else None
            )
            adapter = UsgsObservationAdapter(
                http,
                api_key=api_key,
                page_size=min(
                    settings.provider_http_page_size,
                    settings.evidence_max_records,
                ),
                max_pages=settings.evidence_max_pages,
                max_records=settings.evidence_max_records,
            )
        elif provider == "nws":
            contact = settings.nws_user_agent_contact
            if contact.endswith(".invalid"):
                raise ValueError(
                    "WATERAGENTS_NWS_USER_AGENT_CONTACT must identify a real contact"
                )
            request = WeatherEvidenceRequest(
                operation_id="m5-provider-smoke-nws",
                evidence_kind=_environment("WATERAGENTS_M5_SMOKE_NWS_KIND")
                or "observation",
                location=GeoPoint(
                    latitude=float(_required("WATERAGENTS_M5_SMOKE_LATITUDE")),
                    longitude=float(_required("WATERAGENTS_M5_SMOKE_LONGITUDE")),
                ),
                variables=("temperature",),
                request_range=_time_range(),
                affected_window_ids=("provider-smoke-window",),
            )
            adapter = NwsWeatherAdapter(
                http,
                user_agent_project=settings.nws_user_agent_project,
                user_agent_contact=contact,
                page_size=min(settings.provider_http_page_size, 500),
                max_pages=settings.evidence_max_pages,
                max_records=settings.evidence_max_records,
            )
        else:
            geometry = _epa_geometry()
            radius = _positive_decimal(
                _required("WATERAGENTS_M5_SMOKE_EPA_RADIUS_MILES")
            )
            request = FacilityEvidenceRequest(
                operation_id="m5-provider-smoke-epa",
                geometry_ref=geometry.geometry_ref,
                exact_radius_miles=float(radius),
                provider_radius_miles=float(radius),
                sample_centers=(geometry.representative_point,),
                affected_window_ids=("provider-smoke-window",),
            )
            adapter = EpaEchoFacilityAdapter(
                http,
                geometry_resolver=(
                    lambda geometry_ref: (
                        geometry if geometry_ref == geometry.geometry_ref else None
                    )
                ),
                response_set=min(settings.provider_http_page_size, 1000),
                max_pages_per_center=settings.evidence_max_pages,
                max_records=settings.evidence_max_records,
            )
        batch = await adapter.collect(request)

    counts = batch.counts
    return {
        "provider": provider,
        "endpoint_contract": CONTRACTS[provider],
        "status": "passed" if counts.requests > 0 else "not_run",
        "reason": None if counts.requests > 0 else "provider issued zero requests",
        "requests": counts.requests,
        "pages": counts.pages,
        "records": counts.records,
        "complete": batch.completion_proof.complete,
    }


def _epa_geometry() -> WaterbodyGeometry:
    raw = json.loads(_required("WATERAGENTS_M5_SMOKE_EPA_LINESTRING"))
    if not isinstance(raw, list):
        raise ValueError("EPA linestring must be a JSON coordinate list")
    coordinates = tuple((float(item[0]), float(item[1])) for item in raw)
    return WaterbodyGeometry.from_coordinates(
        source_comid="m5-provider-smoke",
        coordinates=coordinates,
    )


def _positive_decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("EPA radius must be numeric") from error
    if not parsed.is_finite() or parsed <= 0 or parsed > 20:
        raise ValueError("EPA radius must be greater than 0 and at most 20 miles")
    return parsed


async def _main(provider: str) -> dict[str, object]:
    if _environment(OPT_IN) != "1":
        return _not_run(provider, f"set {OPT_IN}=1 to opt in")
    try:
        return await _run_provider(provider)
    except (ValueError, OSError) as error:
        return _not_run(provider, str(error))
    except Exception as error:
        safe = map_evidence_error(error)
        return {
            "provider": provider,
            "endpoint_contract": CONTRACTS[provider],
            "status": "failed",
            "reason": safe.code,
            "requests": None,
            "pages": None,
            "records": None,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("provider", choices=tuple(CONTRACTS))
    arguments = parser.parse_args()
    result = asyncio.run(_main(arguments.provider))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 1 if result["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())

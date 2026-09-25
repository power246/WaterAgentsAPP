from __future__ import annotations

from collections.abc import Mapping

from app.domain.hydrology import (
    AdministrativeLocation,
    GeocodingCandidate,
    GeoPoint,
)
from app.infrastructure.adapters._parsing import (
    invalid_response,
    require_list,
    require_mapping,
    require_number,
    require_string,
)
from app.infrastructure.http import ProviderHttpClient
from app.ports import (
    PortAuthenticationError,
    PortConfigurationError,
    PortRateLimitError,
    PortUnavailableError,
)

PROVIDER = "google_geocoding"
DEFAULT_ENDPOINT = "https://maps.googleapis.com/maps/api/geocode/json"


class GoogleGeocodingAdapter:
    def __init__(
        self,
        http: ProviderHttpClient,
        *,
        api_key: str,
        endpoint: str = DEFAULT_ENDPOINT,
        max_candidates: int = 10,
    ) -> None:
        if not api_key:
            raise PortConfigurationError(
                "Google geocoding API key is not configured.",
                provider=PROVIDER,
                operation="configure",
            )
        if max_candidates <= 0:
            raise ValueError("max_candidates must be positive")
        self._http = http
        self._api_key = api_key
        self._endpoint = endpoint
        self._max_candidates = max_candidates

    async def resolve_text(
        self,
        location_text: str,
        *,
        deadline: float | None = None,
    ) -> tuple[GeocodingCandidate, ...]:
        if not location_text.strip():
            raise ValueError("location_text must not be empty")
        operation = "resolve_text"
        payload = await self._request(
            operation,
            {"address": location_text, "components": "country:US"},
            deadline,
        )
        results = self._results(payload, operation)
        return tuple(
            self._candidate(item, operation)
            for item in results[: self._max_candidates]
        )

    async def reverse(
        self,
        point: GeoPoint,
        *,
        deadline: float | None = None,
    ) -> AdministrativeLocation | None:
        operation = "reverse"
        payload = await self._request(
            operation,
            {"latlng": f"{point.latitude},{point.longitude}"},
            deadline,
        )
        results = self._results(payload, operation)
        if not results:
            return None
        return self._administrative_location(
            require_mapping(results[0], provider=PROVIDER, operation=operation),
            operation,
        )

    async def _request(
        self,
        operation: str,
        query: dict[str, str],
        deadline: float | None,
    ) -> Mapping[str, object]:
        payload = await self._http.get_json(
            self._endpoint,
            provider=PROVIDER,
            operation=operation,
            params={**query, "key": self._api_key},
            deadline=deadline,
        )
        return require_mapping(payload, provider=PROVIDER, operation=operation)

    def _results(
        self,
        payload: Mapping[str, object],
        operation: str,
    ) -> list[object]:
        status = require_string(
            payload.get("status"),
            provider=PROVIDER,
            operation=operation,
        )
        if status == "ZERO_RESULTS":
            return []
        if status == "OVER_QUERY_LIMIT":
            raise PortRateLimitError(
                "Google geocoding rate limit was exceeded.",
                provider=PROVIDER,
                operation=operation,
            )
        if status in {"OVER_DAILY_LIMIT", "REQUEST_DENIED"}:
            raise PortAuthenticationError(
                "Google geocoding authentication failed.",
                provider=PROVIDER,
                operation=operation,
            )
        if status == "INVALID_REQUEST":
            raise PortConfigurationError(
                "Google geocoding rejected the request configuration.",
                provider=PROVIDER,
                operation=operation,
            )
        if status == "UNKNOWN_ERROR":
            raise PortUnavailableError(
                "Google geocoding is temporarily unavailable.",
                provider=PROVIDER,
                operation=operation,
            )
        if status != "OK":
            raise invalid_response(PROVIDER, operation)
        return require_list(
            payload.get("results"),
            provider=PROVIDER,
            operation=operation,
        )

    def _candidate(
        self,
        value: object,
        operation: str,
    ) -> GeocodingCandidate:
        result = require_mapping(value, provider=PROVIDER, operation=operation)
        geometry = require_mapping(
            result.get("geometry"),
            provider=PROVIDER,
            operation=operation,
        )
        location = require_mapping(
            geometry.get("location"),
            provider=PROVIDER,
            operation=operation,
        )
        return GeocodingCandidate(
            point=GeoPoint(
                latitude=require_number(
                    location.get("lat"),
                    provider=PROVIDER,
                    operation=operation,
                ),
                longitude=require_number(
                    location.get("lng"),
                    provider=PROVIDER,
                    operation=operation,
                ),
            ),
            administrative_location=self._administrative_location(
                result,
                operation,
            ),
        )

    def _administrative_location(
        self,
        result: Mapping[str, object],
        operation: str,
    ) -> AdministrativeLocation:
        components = require_list(
            result.get("address_components"),
            provider=PROVIDER,
            operation=operation,
        )
        values: dict[str, str] = {}
        for raw_component in components:
            component = require_mapping(
                raw_component,
                provider=PROVIDER,
                operation=operation,
            )
            types = require_list(
                component.get("types"),
                provider=PROVIDER,
                operation=operation,
            )
            short_name = require_string(
                component.get("short_name"),
                provider=PROVIDER,
                operation=operation,
            )
            for raw_type in types:
                component_type = require_string(
                    raw_type,
                    provider=PROVIDER,
                    operation=operation,
                )
                if component_type in {"country", "administrative_area_level_1"}:
                    values[component_type] = short_name
        if "country" not in values or "administrative_area_level_1" not in values:
            raise invalid_response(PROVIDER, operation)
        place_id = require_string(
            result.get("place_id"),
            provider=PROVIDER,
            operation=operation,
        )
        display_name = require_string(
            result.get("formatted_address"),
            provider=PROVIDER,
            operation=operation,
        )
        return AdministrativeLocation(
            country_code=values["country"],
            subdivision_code=(
                f"{values['country']}-{values['administrative_area_level_1']}"
            ),
            display_name=display_name,
            provider_identity=f"google-geocoding:v3:{place_id}",
        )

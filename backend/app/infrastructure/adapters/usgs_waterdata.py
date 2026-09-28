from __future__ import annotations

import csv
from collections.abc import Mapping
from datetime import date, datetime
from io import StringIO
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from app.domain.hydrology import (
    GeoPoint,
    MonitoringLocationMetadata,
    ParameterCatalogCandidate,
    ParameterKey,
    SamplePeriodOfRecord,
    TimeSeriesKind,
    TimeSeriesMetadata,
    UsgsParameterMetadata,
)
from app.infrastructure.adapters._parsing import (
    invalid_response,
    require_list,
    require_mapping,
    require_number,
    require_string,
)
from app.infrastructure.http import ProviderHttpClient
from app.ports import PortResponseLimitError, UnsupportedPortVersionError

PROVIDER = "usgs_waterdata"
SAMPLES_PROVIDER = "usgs_samples"
DEFAULT_OGC_BASE_URL = "https://api.waterdata.usgs.gov/ogcapi/v1"
DEFAULT_SAMPLES_BASE_URL = "https://api.waterdata.usgs.gov/samples-data"
_SAMPLE_COLUMNS = {
    "monitoringLocationIdentifier",
    "characteristicGroup",
    "characteristic",
    "characteristicUserSupplied",
    "resultCount",
    "activityCount",
    "firstActivity",
    "mostRecentActivity",
}


class UsgsWaterDataAdapter:
    def __init__(
        self,
        http: ProviderHttpClient,
        *,
        api_key: str | None = None,
        api_version: str = "v1",
        ogc_base_url: str = DEFAULT_OGC_BASE_URL,
        samples_base_url: str = DEFAULT_SAMPLES_BASE_URL,
        page_size: int = 1000,
        max_pages: int = 10,
        max_records: int = 10000,
    ) -> None:
        if api_version != "v1":
            raise UnsupportedPortVersionError(
                "USGS Water Data API version is unsupported.",
                provider=PROVIDER,
                operation="configure",
            )
        if page_size <= 0 or max_pages <= 0 or max_records <= 0:
            raise ValueError("USGS pagination limits must be positive")
        if page_size > max_records:
            raise ValueError("page_size must not exceed max_records")
        self._http = http
        self._api_key = api_key
        self._ogc_base_url = ogc_base_url.rstrip("/")
        self._samples_base_url = samples_base_url.rstrip("/")
        self._page_size = page_size
        self._max_pages = max_pages
        self._max_records = max_records

    async def find_candidates(
        self,
        concept: str,
    ) -> tuple[ParameterCatalogCandidate, ...]:
        value = concept.strip()
        if not value:
            raise ValueError("parameter concept must not be empty")
        operation = "find_parameter_candidates"
        query = (
            {"id": value}
            if len(value) == 5 and value.isdecimal()
            else {"parameter_name": value}
        )
        features = await self._ogc_features(
            "parameter-codes",
            operation=operation,
            query=query,
            deadline=None,
        )
        records = tuple(
            self._parameter_metadata(feature, operation)
            for feature in features
        )
        return tuple(
            ParameterCatalogCandidate(
                key=record.key,
                canonical_name=record.name,
                score=1.0,
            )
            for record in sorted(records, key=lambda item: item.key.code)
        )

    async def get_parameter_codes(
        self,
        parameters: tuple[ParameterKey, ...],
        *,
        deadline: float | None = None,
    ) -> tuple[UsgsParameterMetadata, ...]:
        codes = self._parameter_codes(parameters)
        if not codes:
            return ()
        operation = "get_parameter_codes"
        features = await self._ogc_features(
            "parameter-codes",
            operation=operation,
            query={"id": ",".join(codes)},
            deadline=deadline,
        )
        records = tuple(
            self._parameter_metadata(feature, operation)
            for feature in features
        )
        return tuple(sorted(records, key=lambda record: record.key.code))

    async def get_monitoring_locations(
        self,
        station_ids: tuple[str, ...],
        *,
        deadline: float | None = None,
    ) -> tuple[MonitoringLocationMetadata, ...]:
        ids = self._identifiers(station_ids, field_name="station_ids")
        if not ids:
            return ()
        operation = "get_monitoring_locations"
        features = await self._ogc_features(
            "monitoring-locations",
            operation=operation,
            query={"id": ",".join(ids)},
            deadline=deadline,
        )
        records = tuple(
            self._monitoring_location(feature, operation)
            for feature in features
        )
        return tuple(sorted(records, key=lambda record: record.station_id))

    async def get_time_series_metadata(
        self,
        station_ids: tuple[str, ...],
        parameters: tuple[ParameterKey, ...],
        *,
        deadline: float | None = None,
    ) -> tuple[TimeSeriesMetadata, ...]:
        ids = self._identifiers(station_ids, field_name="station_ids")
        codes = self._parameter_codes(parameters)
        if not ids or not codes:
            return ()
        operation = "get_time_series_metadata"
        features = await self._ogc_features(
            "time-series-metadata",
            operation=operation,
            query={
                "monitoring_location_id": ",".join(ids),
                "parameter_code": ",".join(codes),
            },
            deadline=deadline,
        )
        records = tuple(
            record
            for feature in features
            if (record := self._time_series(feature, operation)) is not None
        )
        return tuple(
            sorted(
                records,
                key=lambda record: (
                    record.station_id,
                    record.parameter.code,
                    record.kind.value,
                    record.series_id,
                ),
            )
        )

    async def get_sample_periods(
        self,
        station_id: str,
        *,
        deadline: float | None = None,
    ) -> tuple[SamplePeriodOfRecord, ...]:
        station = self._identifiers((station_id,), field_name="station_id")[0]
        operation = "get_sample_periods"
        params: dict[str, str] = {"mimeType": "text/csv"}
        if self._api_key:
            params["api_key"] = self._api_key
        content = await self._http.get_bytes(
            (
                f"{self._samples_base_url}/summary/"
                f"{quote(station, safe='')}"
            ),
            provider=SAMPLES_PROVIDER,
            operation=operation,
            params=params,
            headers={"Accept": "text/csv"},
            deadline=deadline,
        )
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise invalid_response(SAMPLES_PROVIDER, operation) from error
        reader = csv.DictReader(StringIO(text))
        if reader.fieldnames is None or not _SAMPLE_COLUMNS.issubset(reader.fieldnames):
            raise invalid_response(SAMPLES_PROVIDER, operation)
        records: list[SamplePeriodOfRecord] = []
        try:
            for row in reader:
                if len(records) >= self._max_records:
                    raise PortResponseLimitError(
                        "USGS Samples response exceeded the configured record limit.",
                        provider=SAMPLES_PROVIDER,
                        operation=operation,
                    )
                row_station = require_string(
                    row.get("monitoringLocationIdentifier"),
                    provider=SAMPLES_PROVIDER,
                    operation=operation,
                )
                if row_station != station:
                    raise invalid_response(SAMPLES_PROVIDER, operation)
                characteristic = require_string(
                    row.get("characteristic"),
                    provider=SAMPLES_PROVIDER,
                    operation=operation,
                )
                records.append(
                    SamplePeriodOfRecord(
                        station_id=station,
                        characteristic=characteristic,
                        first_activity=date.fromisoformat(
                            require_string(
                                row.get("firstActivity"),
                                provider=SAMPLES_PROVIDER,
                                operation=operation,
                            )
                        ),
                        most_recent_activity=date.fromisoformat(
                            require_string(
                                row.get("mostRecentActivity"),
                                provider=SAMPLES_PROVIDER,
                                operation=operation,
                            )
                        ),
                        result_count=int(
                            require_string(
                                row.get("resultCount"),
                                provider=SAMPLES_PROVIDER,
                                operation=operation,
                            )
                        ),
                        activity_count=int(
                            require_string(
                                row.get("activityCount"),
                                provider=SAMPLES_PROVIDER,
                                operation=operation,
                            )
                        ),
                        provider_identity=(
                            f"usgs-samples:v1:{station}:{characteristic}"
                        ),
                    )
                )
        except (TypeError, ValueError) as error:
            raise invalid_response(SAMPLES_PROVIDER, operation) from error
        return tuple(
            sorted(
                records,
                key=lambda record: (
                    record.characteristic.casefold(),
                    record.first_activity,
                ),
            )
        )

    async def _ogc_features(
        self,
        collection: str,
        *,
        operation: str,
        query: Mapping[str, str],
        deadline: float | None,
    ) -> list[Mapping[str, object]]:
        url = f"{self._ogc_base_url}/collections/{collection}/items"
        params: dict[str, str | int] = {
            **query,
            "f": "json",
            "limit": self._page_size,
        }
        if self._api_key:
            params["api_key"] = self._api_key
        features: list[Mapping[str, object]] = []
        visited: set[str] = set()

        for _ in range(self._max_pages):
            if url in visited:
                raise invalid_response(PROVIDER, operation)
            visited.add(url)
            payload = await self._http.get_json(
                url,
                provider=PROVIDER,
                operation=operation,
                params=params,
                deadline=deadline,
            )
            root = require_mapping(payload, provider=PROVIDER, operation=operation)
            if root.get("type") != "FeatureCollection":
                raise invalid_response(PROVIDER, operation)
            page = require_list(
                root.get("features"),
                provider=PROVIDER,
                operation=operation,
            )
            features.extend(
                require_mapping(item, provider=PROVIDER, operation=operation)
                for item in page
            )
            if len(features) > self._max_records:
                raise PortResponseLimitError(
                    "USGS metadata exceeded the configured record limit.",
                    provider=PROVIDER,
                    operation=operation,
                )
            next_url = self._next_url(root, operation)
            if next_url is None:
                return features
            self._require_safe_next_url(next_url, operation)
            url = self._with_api_key(next_url)
            params = {}

        raise PortResponseLimitError(
            "USGS metadata exceeded the configured page limit.",
            provider=PROVIDER,
            operation=operation,
        )

    def _next_url(
        self,
        root: Mapping[str, object],
        operation: str,
    ) -> str | None:
        links = require_list(
            root.get("links", []),
            provider=PROVIDER,
            operation=operation,
        )
        next_links: list[str] = []
        for raw_link in links:
            link = require_mapping(
                raw_link,
                provider=PROVIDER,
                operation=operation,
            )
            if link.get("rel") == "next":
                next_links.append(
                    require_string(
                        link.get("href"),
                        provider=PROVIDER,
                        operation=operation,
                    )
                )
        if len(next_links) > 1:
            raise invalid_response(PROVIDER, operation)
        return next_links[0] if next_links else None

    def _require_safe_next_url(self, url: str, operation: str) -> None:
        base = urlsplit(self._ogc_base_url)
        target = urlsplit(url)
        if (
            target.scheme != base.scheme
            or target.netloc != base.netloc
            or not target.path.startswith(base.path.rstrip("/") + "/")
        ):
            raise invalid_response(PROVIDER, operation)

    def _with_api_key(self, url: str) -> str:
        if not self._api_key:
            return url
        parsed = urlsplit(url)
        query = [
            (name, value)
            for name, value in parse_qsl(parsed.query, keep_blank_values=True)
            if name != "api_key"
        ]
        query.append(("api_key", self._api_key))
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urlencode(query),
                parsed.fragment,
            )
        )

    def _parameter_metadata(
        self,
        feature: Mapping[str, object],
        operation: str,
    ) -> UsgsParameterMetadata:
        properties = self._properties(feature, operation)
        code = self._usgs_code(
            properties.get("id", feature.get("id")),
            operation,
        )
        return UsgsParameterMetadata(
            key=ParameterKey(code_system="usgs", code=code),
            name=require_string(
                properties.get("parameter_name"),
                provider=PROVIDER,
                operation=operation,
            ),
            unit=require_string(
                properties.get("unit_of_measure"),
                provider=PROVIDER,
                operation=operation,
            ),
            provider_identity=f"usgs-wdfn:v1:parameter-code:{code}",
        )

    def _monitoring_location(
        self,
        feature: Mapping[str, object],
        operation: str,
    ) -> MonitoringLocationMetadata:
        properties = self._properties(feature, operation)
        station_id = require_string(
            properties.get("id", feature.get("id")),
            provider=PROVIDER,
            operation=operation,
        )
        return MonitoringLocationMetadata(
            station_id=station_id,
            agency_code=require_string(
                properties.get("agency_code"),
                provider=PROVIDER,
                operation=operation,
            ),
            name=require_string(
                properties.get("monitoring_location_name"),
                provider=PROVIDER,
                operation=operation,
            ),
            location=self._point(feature, operation),
            provider_identity=f"usgs-wdfn:v1:monitoring-location:{station_id}",
        )

    def _time_series(
        self,
        feature: Mapping[str, object],
        operation: str,
    ) -> TimeSeriesMetadata | None:
        properties = self._properties(feature, operation)
        period = require_string(
            properties.get("computation_period_identifier"),
            provider=PROVIDER,
            operation=operation,
        )
        computation = require_string(
            properties.get("computation_identifier"),
            provider=PROVIDER,
            operation=operation,
        )
        if period.casefold() == "daily":
            kind = TimeSeriesKind.DAILY
        elif (
            period.casefold() == "points"
            and computation.casefold() == "instantaneous"
        ):
            kind = TimeSeriesKind.CONTINUOUS
        else:
            return None
        code = self._usgs_code(properties.get("parameter_code"), operation)
        series_id = require_string(
            properties.get("id", feature.get("id")),
            provider=PROVIDER,
            operation=operation,
        )
        try:
            return TimeSeriesMetadata(
                series_id=series_id,
                station_id=require_string(
                    properties.get("monitoring_location_id"),
                    provider=PROVIDER,
                    operation=operation,
                ),
                parameter=ParameterKey(code_system="usgs", code=code),
                unit=require_string(
                    properties.get("unit_of_measure"),
                    provider=PROVIDER,
                    operation=operation,
                ),
                begin=self._datetime(properties.get("begin"), operation),
                end=self._datetime(properties.get("end"), operation),
                kind=kind,
                provider_identity=f"usgs-wdfn:v1:time-series:{series_id}",
            )
        except (TypeError, ValueError) as error:
            raise invalid_response(PROVIDER, operation) from error

    @staticmethod
    def _properties(
        feature: Mapping[str, object],
        operation: str,
    ) -> Mapping[str, object]:
        if feature.get("type") != "Feature":
            raise invalid_response(PROVIDER, operation)
        return require_mapping(
            feature.get("properties"),
            provider=PROVIDER,
            operation=operation,
        )

    @staticmethod
    def _point(
        feature: Mapping[str, object],
        operation: str,
    ) -> GeoPoint:
        geometry = require_mapping(
            feature.get("geometry"),
            provider=PROVIDER,
            operation=operation,
        )
        if geometry.get("type") != "Point":
            raise invalid_response(PROVIDER, operation)
        coordinates = require_list(
            geometry.get("coordinates"),
            provider=PROVIDER,
            operation=operation,
        )
        if len(coordinates) < 2:
            raise invalid_response(PROVIDER, operation)
        longitude = require_number(
            coordinates[0],
            provider=PROVIDER,
            operation=operation,
        )
        latitude = require_number(
            coordinates[1],
            provider=PROVIDER,
            operation=operation,
        )
        return GeoPoint(latitude=latitude, longitude=longitude)

    @staticmethod
    def _datetime(value: object, operation: str) -> datetime:
        text = require_string(value, provider=PROVIDER, operation=operation)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise invalid_response(PROVIDER, operation)
        return parsed

    @staticmethod
    def _parameter_codes(parameters: tuple[ParameterKey, ...]) -> tuple[str, ...]:
        codes: set[str] = set()
        for parameter in parameters:
            if parameter.code_system != "usgs":
                raise ValueError("USGS metadata requires usgs parameter keys")
            if len(parameter.code) != 5 or not parameter.code.isdecimal():
                raise ValueError("USGS parameter code must contain five digits")
            codes.add(parameter.code)
        return tuple(sorted(codes))

    @staticmethod
    def _identifiers(
        values: tuple[str, ...],
        *,
        field_name: str,
    ) -> tuple[str, ...]:
        normalized: set[str] = set()
        for value in values:
            candidate = value.strip()
            if not candidate or "," in candidate:
                raise ValueError(f"{field_name} contains an invalid identifier")
            normalized.add(candidate)
        return tuple(sorted(normalized))

    @staticmethod
    def _usgs_code(value: object, operation: str) -> str:
        code = require_string(value, provider=PROVIDER, operation=operation)
        if len(code) != 5 or not code.isdecimal():
            raise invalid_response(PROVIDER, operation)
        return code

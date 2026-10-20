from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import ValidationError

from app.domain import (
    CollectionCompletionContract,
    CollectionPageManifest,
    EvidenceLocation,
    GeoPoint,
    Provenance,
    ProviderCollectionBatch,
    ProviderQuality,
    TimeRange,
    WeatherEvidenceRequest,
    WeatherForecastRecord,
    WeatherObservationRecord,
    geodesic_distance_km,
)
from app.infrastructure.adapters._evidence_collection import (
    build_batch,
    request_identity,
    safe_error_code,
)
from app.infrastructure.adapters._parsing import (
    invalid_response,
    require_list,
    require_mapping,
    require_string,
)
from app.infrastructure.http import ProviderHttpClient
from app.ports import PortError, PortResponseLimitError

PROVIDER = "nws_weather"
DEFAULT_BASE_URL = "https://api.weather.gov"
_DURATION = re.compile(
    r"^P(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?:(?P<seconds>\d+)S)?)?$"
)

WeatherRecord = WeatherObservationRecord | WeatherForecastRecord


class NwsWeatherAdapter:
    """NWS linked-data weather collector with fixed-origin navigation."""

    def __init__(
        self,
        http: ProviderHttpClient,
        *,
        user_agent_project: str,
        user_agent_contact: str,
        base_url: str = DEFAULT_BASE_URL,
        page_size: int = 500,
        max_pages: int = 10,
        max_records: int = 10_000,
        max_stations: int = 8,
        clock=None,
    ) -> None:
        project = user_agent_project.strip()
        contact = user_agent_contact.strip()
        if (
            not project
            or not contact
            or any("\r" in item or "\n" in item for item in (project, contact))
        ):
            raise ValueError("NWS User-Agent project and contact are required")
        if not 1 <= page_size <= 500:
            raise ValueError("NWS page_size must be between 1 and 500")
        if max_pages <= 0 or max_records <= 0 or max_stations <= 0:
            raise ValueError("NWS collection limits must be positive")
        parsed = urlsplit(base_url.rstrip("/"))
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.weather.gov"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("NWS base_url must be the api.weather.gov HTTPS origin")
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._user_agent = f"{project} ({contact})"
        self._page_size = page_size
        self._max_pages = max_pages
        self._max_records = max_records
        self._max_stations = max_stations
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def collect(
        self,
        request: WeatherEvidenceRequest,
        *,
        deadline: float | None = None,
    ) -> ProviderCollectionBatch[WeatherRecord]:
        await asyncio.sleep(0)
        pages: list[CollectionPageManifest] = []
        records: list[WeatherRecord] = []
        request_count = 0
        retrieved_at = self._retrieved_at()
        point_url = (
            f"{self._base_url}/points/"
            f"{request.location.latitude:.4f},{request.location.longitude:.4f}"
        )
        root_identity = request_identity(point_url)
        collection_incomplete_reason: str | None = None

        def count_attempt() -> None:
            nonlocal request_count
            request_count += 1

        try:
            point, size = await self._get(
                point_url,
                operation=request.operation_id,
                deadline=deadline,
                on_attempt=count_attempt,
            )
            properties = require_mapping(
                point.get("properties"),
                provider=PROVIDER,
                operation=request.operation_id,
            )
            discovered_links = {
                name: self._linked_url(
                    properties.get(name), request.operation_id
                )
                for name in (
                    "forecast",
                    "forecastHourly",
                    "forecastGridData",
                    "observationStations",
                )
            }
            self._append_page(pages, root_identity, 0, size)
            if request.evidence_kind == "forecast":
                grid_url = discovered_links["forecastGridData"]
                grid_id = self._grid_id(properties, request.operation_id)
                await self._collect_forecast(
                    request,
                    grid_url=grid_url,
                    grid_id=grid_id,
                    root_identity=root_identity,
                    retrieved_at=retrieved_at,
                    records=records,
                    pages=pages,
                    deadline=deadline,
                    on_attempt=count_attempt,
                )
            else:
                stations_url = discovered_links["observationStations"]
                collection_incomplete_reason = await self._collect_observations(
                    request,
                    stations_url=stations_url,
                    root_identity=root_identity,
                    retrieved_at=retrieved_at,
                    records=records,
                    pages=pages,
                    deadline=deadline,
                    on_attempt=count_attempt,
                )
        except PortError as error:
            if not pages:
                raise
            failed_identity = pages[-1].next_page_identity
            if failed_identity is None:
                pages[-1] = pages[-1].model_copy(
                    update={"page_complete": False, "next_page_identity": None}
                )
            else:
                self._append_page(pages, failed_identity, 0, 0, complete=False)
            return build_batch(
                operation_id=request.operation_id,
                records=records,
                pages=pages,
                request_count=request_count,
                complete=False,
                completed_ranges=(),
                contract=CollectionCompletionContract.OFFICIAL_NEXT_CHAIN,
                incomplete_reason=safe_error_code(error),
            )

        return build_batch(
            operation_id=request.operation_id,
            records=records,
            pages=pages,
            request_count=request_count,
            complete=collection_incomplete_reason is None,
            completed_ranges=(request.request_range,)
            if collection_incomplete_reason is None
            else (),
            contract=CollectionCompletionContract.OFFICIAL_NEXT_CHAIN,
            incomplete_reason=collection_incomplete_reason,
        )

    async def _collect_forecast(
        self,
        request: WeatherEvidenceRequest,
        *,
        grid_url: str,
        grid_id: str,
        root_identity: str,
        retrieved_at: datetime,
        records: list[WeatherRecord],
        pages: list[CollectionPageManifest],
        deadline: float | None,
        on_attempt,
    ) -> None:
        self._reserve_page(pages, grid_url, None, request.operation_id)
        root, size = await self._get(
            grid_url,
            operation=request.operation_id,
            deadline=deadline,
            on_attempt=on_attempt,
        )
        properties = require_mapping(
            root.get("properties"), provider=PROVIDER, operation=request.operation_id
        )
        issued_at = self._datetime(properties.get("updateTime"), request.operation_id)
        page_identity = request_identity(grid_url)
        parsed: list[WeatherForecastRecord] = []
        for variable in request.variables:
            series = properties.get(variable)
            if series is None:
                continue
            series_value = require_mapping(
                series, provider=PROVIDER, operation=request.operation_id
            )
            unit = require_string(
                series_value.get("uom"),
                provider=PROVIDER,
                operation=request.operation_id,
            )
            values = require_list(
                series_value.get("values"),
                provider=PROVIDER,
                operation=request.operation_id,
            )
            for raw_value in values:
                item = require_mapping(
                    raw_value, provider=PROVIDER, operation=request.operation_id
                )
                valid_range, duration = self._interval(
                    item.get("validTime"), request.operation_id
                )
                if not request.request_range.overlaps(valid_range):
                    continue
                value = item.get("value")
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise invalid_response(PROVIDER, request.operation_id)
                provider_id = self._provider_id(
                    grid_url, variable, valid_range.start.isoformat(), str(value)
                )
                try:
                    parsed.append(
                        WeatherForecastRecord(
                            provider=PROVIDER,
                            channel="forecast_grid_data",
                            source_uri=grid_url,
                            retrieved_at=retrieved_at,
                            observed_time=None,
                            valid_time_range=valid_range,
                            location=EvidenceLocation(
                                point=request.location,
                                comid=None,
                                station_id=None,
                                distance_to_target_km=None,
                            ),
                            provider_quality=ProviderQuality(approval="official"),
                            raw_unit=unit,
                            provenance=Provenance(
                                operation_id=request.operation_id,
                                request_identity=root_identity,
                                page_identity=page_identity,
                                provider_series_id=f"{grid_id}:{variable}",
                                provider_record_id=provider_id,
                            ),
                            grid_id=grid_id,
                            issued_at=issued_at,
                            weather_variable=variable,
                            value=str(value),
                            duration_seconds=duration,
                            statistic="forecast_value",
                        )
                    )
                except (ValidationError, ValueError) as error:
                    raise invalid_response(PROVIDER, request.operation_id) from error
        accepted = self._accept(records, parsed, request.operation_id)
        self._append_page(pages, page_identity, accepted, size)

    async def _collect_observations(
        self,
        request: WeatherEvidenceRequest,
        *,
        stations_url: str,
        root_identity: str,
        retrieved_at: datetime,
        records: list[WeatherRecord],
        pages: list[CollectionPageManifest],
        deadline: float | None,
        on_attempt,
    ) -> str | None:
        # The provider does not document a global distance order for station pages.
        # Select nearest stations within one explicitly bounded page, and retain a
        # partial result when additional candidate pages remain.
        station_limit = min(self._max_stations, 500)
        station_params = {"limit": station_limit}
        self._reserve_page(pages, stations_url, station_params, request.operation_id)
        root, size = await self._get(
            stations_url,
            params=station_params,
            operation=request.operation_id,
            deadline=deadline,
            on_attempt=on_attempt,
        )
        station_page_identity = request_identity(stations_url, station_params)
        features = require_list(
            root.get("features"), provider=PROVIDER, operation=request.operation_id
        )
        stations: list[tuple[str, str, GeoPoint]] = []
        for raw_feature in features:
            feature = require_mapping(
                raw_feature, provider=PROVIDER, operation=request.operation_id
            )
            station_url = self._linked_url(feature.get("id"), request.operation_id)
            properties = require_mapping(
                feature.get("properties"),
                provider=PROVIDER,
                operation=request.operation_id,
            )
            station_id = require_string(
                properties.get("stationIdentifier"),
                provider=PROVIDER,
                operation=request.operation_id,
            )
            point = self._point(feature.get("geometry"), request.operation_id)
            candidate = (station_url, station_id, point)
            if candidate not in stations:
                stations.append(candidate)
        more_candidates = self._pagination_next(
            root, stations_url, request.operation_id, station_ids=True
        ) is not None
        self._append_page(pages, station_page_identity, 0, size)
        stations.sort(
            key=lambda candidate: (
                geodesic_distance_km(
                    (request.location.longitude, request.location.latitude),
                    (candidate[2].longitude, candidate[2].latitude),
                ),
                candidate[1],
            )
        )
        candidates_truncated = len(stations) > station_limit
        stations = stations[: min(station_limit, self._max_pages - len(pages))]
        candidates_truncated |= len(stations) < min(len(features), station_limit)
        pagination_incomplete = False

        for station_url, station_id, station_point in stations:
            params: Mapping[str, object] | None = {
                "start": self._iso(request.request_range.start),
                "end": self._iso(request.request_range.end),
                "limit": self._page_size,
            }
            next_url = f"{station_url}/observations"
            while next_url is not None:
                self._reserve_page(pages, next_url, params, request.operation_id)
                root, size = await self._get(
                    next_url,
                    params=params,
                    operation=request.operation_id,
                    deadline=deadline,
                    on_attempt=on_attempt,
                )
                page_identity = request_identity(next_url, params)
                page_records = self._observation_records(
                    root,
                    request=request,
                    station_id=station_id,
                    station_point=station_point,
                    root_identity=root_identity,
                    station_page_identity=station_page_identity,
                    page_identity=page_identity,
                    source_uri=self._canonical_uri(next_url, params),
                    retrieved_at=retrieved_at,
                )
                accepted = self._accept(
                    records, page_records, request.operation_id
                )
                self._append_page(pages, page_identity, accepted, size)
                provider_next = self._pagination_next(
                    root, next_url, request.operation_id
                )
                if provider_next is not None:
                    next_query = dict(parse_qsl(urlsplit(provider_next).query))
                    if (
                        next_query.get("start")
                        != self._iso(request.request_range.start)
                        or next_query.get("end")
                        != self._iso(request.request_range.end)
                    ):
                        # NWS has returned cursor-only links that drop the time
                        # filter and page into observations outside this request.
                        pagination_incomplete = True
                        break
                next_url = provider_next
                params = None

        if pagination_incomplete:
            return "evidence_provider_response_invalid"
        if more_candidates or candidates_truncated:
            return "evidence_provider_response_too_large"
        return None

    def _observation_records(
        self,
        root: Mapping[str, object],
        *,
        request: WeatherEvidenceRequest,
        station_id: str,
        station_point: GeoPoint,
        root_identity: str,
        station_page_identity: str,
        page_identity: str,
        source_uri: str,
        retrieved_at: datetime,
    ) -> tuple[WeatherObservationRecord, ...]:
        features = require_list(
            root.get("features"), provider=PROVIDER, operation=request.operation_id
        )
        result: list[WeatherObservationRecord] = []
        for raw_feature in features:
            feature = require_mapping(
                raw_feature, provider=PROVIDER, operation=request.operation_id
            )
            properties = require_mapping(
                feature.get("properties"),
                provider=PROVIDER,
                operation=request.operation_id,
            )
            observed_at = self._datetime(
                properties.get("timestamp"), request.operation_id
            )
            if not request.request_range.contains(observed_at):
                continue
            observation_id = require_string(
                properties.get("@id") or feature.get("id"),
                provider=PROVIDER,
                operation=request.operation_id,
            )
            for variable in request.variables:
                measurement = properties.get(variable)
                if measurement is None:
                    continue
                value_object = require_mapping(
                    measurement, provider=PROVIDER, operation=request.operation_id
                )
                value = value_object.get("value")
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise invalid_response(PROVIDER, request.operation_id)
                unit = require_string(
                    value_object.get("unitCode"),
                    provider=PROVIDER,
                    operation=request.operation_id,
                )
                quality = value_object.get("qualityControl")
                if quality is not None and not isinstance(quality, str):
                    raise invalid_response(PROVIDER, request.operation_id)
                duration, statistic = self._observation_semantics(variable)
                try:
                    result.append(
                        WeatherObservationRecord(
                            provider=PROVIDER,
                            channel="station_observations",
                            source_uri=source_uri,
                            retrieved_at=retrieved_at,
                            observed_time=observed_at,
                            valid_time_range=None,
                            location=EvidenceLocation(
                                point=station_point,
                                comid=None,
                                station_id=station_id,
                                distance_to_target_km=None,
                            ),
                            provider_quality=ProviderQuality(
                                approval="official",
                                qualifiers=(f"quality_control:{quality}",)
                                if quality and quality.strip()
                                else (),
                            ),
                            raw_unit=unit,
                            provenance=Provenance(
                                operation_id=request.operation_id,
                                request_identity=root_identity,
                                page_identity=page_identity,
                                discovery_identities=(station_page_identity,),
                                provider_series_id=f"{station_id}:{variable}",
                                provider_record_id=self._provider_id(
                                    observation_id, variable
                                ),
                            ),
                            grid_id=None,
                            weather_variable=variable,
                            value=str(value),
                            duration_seconds=duration,
                            statistic=statistic,
                        )
                    )
                except (ValidationError, ValueError) as error:
                    raise invalid_response(PROVIDER, request.operation_id) from error
        return tuple(result)

    async def _get(
        self,
        url: str,
        *,
        operation: str,
        params: Mapping[str, object] | None = None,
        deadline: float | None,
        on_attempt,
    ) -> tuple[Mapping[str, object], int]:
        content = await self._http.get_bytes(
            url,
            provider=PROVIDER,
            operation=operation,
            params=params,
            headers={
                "Accept": "application/geo+json",
                "User-Agent": self._user_agent,
            },
            deadline=deadline,
            on_attempt=on_attempt,
        )
        try:
            decoded = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise invalid_response(PROVIDER, operation) from error
        return (
            require_mapping(decoded, provider=PROVIDER, operation=operation),
            len(content),
        )

    def _pagination_next(
        self,
        root: Mapping[str, object],
        current_url: str,
        operation: str,
        *,
        station_ids: bool = False,
    ) -> str | None:
        pagination = root.get("pagination")
        if pagination is None:
            return None
        value = require_mapping(pagination, provider=PROVIDER, operation=operation)
        next_value = value.get("next")
        if next_value is None:
            return None
        next_url = self._linked_url(next_value, operation)
        current_path = urlsplit(current_url).path
        next_path = urlsplit(next_url).path
        if next_path != current_path and not (
            station_ids
            and current_path.startswith("/gridpoints/")
            and current_path.endswith("/stations")
            and next_path == "/stations"
        ):
            raise invalid_response(PROVIDER, operation)
        allowed = {"start", "end", "limit", "cursor"}
        if any(
            name not in allowed
            and not (station_ids and re.fullmatch(r"id\[\d+\]", name))
            for name, _ in parse_qsl(urlsplit(next_url).query)
        ):
            raise invalid_response(PROVIDER, operation)
        return next_url

    def _linked_url(self, value: object, operation: str) -> str:
        url = require_string(value, provider=PROVIDER, operation=operation)
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.weather.gov"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise invalid_response(PROVIDER, operation)
        return urlunsplit(("https", "api.weather.gov", parsed.path, parsed.query, ""))

    def _reserve_page(
        self,
        pages: list[CollectionPageManifest],
        url: str,
        params: Mapping[str, object] | None,
        operation: str,
    ) -> None:
        identity = request_identity(url, params)
        if len(pages) >= self._max_pages or any(
            page.page_identity == identity for page in pages
        ):
            raise PortResponseLimitError(
                "NWS pagination exceeded the configured limit.",
                provider=PROVIDER,
                operation=operation,
            )
        if pages:
            pages[-1] = pages[-1].model_copy(
                update={"next_page_identity": identity}
            )

    @staticmethod
    def _append_page(
        pages: list[CollectionPageManifest],
        identity: str,
        records: int,
        response_bytes: int,
        *,
        complete: bool = True,
    ) -> None:
        if pages and pages[-1].page_identity != identity:
            pages[-1] = pages[-1].model_copy(
                update={"next_page_identity": identity}
            )
        pages.append(
            CollectionPageManifest(
                page_identity=identity,
                record_count=records,
                response_bytes=response_bytes,
                page_complete=complete,
            )
        )

    def _accept(
        self,
        records: list[WeatherRecord],
        additions,
        operation: str,
    ) -> int:
        by_id = {item.record_id: item for item in records}
        accepted = 0
        for item in additions:
            previous = by_id.get(item.record_id)
            if previous is not None:
                if not self._same_record(previous, item):
                    raise invalid_response(PROVIDER, operation)
                continue
            if len(records) >= self._max_records:
                raise PortResponseLimitError(
                    "NWS records exceeded the configured limit.",
                    provider=PROVIDER,
                    operation=operation,
                )
            by_id[item.record_id] = item
            records.append(item)
            accepted += 1
        return accepted

    @staticmethod
    def _same_record(first: WeatherRecord, second: WeatherRecord) -> bool:
        first_value = first.model_dump(mode="json")
        second_value = second.model_dump(mode="json")
        for value in (first_value, second_value):
            value.pop("source_uri", None)
            provenance = value.get("provenance")
            if isinstance(provenance, dict):
                provenance.pop("page_identity", None)
        return first_value == second_value

    @staticmethod
    def _grid_id(properties: Mapping[str, object], operation: str) -> str:
        office = require_string(
            properties.get("gridId"), provider=PROVIDER, operation=operation
        )
        x = properties.get("gridX")
        y = properties.get("gridY")
        if (
            isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, int)
            or not isinstance(y, int)
        ):
            raise invalid_response(PROVIDER, operation)
        return f"{office}/{x},{y}"

    @staticmethod
    def _point(value: object, operation: str) -> GeoPoint:
        geometry = require_mapping(value, provider=PROVIDER, operation=operation)
        if geometry.get("type") != "Point":
            raise invalid_response(PROVIDER, operation)
        coordinates = require_list(
            geometry.get("coordinates"), provider=PROVIDER, operation=operation
        )
        if len(coordinates) < 2:
            raise invalid_response(PROVIDER, operation)
        try:
            return GeoPoint(latitude=coordinates[1], longitude=coordinates[0])
        except (ValidationError, TypeError, ValueError) as error:
            raise invalid_response(PROVIDER, operation) from error

    @staticmethod
    def _datetime(value: object, operation: str) -> datetime:
        text = require_string(value, provider=PROVIDER, operation=operation)
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as error:
            raise invalid_response(PROVIDER, operation) from error
        if result.tzinfo is None or result.utcoffset() is None:
            raise invalid_response(PROVIDER, operation)
        return result.astimezone(timezone.utc)

    @classmethod
    def _interval(cls, value: object, operation: str) -> tuple[TimeRange, int]:
        text = require_string(value, provider=PROVIDER, operation=operation)
        start_text, separator, duration_text = text.partition("/")
        if not separator:
            raise invalid_response(PROVIDER, operation)
        start = cls._datetime(start_text, operation)
        match = _DURATION.fullmatch(duration_text)
        if match is None or not any(match.groupdict().values()):
            raise invalid_response(PROVIDER, operation)
        duration = timedelta(
            days=int(match.group("days") or 0),
            hours=int(match.group("hours") or 0),
            minutes=int(match.group("minutes") or 0),
            seconds=int(match.group("seconds") or 0),
        )
        seconds = int(duration.total_seconds())
        if seconds <= 0:
            raise invalid_response(PROVIDER, operation)
        return TimeRange(start=start, end=start + duration), seconds

    @staticmethod
    def _observation_semantics(variable: str) -> tuple[int | None, str]:
        durations = {
            "precipitationLastHour": 3600,
            "precipitationLast3Hours": 10800,
            "precipitationLast6Hours": 21600,
            "maxTemperatureLast24Hours": 86400,
            "minTemperatureLast24Hours": 86400,
        }
        if variable.startswith("maxTemperature"):
            return durations.get(variable), "maximum"
        if variable.startswith("minTemperature"):
            return durations.get(variable), "minimum"
        if variable.startswith("precipitationLast"):
            return durations.get(variable), "accumulation"
        return None, "instantaneous"

    def _retrieved_at(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("NWS adapter clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _provider_id(*values: str) -> str:
        digest = sha256("\x1f".join(values).encode("utf-8")).hexdigest()
        return f"sha256:{digest}"

    @staticmethod
    def _canonical_uri(url: str, params: Mapping[str, object] | None) -> str:
        parsed = urlsplit(url)
        pairs = list(parse_qsl(parsed.query, keep_blank_values=True))
        if params:
            pairs.extend((name, str(value)) for name, value in params.items())
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urlencode(sorted(pairs)), "")
        )

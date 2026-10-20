from __future__ import annotations

import csv
import json
import logging
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta, timezone
from io import StringIO
from urllib.parse import parse_qsl, quote, urlsplit

from app.domain.hydrology import (
    AvailabilityTimeRange,
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
from app.ports import (
    InvalidPortResponseError,
    PortResponseLimitError,
    UnsupportedPortVersionError,
)

PROVIDER = "usgs_waterdata"
SAMPLES_PROVIDER = "usgs_samples"
logger = logging.getLogger("wateragents.usgs_waterdata")
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
_SAMPLE_PROBE_COLUMNS = {
    "Location_Identifier",
    "Activity_Media",
    "Activity_StartDate",
    "Activity_StartTime",
    "Activity_StartTimeZone",
    "Result_Characteristic",
    "Result_Measure",
    "USGSpcode",
}
_MAX_CONTINUOUS_PROBE_SPAN = timedelta(days=365 * 3)
_SAMPLE_TIMEZONE_OFFSETS = {
    "Z": "+00:00", "UTC": "+00:00", "GMT": "+00:00",
    "EST": "-05:00", "EDT": "-04:00",
    "CST": "-06:00", "CDT": "-05:00",
    "MST": "-07:00", "MDT": "-06:00",
    "PST": "-08:00", "PDT": "-07:00",
    "AKST": "-09:00", "AKDT": "-08:00",
    "HST": "-10:00",
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
        content = await self._http.get_bytes(
            (
                f"{self._samples_base_url}/summary/"
                f"{quote(station, safe='')}"
            ),
            provider=SAMPLES_PROVIDER,
            operation=operation,
            params=params,
            headers=self._headers("text/csv"),
            deadline=deadline,
        )
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise invalid_response(SAMPLES_PROVIDER, operation) from error
        reader = csv.DictReader(StringIO(text))
        if reader.fieldnames is None:
            raise invalid_response(SAMPLES_PROVIDER, operation)
        empty_header = (_SAMPLE_COLUMNS - {"activityCount"}) | {"activities"}
        if set(reader.fieldnames) == empty_header:
            if next(reader, None) is None:
                return ()
            raise invalid_response(SAMPLES_PROVIDER, operation)
        if not _SAMPLE_COLUMNS.issubset(reader.fieldnames):
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
                characteristic_group = require_string(
                    row.get("characteristicGroup"),
                    provider=SAMPLES_PROVIDER,
                    operation=operation,
                )
                characteristic_user_supplied = require_string(
                    row.get("characteristicUserSupplied"),
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
                            "usgs-samples:v1:"
                            + json.dumps(
                                (
                                    station,
                                    characteristic_group,
                                    characteristic,
                                    characteristic_user_supplied,
                                ),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            )
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

    async def get_time_series_observation_windows(
        self,
        series: TimeSeriesMetadata,
        requested: tuple[AvailabilityTimeRange, ...],
        *,
        deadline: float | None = None,
    ) -> tuple[int, ...]:
        operation = "get_time_series_observation_windows"
        self._validate_probe_windows(requested)
        if not requested:
            return ()
        if series.parameter.code_system != "usgs":
            raise ValueError("USGS observation probe requires a USGS parameter")
        if (
            series.kind == TimeSeriesKind.CONTINUOUS
            and requested[-1].end - requested[0].start > _MAX_CONTINUOUS_PROBE_SPAN
        ):
            raise PortResponseLimitError(
                "USGS continuous observations cannot be queried over more than three years at once.",
                provider=PROVIDER,
                operation=operation,
            )
        end = requested[-1].end - timedelta(microseconds=1)
        interval = (
            f"{(requested[0].start.date() - timedelta(days=1)).isoformat()}/"
            f"{(end.date() + timedelta(days=1)).isoformat()}"
            if series.kind.value == "daily"
            else f"{self._iso(requested[0].start)}/{self._iso(end)}"
        )
        url = f"{self._ogc_base_url}/collections/{series.kind.value}/items"
        params: dict[str, str | int] = {
            "monitoring_location_id": series.station_id,
            "time_series_id": series.series_id,
            "parameter_code": series.parameter.code,
            "datetime": interval,
            "f": "json",
            "limit": self._page_size,
        }
        visited: set[str] = set()
        found: set[int] = set()
        record_count = 0
        for page_number in range(1, self._max_pages + 1):
            record_index = None
            validation_step = "pagination"
            try:
                if url in visited:
                    raise invalid_response(PROVIDER, operation)
                visited.add(url)
                validation_step = "json"
                payload = await self._http.get_json(
                    url,
                    provider=PROVIDER,
                    operation=operation,
                    params=params,
                    headers=self._headers("application/geo+json"),
                    deadline=deadline,
                )
                validation_step = "collection"
                root = require_mapping(payload, provider=PROVIDER, operation=operation)
                if root.get("type") != "FeatureCollection":
                    raise invalid_response(PROVIDER, operation)
                validation_step = "features"
                page = require_list(
                    root.get("features"), provider=PROVIDER, operation=operation
                )
                validation_step = "pagination"
                next_url = self._next_url(root, operation)
                if not page and next_url is not None:
                    raise PortResponseLimitError(
                        "USGS observation probe returned an empty page before completion.",
                        provider=PROVIDER,
                        operation=operation,
                    )
                for record_index, feature in enumerate(page):
                    record_count += 1
                    validation_step = "record_limit"
                    if record_count > self._max_records:
                        raise PortResponseLimitError(
                            "USGS observation probe exceeded the configured record limit.",
                            provider=PROVIDER,
                            operation=operation,
                        )
                    validation_step = "properties"
                    properties = self._properties(
                        require_mapping(
                            feature, provider=PROVIDER, operation=operation
                        ),
                        operation,
                    )
                    validation_step = "time_series_id"
                    if (
                        require_string(
                            properties.get("time_series_id"),
                            provider=PROVIDER,
                            operation=operation,
                        )
                        != series.series_id
                    ):
                        raise invalid_response(PROVIDER, operation)
                    validation_step = "monitoring_location_id"
                    if (
                        require_string(
                            properties.get("monitoring_location_id"),
                            provider=PROVIDER,
                            operation=operation,
                        )
                        != series.station_id
                    ):
                        raise invalid_response(PROVIDER, operation)
                    validation_step = "parameter_code"
                    if (
                        self._usgs_code(properties.get("parameter_code"), operation)
                        != series.parameter.code
                    ):
                        raise invalid_response(PROVIDER, operation)
                    validation_step = "value"
                    if "value" not in properties:
                        raise invalid_response(PROVIDER, operation)
                    has_measurement = properties["value"] is not None
                    if has_measurement:
                        require_string(
                            properties["value"], provider=PROVIDER, operation=operation
                        )
                    validation_step = "time"
                    observed = require_string(
                        properties.get("time"), provider=PROVIDER, operation=operation
                    )
                    validation_step = "time_window"
                    try:
                        if series.kind.value == "daily" and len(observed) == 10:
                            day = date.fromisoformat(observed)
                            if not (
                                requested[0].start.date() - timedelta(days=1)
                                <= day
                                <= end.date() + timedelta(days=1)
                            ):
                                raise invalid_response(PROVIDER, operation)
                            day_start, day_end = self._date_without_zone_bounds(day)
                            if has_measurement:
                                found.update(
                                    item.window_index
                                    for item in requested
                                    if item.start <= day_start and day_end <= item.end
                                )
                        else:
                            timestamp = self._datetime(observed, operation)
                            if not requested[0].start <= timestamp < requested[-1].end:
                                raise invalid_response(PROVIDER, operation)
                            if has_measurement:
                                found.update(
                                    item.window_index
                                    for item in requested
                                    if item.start <= timestamp < item.end
                                )
                    except ValueError as error:
                        raise invalid_response(PROVIDER, operation) from error
                    if len(found) == len(requested):
                        return tuple(sorted(found))
                if next_url is None:
                    return tuple(sorted(found))
                record_index = None
                validation_step = "pagination"
                self._require_safe_next_url(next_url, operation)
            except InvalidPortResponseError as error:
                logger.warning(
                    "USGS time-series observation probe response failed validation.",
                    extra={
                        "event_name": "usgs_observation_probe_validation_failed",
                        "provider": PROVIDER,
                        "provider_operation": operation,
                        "provider_error": type(error).__name__,
                        "station_id": series.station_id,
                        "series_id": series.series_id,
                        "parameter_code": series.parameter.code,
                        "page_number": page_number,
                        "record_index": record_index,
                        "validation_step": validation_step,
                    },
                )
                raise
            url = next_url
            params = {}
        raise PortResponseLimitError(
            "USGS observation probe exceeded the configured page limit.",
            provider=PROVIDER,
            operation=operation,
        )

    async def get_sample_observation_windows(
        self,
        period: SamplePeriodOfRecord,
        parameter: ParameterKey,
        requested: tuple[AvailabilityTimeRange, ...],
        *,
        deadline: float | None = None,
    ) -> tuple[int, ...]:
        operation = "get_sample_observation_windows"
        self._validate_probe_windows(requested)
        if not requested:
            return ()
        codes = self._parameter_codes((parameter,))
        identity = self._sample_probe_identity(period, operation)
        # Samples filters use the activity's local date, which can be a day
        # earlier or later than the UTC date of the result window.
        lower_date = requested[0].start.date() - timedelta(days=1)
        upper_date = (requested[-1].end - timedelta(microseconds=1)).date() + timedelta(
            days=1
        )
        content = await self._http.get_bytes(
            f"{self._samples_base_url}/results/narrow",
            provider=SAMPLES_PROVIDER,
            operation=operation,
            params={
                "mimeType": "text/csv",
                "activityMediaName": "Water",
                "monitoringLocationIdentifier": period.station_id,
                "usgsPCode": codes,
                "characteristicGroup": identity[1],
                "characteristic": identity[2],
                "characteristicUserSupplied": identity[3],
                "activityStartDateLower": lower_date.isoformat(),
                "activityStartDateUpper": upper_date.isoformat(),
            },
            headers=self._headers("text/csv"),
            deadline=deadline,
        )
        found: set[int] = set()
        try:
            reader = csv.DictReader(StringIO(content.decode("utf-8-sig"), newline=""))
            if reader.fieldnames is None or not _SAMPLE_PROBE_COLUMNS.issubset(
                reader.fieldnames
            ):
                raise invalid_response(SAMPLES_PROVIDER, operation)
            for count, row in enumerate(reader, start=1):
                if count > self._max_records:
                    raise PortResponseLimitError(
                        "USGS Samples probe exceeded the configured record limit.",
                        provider=SAMPLES_PROVIDER,
                        operation=operation,
                    )
                observed_start, observed_end = self._validated_sample_probe_time(
                    row, period, parameter, operation
                )
                timed = bool(row.get("Activity_StartTime"))
                if not timed:
                    observed_start, observed_end = self._date_without_zone_bounds(
                        observed_start.date()
                    )
                found.update(
                    item.window_index
                    for item in requested
                    if (
                        observed_start < item.end and observed_end > item.start
                        if timed
                        else item.start <= observed_start and observed_end <= item.end
                    )
                )
                if len(found) == len(requested):
                    return tuple(sorted(found))
        except (UnicodeDecodeError, csv.Error, TypeError, ValueError) as error:
            raise invalid_response(SAMPLES_PROVIDER, operation) from error
        return tuple(sorted(found))

    @staticmethod
    def _sample_probe_identity(
        period: SamplePeriodOfRecord, operation: str
    ) -> list[str]:
        try:
            prefix = "usgs-samples:v1:"
            if not period.provider_identity.startswith(prefix):
                raise ValueError("invalid sample identity")
            identity = json.loads(period.provider_identity[len(prefix) :])
            if (
                not isinstance(identity, list)
                or len(identity) != 4
                or any(
                    not isinstance(item, str) or not item.strip() for item in identity
                )
                or identity[0] != period.station_id
                or identity[2] != period.characteristic
            ):
                raise ValueError("invalid sample identity")
        except (TypeError, ValueError) as error:
            raise invalid_response(SAMPLES_PROVIDER, operation) from error
        return identity

    def _validated_sample_probe_time(
        self,
        row: Mapping[str | None, str | list[str] | None],
        period: SamplePeriodOfRecord,
        parameter: ParameterKey,
        operation: str,
    ) -> tuple[datetime, datetime]:
        row_code = require_string(
            row.get("USGSpcode"), provider=SAMPLES_PROVIDER, operation=operation
        )
        if None in row or (
            require_string(
                row.get("Location_Identifier"),
                provider=SAMPLES_PROVIDER,
                operation=operation,
            )
            != period.station_id
            or row_code != parameter.code
            or require_string(
                row.get("Result_Characteristic"),
                provider=SAMPLES_PROVIDER,
                operation=operation,
            )
            != period.characteristic
            or require_string(
                row.get("Activity_Media"),
                provider=SAMPLES_PROVIDER,
                operation=operation,
            )
            != "Water"
        ):
            raise invalid_response(SAMPLES_PROVIDER, operation)
        require_string(
            row.get("Result_Measure"),
            provider=SAMPLES_PROVIDER,
            operation=operation,
        )
        return self._sample_probe_time(row, operation)

    @staticmethod
    def _sample_probe_time(
        row: Mapping[str | None, str | list[str] | None], operation: str
    ) -> tuple[datetime, datetime]:
        day = date.fromisoformat(
            require_string(
                row.get("Activity_StartDate"),
                provider=SAMPLES_PROVIDER,
                operation=operation,
            )
        )
        raw_time = row.get("Activity_StartTime")
        raw_zone = row.get("Activity_StartTimeZone")
        if not raw_time:
            start = datetime.combine(day, time.min, tzinfo=UTC)
            return start, start + timedelta(days=1)
        if (
            not isinstance(raw_time, str)
            or not isinstance(raw_zone, str)
            or not raw_zone.strip()
        ):
            raise invalid_response(SAMPLES_PROVIDER, operation)
        offset = _SAMPLE_TIMEZONE_OFFSETS.get(raw_zone.upper(), raw_zone)
        if len(offset) != 6 or offset[0] not in "+-" or offset[3] != ":":
            raise invalid_response(SAMPLES_PROVIDER, operation)
        sign = 1 if offset[0] == "+" else -1
        hours, minutes = int(offset[1:3]), int(offset[4:6])
        local = datetime.combine(
            day,
            time.fromisoformat(raw_time),
            tzinfo=timezone(sign * timedelta(hours=hours, minutes=minutes)),
        )
        observed = local.astimezone(UTC)
        return observed, observed + timedelta(microseconds=1)

    @staticmethod
    def _date_without_zone_bounds(day: date) -> tuple[datetime, datetime]:
        # A date without a time zone can straddle UTC days. Bound all possible
        # local-day offsets so short result windows never get a false hit.
        start = datetime.combine(day, time.min, tzinfo=UTC)
        return start - timedelta(days=1), start + timedelta(days=2)

    @staticmethod
    def _validate_probe_windows(
        requested: tuple[AvailabilityTimeRange, ...],
    ) -> None:
        previous: AvailabilityTimeRange | None = None
        for item in requested:
            if item.window_index is None:
                raise ValueError("observation probe windows require window_index")
            if previous is not None and (
                item.window_index <= previous.window_index or item.start < previous.end
            ):
                raise ValueError(
                    "observation probe windows must be ordered and non-overlapping"
                )
            previous = item

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")

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
                headers=self._headers("application/geo+json"),
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
            url = next_url
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
        query_names = {name.casefold() for name, _ in parse_qsl(target.query)}
        if (
            target.scheme != "https"
            or target.netloc != base.netloc
            or not target.path.startswith(base.path.rstrip("/") + "/")
            or target.fragment
            or {"api_key", "apikey", "key", "token"} & query_names
        ):
            raise invalid_response(PROVIDER, operation)

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {"Accept": accept}
        if self._api_key:
            headers["X-Api-Key"] = self._api_key
        return headers

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

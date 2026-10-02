from __future__ import annotations

import asyncio
import csv
import json
from collections.abc import Callable, Mapping
from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
from io import StringIO
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import ValidationError

from app.domain import (
    CollectionCompletionContract,
    CollectionCompletionProof,
    CollectionCounts,
    CollectionPageManifest,
    DischargeObservationRecord,
    EvidenceLocation,
    GeoPoint,
    ParameterKey,
    ParameterProvider,
    Provenance,
    ProviderCollectionBatch,
    ProviderQuality,
    TimeRange,
    WaterObservationRecord,
    WaterObservationRequest,
)
from app.infrastructure.adapters._parsing import (
    invalid_response,
    require_list,
    require_mapping,
    require_string,
)
from app.infrastructure.http import ProviderHttpClient
from app.ports import (
    InvalidPortResponseError,
    PortAuthenticationError,
    PortConfigurationError,
    PortError,
    PortHttpError,
    PortRateLimitError,
    PortResponseLimitError,
    PortTimeoutError,
    PortUnavailableError,
    UnsupportedPortVersionError,
)

WDFN_PROVIDER = "usgs_waterdata"
SAMPLES_PROVIDER = "usgs_samples"
DEFAULT_OGC_BASE_URL = "https://api.waterdata.usgs.gov/ogcapi/v1"
DEFAULT_SAMPLES_BASE_URL = "https://api.waterdata.usgs.gov/samples-data"
DISCHARGE = ParameterKey(code_system="usgs", code="00060")

_SAMPLE_COLUMNS = {
    "Org_Identifier",
    "Location_Identifier",
    "Activity_ActivityIdentifier",
    "Activity_Media",
    "Activity_StartDate",
    "Activity_StartTime",
    "Activity_StartTimeZone",
    "Result_MeasureIdentifier",
    "Result_ResultDetectionCondition",
    "Result_Characteristic",
    "Result_SampleFraction",
    "Result_Measure",
    "Result_MeasureUnit",
    "Result_MeasureQualifierCode",
    "Result_MeasureStatusIdentifier",
    "Result_StatisticalBase",
    "Result_MeasureType",
    "ResultAnalyticalMethod_Name",
    "USGSpcode",
}
_UTC_OFFSETS = {
    "Z": "+00:00",
    "UTC": "+00:00",
    "GMT": "+00:00",
    "EST": "-05:00",
    "EDT": "-04:00",
    "CST": "-06:00",
    "CDT": "-05:00",
    "MST": "-07:00",
    "MDT": "-06:00",
    "PST": "-08:00",
    "PDT": "-07:00",
    "AKST": "-09:00",
    "AKDT": "-08:00",
    "HST": "-10:00",
}

ObservationRecord = WaterObservationRecord | DischargeObservationRecord


class UsgsObservationAdapter:
    """Collect current USGS OGC observations and Samples narrow CSV records."""

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
        max_records: int = 10_000,
        discharge_parameters: tuple[ParameterKey, ...] = (DISCHARGE,),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if api_version != "v1":
            raise UnsupportedPortVersionError(
                "USGS Water Data API version is unsupported.",
                provider=WDFN_PROVIDER,
                operation="configure",
            )
        if page_size <= 0 or max_pages <= 0 or max_records <= 0:
            raise ValueError("USGS observation limits must be positive")
        if page_size > max_records:
            raise ValueError("page_size must not exceed max_records")
        self._require_https_base(ogc_base_url, "ogc_base_url")
        self._require_https_base(samples_base_url, "samples_base_url")
        discharge = frozenset(discharge_parameters)
        if not discharge or any(not self._is_usgs_key(item) for item in discharge):
            raise ValueError("discharge_parameters must contain USGS parameter keys")
        self._http = http
        self._api_key = api_key.strip() if api_key else None
        self._ogc_base_url = ogc_base_url.rstrip("/")
        self._samples_base_url = samples_base_url.rstrip("/")
        self._page_size = page_size
        self._max_pages = max_pages
        self._max_records = max_records
        self._discharge_parameters = discharge
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def collect(
        self,
        request: WaterObservationRequest,
        *,
        deadline: float | None = None,
    ) -> ProviderCollectionBatch[ObservationRecord]:
        self._validate_request(request)
        if request.provider is ParameterProvider.USGS_SAMPLES:
            return await self._collect_samples(request, deadline=deadline)
        return await self._collect_ogc(request, deadline=deadline)

    async def _collect_ogc(
        self,
        request: WaterObservationRequest,
        *,
        deadline: float | None,
    ) -> ProviderCollectionBatch[ObservationRecord]:
        operation = request.operation_id
        collection_url = (
            f"{self._ogc_base_url}/collections/{request.channel}/items"
        )
        params: dict[str, str | int] = {
            "monitoring_location_id": request.station_id,
            "time_series_id": ",".join(sorted(request.series_refs)),
            "parameter_code": ",".join(self._parameter_codes(request)),
            "datetime": self._interval(request.request_range),
            "f": "json",
            "limit": self._page_size,
        }
        url = collection_url
        page_params: Mapping[str, str | int] | None = params
        records: list[ObservationRecord] = []
        by_id: dict[str, ObservationRecord] = {}
        pages: list[CollectionPageManifest] = []
        visited: set[str] = set()
        request_count = 0
        retrieved_at = self._retrieved_at()
        request_identity = self._request_identity(collection_url, params)

        def count_attempt() -> None:
            nonlocal request_count
            request_count += 1

        for page_number in range(self._max_pages):
            await asyncio.sleep(0)
            page_identity = self._request_identity(url, page_params)
            if page_identity in visited:
                error = invalid_response(WDFN_PROVIDER, operation)
                return self._partial_or_raise(
                    error,
                    request=request,
                    records=records,
                    pages=pages,
                    request_count=request_count,
                    failed_page_identity=page_identity,
                    failed_page_bytes=0,
                    contract=CollectionCompletionContract.OFFICIAL_NEXT_CHAIN,
                )
            visited.add(page_identity)
            try:
                content = await self._http.get_bytes(
                    url,
                    provider=WDFN_PROVIDER,
                    operation=operation,
                    params=page_params,
                    headers=self._headers("application/geo+json"),
                    deadline=deadline,
                    on_attempt=count_attempt,
                )
            except PortError as error:
                return self._partial_or_raise(
                    error,
                    request=request,
                    records=records,
                    pages=pages,
                    request_count=request_count,
                    failed_page_identity=page_identity,
                    failed_page_bytes=0,
                    contract=CollectionCompletionContract.OFFICIAL_NEXT_CHAIN,
                )
            try:
                page_records, next_url = self._parse_ogc_page(
                    content,
                    request=request,
                    page_identity=page_identity,
                    request_identity=request_identity,
                    retrieved_at=retrieved_at,
                    collection_url=collection_url,
                )
                accepted = self._accept_page_records(
                    page_records,
                    by_id=by_id,
                    current_count=len(records),
                    operation=operation,
                )
            except PortError as error:
                return self._partial_or_raise(
                    error,
                    request=request,
                    records=records,
                    pages=pages,
                    request_count=request_count,
                    failed_page_identity=page_identity,
                    failed_page_bytes=len(content),
                    contract=CollectionCompletionContract.OFFICIAL_NEXT_CHAIN,
                )
            records.extend(accepted)
            next_identity = (
                self._request_identity(next_url, None) if next_url else None
            )
            pages.append(
                CollectionPageManifest(
                    page_identity=page_identity,
                    record_count=len(accepted),
                    response_bytes=len(content),
                    page_complete=True,
                    next_page_identity=next_identity,
                )
            )
            if next_url is None:
                return self._batch(
                    request=request,
                    records=records,
                    pages=pages,
                    request_count=request_count,
                    complete=True,
                    completed_ranges=(request.request_range,),
                    contract=CollectionCompletionContract.OFFICIAL_NEXT_CHAIN,
                )
            try:
                self._validate_next_url(next_url, collection_url, operation)
            except PortError as error:
                if len(pages) == 1:
                    raise
                return self._batch(
                    request=request,
                    records=records,
                    pages=pages,
                    request_count=request_count,
                    complete=False,
                    completed_ranges=(),
                    contract=CollectionCompletionContract.OFFICIAL_NEXT_CHAIN,
                    incomplete_reason=self._error_code(error),
                )
            if page_number + 1 == self._max_pages:
                return self._batch(
                    request=request,
                    records=records,
                    pages=pages,
                    request_count=request_count,
                    complete=False,
                    completed_ranges=(),
                    contract=CollectionCompletionContract.OFFICIAL_NEXT_CHAIN,
                    incomplete_reason="evidence_provider_response_too_large",
                )
            url = next_url
            page_params = None
        raise AssertionError("bounded USGS page loop did not terminate")

    async def _collect_samples(
        self,
        request: WaterObservationRequest,
        *,
        deadline: float | None,
    ) -> ProviderCollectionBatch[ObservationRecord]:
        operation = request.operation_id
        url = f"{self._samples_base_url}/results/narrow"
        upper_date = (request.request_range.end - timedelta(microseconds=1)).date()
        params: dict[str, str | tuple[str, ...]] = {
            "mimeType": "text/csv",
            "activityMediaName": "Water",
            "monitoringLocationIdentifier": request.station_id,
            "usgsPCode": self._parameter_codes(request),
            "activityStartDateLower": request.request_range.start.date().isoformat(),
            "activityStartDateUpper": upper_date.isoformat(),
        }
        request_count = 0

        def count_attempt() -> None:
            nonlocal request_count
            request_count += 1

        content = await self._http.get_bytes(
            url,
            provider=SAMPLES_PROVIDER,
            operation=operation,
            params=params,
            headers=self._headers("text/csv"),
            deadline=deadline,
            on_attempt=count_attempt,
        )
        page_identity = self._request_identity(url, params)
        request_identity = page_identity
        records = self._parse_samples_csv(
            content,
            request=request,
            page_identity=page_identity,
            request_identity=request_identity,
            retrieved_at=self._retrieved_at(),
            source_uri=url,
        )
        return self._batch(
            request=request,
            records=list(records),
            pages=[
                CollectionPageManifest(
                    page_identity=page_identity,
                    record_count=len(records),
                    response_bytes=len(content),
                    page_complete=True,
                )
            ],
            request_count=request_count,
            complete=True,
            completed_ranges=(request.request_range,),
            contract=CollectionCompletionContract.COMPLETE_CSV_STREAM,
        )

    def _parse_ogc_page(
        self,
        content: bytes,
        *,
        request: WaterObservationRequest,
        page_identity: str,
        request_identity: str,
        retrieved_at: datetime,
        collection_url: str,
    ) -> tuple[tuple[ObservationRecord, ...], str | None]:
        operation = request.operation_id
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise invalid_response(WDFN_PROVIDER, operation) from error
        root = require_mapping(payload, provider=WDFN_PROVIDER, operation=operation)
        if root.get("type") != "FeatureCollection":
            raise invalid_response(WDFN_PROVIDER, operation)
        features = require_list(
            root.get("features"), provider=WDFN_PROVIDER, operation=operation
        )
        parsed: list[ObservationRecord] = []
        for feature in features:
            parsed.append(
                self._ogc_record(
                    require_mapping(
                        feature, provider=WDFN_PROVIDER, operation=operation
                    ),
                    request=request,
                    page_identity=page_identity,
                    request_identity=request_identity,
                    retrieved_at=retrieved_at,
                    source_uri=collection_url,
                )
            )
        return tuple(parsed), self._next_url(root, operation)

    def _ogc_record(
        self,
        feature: Mapping[str, object],
        *,
        request: WaterObservationRequest,
        page_identity: str,
        request_identity: str,
        retrieved_at: datetime,
        source_uri: str,
    ) -> ObservationRecord:
        operation = request.operation_id
        if feature.get("type") != "Feature":
            raise invalid_response(WDFN_PROVIDER, operation)
        properties = require_mapping(
            feature.get("properties"), provider=WDFN_PROVIDER, operation=operation
        )
        feature_id = require_string(
            feature.get("id"), provider=WDFN_PROVIDER, operation=operation
        )
        station = require_string(
            properties.get("monitoring_location_id"),
            provider=WDFN_PROVIDER,
            operation=operation,
        )
        series = require_string(
            properties.get("time_series_id"),
            provider=WDFN_PROVIDER,
            operation=operation,
        )
        parameter = ParameterKey(
            code_system="usgs",
            code=self._usgs_code(
                properties.get("parameter_code"),
                provider=WDFN_PROVIDER,
                operation=operation,
            ),
        )
        if (
            station != request.station_id
            or series not in request.series_refs
            or parameter not in request.parameter_keys
        ):
            raise invalid_response(WDFN_PROVIDER, operation)
        observed_time = self._utc_datetime(properties.get("time"), operation)
        if not request.request_range.contains(observed_time):
            raise invalid_response(WDFN_PROVIDER, operation)
        approval = self._optional_string(properties.get("approval_status"))
        qualifier = self._optional_string(properties.get("qualifier"))
        method = self._optional_string(properties.get("method_category"))
        provider_record_id = (
            f"usgs-wdfn:v1:{request.channel}:{feature_id}"
        )
        record_type = self._record_type(parameter)
        try:
            return record_type(
                provider=WDFN_PROVIDER,
                channel=request.channel,
                source_uri=source_uri,
                retrieved_at=retrieved_at,
                observed_time=observed_time,
                location=EvidenceLocation(
                    point=self._optional_point(feature.get("geometry"), operation),
                    comid=None,
                    station_id=station,
                    distance_to_target_km=None,
                ),
                provider_quality=ProviderQuality(
                    approval=approval,
                    qualifiers=(qualifier,) if qualifier else (),
                ),
                raw_unit=require_string(
                    properties.get("unit_of_measure"),
                    provider=WDFN_PROVIDER,
                    operation=operation,
                ),
                provenance=Provenance(
                    operation_id=operation,
                    request_identity=request_identity,
                    page_identity=page_identity,
                    provider_series_id=series,
                    provider_record_id=provider_record_id,
                    source_timezone="UTC",
                    source_utc_offset="+00:00",
                ),
                parameter=parameter,
                value=require_string(
                    properties.get("value"),
                    provider=WDFN_PROVIDER,
                    operation=operation,
                ),
                medium="surface water",
                fraction=None,
                method=method,
                statistic=require_string(
                    properties.get("statistic_id"),
                    provider=WDFN_PROVIDER,
                    operation=operation,
                ),
            )
        except (ValidationError, TypeError, ValueError) as error:
            raise invalid_response(WDFN_PROVIDER, operation) from error

    def _parse_samples_csv(
        self,
        content: bytes,
        *,
        request: WaterObservationRequest,
        page_identity: str,
        request_identity: str,
        retrieved_at: datetime,
        source_uri: str,
    ) -> tuple[ObservationRecord, ...]:
        operation = request.operation_id
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise invalid_response(SAMPLES_PROVIDER, operation) from error
        reader = csv.DictReader(StringIO(text, newline=""))
        if reader.fieldnames is None or not _SAMPLE_COLUMNS.issubset(
            set(reader.fieldnames)
        ):
            raise invalid_response(SAMPLES_PROVIDER, operation)
        records: list[ObservationRecord] = []
        by_id: dict[str, ObservationRecord] = {}
        try:
            for row in reader:
                if None in row:
                    raise invalid_response(SAMPLES_PROVIDER, operation)
                record = self._sample_record(
                    row,
                    request=request,
                    page_identity=page_identity,
                    request_identity=request_identity,
                    retrieved_at=retrieved_at,
                    source_uri=source_uri,
                )
                if not self._record_overlaps(record, request.request_range):
                    continue
                previous = by_id.get(record.record_id)
                if previous is not None:
                    if not self._same_record(previous, record):
                        raise invalid_response(SAMPLES_PROVIDER, operation)
                    continue
                if len(records) >= self._max_records:
                    raise PortResponseLimitError(
                        "USGS Samples response exceeded the configured record limit.",
                        provider=SAMPLES_PROVIDER,
                        operation=operation,
                    )
                by_id[record.record_id] = record
                records.append(record)
        except PortError:
            raise
        except (ValidationError, TypeError, ValueError, csv.Error) as error:
            raise invalid_response(SAMPLES_PROVIDER, operation) from error
        return tuple(records)

    def _sample_record(
        self,
        row: Mapping[str | None, str | list[str] | None],
        *,
        request: WaterObservationRequest,
        page_identity: str,
        request_identity: str,
        retrieved_at: datetime,
        source_uri: str,
    ) -> ObservationRecord:
        operation = request.operation_id
        station = require_string(
            row.get("Location_Identifier"),
            provider=SAMPLES_PROVIDER,
            operation=operation,
        )
        parameter = ParameterKey(
            code_system="usgs",
            code=self._usgs_code(
                row.get("USGSpcode"),
                provider=SAMPLES_PROVIDER,
                operation=operation,
            ),
        )
        if station != request.station_id or parameter not in request.parameter_keys:
            raise invalid_response(SAMPLES_PROVIDER, operation)
        observed_time, valid_range, source_timezone, source_offset = (
            self._sample_time(row, operation)
        )
        result_id = require_string(
            row.get("Result_MeasureIdentifier"),
            provider=SAMPLES_PROVIDER,
            operation=operation,
        )
        organization = require_string(
            row.get("Org_Identifier"),
            provider=SAMPLES_PROVIDER,
            operation=operation,
        )
        activity = require_string(
            row.get("Activity_ActivityIdentifier"),
            provider=SAMPLES_PROVIDER,
            operation=operation,
        )
        provider_record_id = (
            f"usgs-samples:v1:{organization}:{activity}:{result_id}"
        )
        qualifiers = self._unique_nonempty(
            row.get("Result_MeasureQualifierCode"),
            row.get("Result_ResultDetectionCondition"),
            row.get("Result_MeasureType"),
        )
        statistic = self._optional_string(row.get("Result_StatisticalBase"))
        method = self._optional_string(row.get("ResultAnalyticalMethod_Name"))
        point = self._sample_point(row, operation)
        record_type = self._record_type(parameter)
        try:
            return record_type(
                provider=SAMPLES_PROVIDER,
                channel=request.channel,
                source_uri=source_uri,
                retrieved_at=retrieved_at,
                observed_time=observed_time,
                valid_time_range=valid_range,
                location=EvidenceLocation(
                    point=point,
                    comid=None,
                    station_id=station,
                    distance_to_target_km=None,
                ),
                provider_quality=ProviderQuality(
                    approval=self._optional_string(
                        row.get("Result_MeasureStatusIdentifier")
                    ),
                    qualifiers=qualifiers,
                ),
                raw_unit=require_string(
                    row.get("Result_MeasureUnit"),
                    provider=SAMPLES_PROVIDER,
                    operation=operation,
                ),
                provenance=Provenance(
                    operation_id=operation,
                    request_identity=request_identity,
                    page_identity=page_identity,
                    provider_series_id=activity,
                    provider_record_id=provider_record_id,
                    source_timezone=source_timezone,
                    source_utc_offset=source_offset,
                ),
                parameter=parameter,
                value=require_string(
                    row.get("Result_Measure"),
                    provider=SAMPLES_PROVIDER,
                    operation=operation,
                ),
                medium=require_string(
                    row.get("Activity_Media"),
                    provider=SAMPLES_PROVIDER,
                    operation=operation,
                ),
                fraction=self._optional_string(row.get("Result_SampleFraction")),
                method=method,
                statistic=statistic or "sample",
            )
        except (ValidationError, TypeError, ValueError) as error:
            raise invalid_response(SAMPLES_PROVIDER, operation) from error

    def _sample_time(
        self,
        row: Mapping[str | None, str | list[str] | None],
        operation: str,
    ) -> tuple[datetime | None, TimeRange | None, str | None, str | None]:
        day = date.fromisoformat(
            require_string(
                row.get("Activity_StartDate"),
                provider=SAMPLES_PROVIDER,
                operation=operation,
            )
        )
        raw_time = self._optional_string(row.get("Activity_StartTime"))
        raw_zone = self._optional_string(row.get("Activity_StartTimeZone"))
        if raw_time is None:
            start = datetime.combine(day, time.min, tzinfo=timezone.utc)
            return None, TimeRange(start=start, end=start + timedelta(days=1)), None, None
        if raw_zone is None:
            raise invalid_response(SAMPLES_PROVIDER, operation)
        offset_text = _UTC_OFFSETS.get(raw_zone.upper(), raw_zone)
        try:
            if offset_text == "+00:00":
                tz = timezone.utc
            else:
                sign = 1 if offset_text.startswith("+") else -1
                hours, minutes = (int(part) for part in offset_text[1:].split(":"))
                tz = timezone(sign * timedelta(hours=hours, minutes=minutes))
            local = datetime.combine(day, time.fromisoformat(raw_time), tzinfo=tz)
        except (TypeError, ValueError) as error:
            raise invalid_response(SAMPLES_PROVIDER, operation) from error
        return local.astimezone(timezone.utc), None, raw_zone, offset_text

    def _sample_point(
        self,
        row: Mapping[str | None, str | list[str] | None],
        operation: str,
    ) -> GeoPoint | None:
        latitude = self._optional_string(row.get("Location_Latitude"))
        longitude = self._optional_string(row.get("Location_Longitude"))
        if latitude is None and longitude is None:
            return None
        if latitude is None or longitude is None:
            raise invalid_response(SAMPLES_PROVIDER, operation)
        try:
            return GeoPoint(latitude=float(latitude), longitude=float(longitude))
        except (ValidationError, TypeError, ValueError) as error:
            raise invalid_response(SAMPLES_PROVIDER, operation) from error

    def _accept_page_records(
        self,
        page_records: tuple[ObservationRecord, ...],
        *,
        by_id: dict[str, ObservationRecord],
        current_count: int,
        operation: str,
    ) -> tuple[ObservationRecord, ...]:
        accepted: list[ObservationRecord] = []
        for record in page_records:
            previous = by_id.get(record.record_id)
            if previous is not None:
                if not self._same_record(previous, record):
                    raise invalid_response(WDFN_PROVIDER, operation)
                continue
            if current_count + len(accepted) >= self._max_records:
                raise PortResponseLimitError(
                    "USGS observations exceeded the configured record limit.",
                    provider=WDFN_PROVIDER,
                    operation=operation,
                )
            by_id[record.record_id] = record
            accepted.append(record)
        return tuple(accepted)

    def _partial_or_raise(
        self,
        error: PortError,
        *,
        request: WaterObservationRequest,
        records: list[ObservationRecord],
        pages: list[CollectionPageManifest],
        request_count: int,
        failed_page_identity: str,
        failed_page_bytes: int,
        contract: CollectionCompletionContract,
    ) -> ProviderCollectionBatch[ObservationRecord]:
        if not pages:
            raise error
        pages.append(
            CollectionPageManifest(
                page_identity=failed_page_identity,
                record_count=0,
                response_bytes=failed_page_bytes,
                page_complete=False,
            )
        )
        return self._batch(
            request=request,
            records=records,
            pages=pages,
            request_count=request_count,
            complete=False,
            completed_ranges=(),
            contract=contract,
            incomplete_reason=self._error_code(error),
        )

    @staticmethod
    def _batch(
        *,
        request: WaterObservationRequest,
        records: list[ObservationRecord],
        pages: list[CollectionPageManifest],
        request_count: int,
        complete: bool,
        completed_ranges: tuple[TimeRange, ...],
        contract: CollectionCompletionContract,
        incomplete_reason: str | None = None,
    ) -> ProviderCollectionBatch[ObservationRecord]:
        terminal = pages[-1].page_identity if complete else None
        proof_payload = {
            "contract": contract.value,
            "complete": complete,
            "terminal_identity": terminal,
            "pages": [page.model_dump(mode="json") for page in pages],
        }
        digest = "sha256:" + sha256(
            json.dumps(
                proof_payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return ProviderCollectionBatch[ObservationRecord](
            operation_id=request.operation_id,
            records=tuple(records),
            page_or_chunk_manifests=tuple(pages),
            completed_ranges=completed_ranges,
            counts=CollectionCounts(
                requests=request_count,
                pages=len(pages),
                records=len(records),
                response_bytes=sum(page.response_bytes for page in pages),
            ),
            completion_proof=CollectionCompletionProof(
                contract=contract,
                complete=complete,
                terminal_identity=terminal,
                digest=digest,
            ),
            incomplete_reason=incomplete_reason,
        )

    def _next_url(
        self, root: Mapping[str, object], operation: str
    ) -> str | None:
        links = require_list(
            root.get("links", []), provider=WDFN_PROVIDER, operation=operation
        )
        next_links: list[str] = []
        for raw in links:
            link = require_mapping(raw, provider=WDFN_PROVIDER, operation=operation)
            if link.get("rel") == "next":
                next_links.append(
                    require_string(
                        link.get("href"),
                        provider=WDFN_PROVIDER,
                        operation=operation,
                    )
                )
        if len(next_links) > 1:
            raise invalid_response(WDFN_PROVIDER, operation)
        return next_links[0] if next_links else None

    @staticmethod
    def _validate_next_url(url: str, collection_url: str, operation: str) -> None:
        target = urlsplit(url)
        collection = urlsplit(collection_url)
        query_names = {name.casefold() for name, _ in parse_qsl(target.query)}
        if (
            target.scheme != "https"
            or target.netloc != collection.netloc
            or target.path != collection.path
            or target.fragment
            or {"api_key", "apikey", "key", "token"} & query_names
        ):
            raise invalid_response(WDFN_PROVIDER, operation)

    def _validate_request(self, request: WaterObservationRequest) -> None:
        if request.provider is ParameterProvider.WDFN_TIME_SERIES:
            if request.channel not in {"continuous", "daily"}:
                raise ValueError("USGS WDFN requires continuous or daily channel")
        elif request.provider is ParameterProvider.USGS_SAMPLES:
            if request.channel != "discrete_samples":
                raise ValueError("USGS Samples requires discrete_samples channel")
        else:
            raise ValueError("USGS observation adapter received another provider")
        if "," in request.station_id or any("," in item for item in request.series_refs):
            raise ValueError("USGS identities must not contain commas")
        if any(not self._is_usgs_key(item) for item in request.parameter_keys):
            raise ValueError("USGS observations require five-digit USGS parameters")

    def _record_type(
        self, parameter: ParameterKey
    ) -> type[WaterObservationRecord] | type[DischargeObservationRecord]:
        if parameter in self._discharge_parameters:
            return DischargeObservationRecord
        return WaterObservationRecord

    @staticmethod
    def _is_usgs_key(parameter: ParameterKey) -> bool:
        return (
            parameter.code_system == "usgs"
            and len(parameter.code) == 5
            and parameter.code.isdecimal()
        )

    @staticmethod
    def _parameter_codes(request: WaterObservationRequest) -> tuple[str, ...]:
        return tuple(sorted(parameter.code for parameter in request.parameter_keys))

    @staticmethod
    def _interval(value: TimeRange) -> str:
        return f"{UsgsObservationAdapter._iso(value.start)}/{UsgsObservationAdapter._iso(value.end)}"

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {"Accept": accept}
        if self._api_key:
            headers["X-Api-Key"] = self._api_key
        return headers

    def _retrieved_at(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("USGS adapter clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _request_identity(
        url: str, params: Mapping[str, object] | None
    ) -> str:
        parsed = urlsplit(url)
        pairs = list(parse_qsl(parsed.query, keep_blank_values=True))
        if params:
            for name, raw in params.items():
                values = raw if isinstance(raw, tuple) else (raw,)
                pairs.extend((name, str(value)) for value in values)
        safe_pairs = [
            (name, value)
            for name, value in pairs
            if name.casefold() not in {"api_key", "apikey", "key", "token"}
        ]
        canonical = urlunsplit(
            (
                parsed.scheme.casefold(),
                parsed.netloc.casefold(),
                parsed.path,
                urlencode(sorted(safe_pairs)),
                "",
            )
        )
        return f"sha256:{sha256(canonical.encode('utf-8')).hexdigest()}"

    @staticmethod
    def _utc_datetime(value: object, operation: str) -> datetime:
        text = require_string(value, provider=WDFN_PROVIDER, operation=operation)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as error:
            raise invalid_response(WDFN_PROVIDER, operation) from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise invalid_response(WDFN_PROVIDER, operation)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _optional_point(value: object, operation: str) -> GeoPoint | None:
        if value is None:
            return None
        geometry = require_mapping(value, provider=WDFN_PROVIDER, operation=operation)
        if geometry.get("type") != "Point":
            raise invalid_response(WDFN_PROVIDER, operation)
        coordinates = require_list(
            geometry.get("coordinates"),
            provider=WDFN_PROVIDER,
            operation=operation,
        )
        if len(coordinates) < 2:
            raise invalid_response(WDFN_PROVIDER, operation)
        try:
            if isinstance(coordinates[0], bool) or isinstance(coordinates[1], bool):
                raise ValueError
            return GeoPoint(
                latitude=float(coordinates[1]), longitude=float(coordinates[0])
            )
        except (ValidationError, TypeError, ValueError) as error:
            raise invalid_response(WDFN_PROVIDER, operation) from error

    @staticmethod
    def _optional_string(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("optional provider value must be text")
        stripped = value.strip()
        return stripped or None

    @staticmethod
    def _unique_nonempty(*values: object) -> tuple[str, ...]:
        result: list[str] = []
        for value in values:
            text = UsgsObservationAdapter._optional_string(value)
            if text and text not in result:
                result.append(text)
        return tuple(result)

    @staticmethod
    def _usgs_code(
        value: object,
        *,
        provider: str,
        operation: str,
    ) -> str:
        code = require_string(
            value,
            provider=provider,
            operation=operation,
        )
        if len(code) != 5 or not code.isdecimal():
            raise invalid_response(provider, operation)
        return code

    @staticmethod
    def _record_overlaps(record: ObservationRecord, scope: TimeRange) -> bool:
        if record.observed_time is not None:
            return scope.contains(record.observed_time)
        assert record.valid_time_range is not None
        return scope.overlaps(record.valid_time_range)

    @staticmethod
    def _same_record(first: ObservationRecord, second: ObservationRecord) -> bool:
        first_value = first.model_dump(mode="json")
        second_value = second.model_dump(mode="json")
        for value in (first_value, second_value):
            provenance = value.get("provenance")
            if isinstance(provenance, dict):
                provenance.pop("page_identity", None)
        return first_value == second_value

    @staticmethod
    def _error_code(error: PortError) -> str:
        if isinstance(error, PortAuthenticationError):
            return "evidence_provider_authentication_failed"
        if isinstance(error, PortRateLimitError):
            return "evidence_provider_rate_limited"
        if isinstance(error, PortTimeoutError):
            return "evidence_provider_timeout"
        if isinstance(error, PortResponseLimitError):
            return "evidence_provider_response_too_large"
        if isinstance(error, (InvalidPortResponseError, UnsupportedPortVersionError)):
            return "evidence_provider_response_invalid"
        if isinstance(
            error,
            (PortUnavailableError, PortHttpError, PortConfigurationError),
        ):
            return "evidence_provider_unavailable"
        return "evidence_provider_unavailable"

    @staticmethod
    def _require_https_base(value: str, field_name: str) -> None:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError(f"{field_name} must be an HTTPS base URL")

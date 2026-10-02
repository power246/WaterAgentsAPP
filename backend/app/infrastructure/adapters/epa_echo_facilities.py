from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from math import ceil
from urllib.parse import urlencode, urlsplit, urlunsplit

from pydantic import ValidationError

from app.domain import (
    CollectionCompletionContract,
    CollectionPageManifest,
    EvidenceLocation,
    FacilityEvidenceRequest,
    FacilityRecord,
    GeoPoint,
    Provenance,
    ProviderCollectionBatch,
    ProviderQuality,
    WaterbodyGeometry,
    point_to_linestring_distance_km,
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
from app.ports import PortConfigurationError, PortError, PortResponseLimitError

PROVIDER = "epa_echo"
DEFAULT_BASE_URL = "https://echodata.epa.gov/echo"
KM_PER_MILE = Decimal("1.609344")


class EpaEchoFacilityAdapter:
    """Bounded CWA/NPDES facility search over a trusted corridor geometry."""

    def __init__(
        self,
        http: ProviderHttpClient,
        *,
        geometry_resolver: Callable[[str], WaterbodyGeometry | None],
        base_url: str = DEFAULT_BASE_URL,
        response_set: int = 500,
        max_pages: int = 10,
        max_records: int = 10_000,
        clock=None,
    ) -> None:
        if not 1 <= response_set <= 1000:
            raise ValueError("EPA ECHO responseset must be between 1 and 1000")
        if max_pages <= 0 or max_records <= 0:
            raise ValueError("EPA ECHO collection limits must be positive")
        parsed = urlsplit(base_url.rstrip("/"))
        if (
            parsed.scheme != "https"
            or parsed.hostname != "echodata.epa.gov"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "EPA ECHO base_url must be the echodata.epa.gov HTTPS origin"
            )
        self._http = http
        self._geometry_resolver = geometry_resolver
        self._base_url = base_url.rstrip("/")
        self._response_set = response_set
        self._max_pages = max_pages
        self._max_records = max_records
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def collect(
        self,
        request: FacilityEvidenceRequest,
        *,
        deadline: float | None = None,
    ) -> ProviderCollectionBatch[FacilityRecord]:
        geometry = self._geometry_resolver(request.geometry_ref)
        if geometry is None or geometry.geometry_ref != request.geometry_ref:
            raise PortConfigurationError(
                "Trusted waterbody geometry is unavailable.",
                provider=PROVIDER,
                operation=request.operation_id,
            )
        await asyncio.sleep(0)
        raw_records: list[FacilityRecord] = []
        pages: list[CollectionPageManifest] = []
        request_count = 0
        pending_identity: str | None = None
        retrieved_at = self._retrieved_at()

        def count_attempt() -> None:
            nonlocal request_count
            request_count += 1

        try:
            for center in request.sample_centers:
                self._ensure_page_budget(len(pages), request.operation_id)
                center_start = len(raw_records)
                params: Mapping[str, object] = {
                    "output": "JSON",
                    "p_lat": center.latitude,
                    "p_long": center.longitude,
                    "p_radius": request.provider_radius_miles,
                    "responseset": self._response_set,
                    "queryset": self._max_records,
                }
                search_url = f"{self._base_url}/cwa_rest_services.get_facilities"
                pending_identity = request_identity(search_url, params)
                self._link_next(pages, pending_identity)
                root, size = await self._get(
                    search_url,
                    params=params,
                    operation=request.operation_id,
                    deadline=deadline,
                    on_attempt=count_attempt,
                )
                results = self._results(root, request.operation_id)
                query_id = require_string(
                    results.get("QueryID"),
                    provider=PROVIDER,
                    operation=request.operation_id,
                )
                total = self._integer(
                    results.get("QueryRows"), request.operation_id
                )
                if self._integer(results.get("PageNo"), request.operation_id) != 1:
                    raise invalid_response(PROVIDER, request.operation_id)
                if total > self._max_records:
                    raise PortResponseLimitError(
                        "EPA ECHO query exceeded the configured record limit.",
                        provider=PROVIDER,
                        operation=request.operation_id,
                    )
                expected_pages = max(1, ceil(total / self._response_set))
                if len(pages) + expected_pages > self._max_pages:
                    raise PortResponseLimitError(
                        "EPA ECHO query exceeded the configured page limit.",
                        provider=PROVIDER,
                        operation=request.operation_id,
                    )
                first = self._facility_records(
                    results,
                    request=request,
                    center=center,
                    page_identity=pending_identity,
                    source_uri=self._canonical_uri(search_url, params),
                    retrieved_at=retrieved_at,
                )
                raw_records.extend(first)
                if len(raw_records) > self._max_records:
                    raise PortResponseLimitError(
                        "EPA ECHO collection exceeded the configured record limit.",
                        provider=PROVIDER,
                        operation=request.operation_id,
                    )
                pages.append(
                    CollectionPageManifest(
                        page_identity=pending_identity,
                        record_count=0,
                        response_bytes=size,
                        page_complete=True,
                    )
                )
                for page_number in range(2, expected_pages + 1):
                    self._ensure_page_budget(len(pages), request.operation_id)
                    page_url = f"{self._base_url}/cwa_rest_services.get_qid"
                    page_params: Mapping[str, object] = {
                        "output": "JSON",
                        "qid": query_id,
                        "pageno": page_number,
                    }
                    pending_identity = request_identity(page_url, page_params)
                    self._link_next(pages, pending_identity)
                    page_root, page_size = await self._get(
                        page_url,
                        params=page_params,
                        operation=request.operation_id,
                        deadline=deadline,
                        on_attempt=count_attempt,
                    )
                    page_results = self._results(
                        page_root, request.operation_id
                    )
                    self._validate_page(
                        page_results,
                        query_id=query_id,
                        page_number=page_number,
                        operation=request.operation_id,
                    )
                    raw_records.extend(
                        self._facility_records(
                            page_results,
                            request=request,
                            center=center,
                            page_identity=pending_identity,
                            source_uri=self._canonical_uri(page_url, page_params),
                            retrieved_at=retrieved_at,
                        )
                    )
                    if len(raw_records) > self._max_records:
                        raise PortResponseLimitError(
                            "EPA ECHO collection exceeded the configured record limit.",
                            provider=PROVIDER,
                            operation=request.operation_id,
                        )
                    pages.append(
                        CollectionPageManifest(
                            page_identity=pending_identity,
                            record_count=0,
                            response_bytes=page_size,
                            page_complete=True,
                        )
                    )
                if len(raw_records) - center_start != total:
                    raise invalid_response(PROVIDER, request.operation_id)
        except PortError as error:
            if not pages:
                raise
            if pending_identity is None:
                raise
            if pages[-1].page_identity != pending_identity:
                self._link_next(pages, pending_identity)
                pages.append(
                    CollectionPageManifest(
                        page_identity=pending_identity,
                        record_count=0,
                        response_bytes=0,
                        page_complete=False,
                    )
                )
            records = self._finalize(raw_records, request=request, geometry=geometry)
            pages = self._recount(pages, records)
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

        records = self._finalize(raw_records, request=request, geometry=geometry)
        pages = self._recount(pages, records)
        return build_batch(
            operation_id=request.operation_id,
            records=records,
            pages=pages,
            request_count=request_count,
            complete=True,
            completed_ranges=(),
            contract=CollectionCompletionContract.OFFICIAL_NEXT_CHAIN,
        )

    def _ensure_page_budget(self, completed_pages: int, operation: str) -> None:
        if completed_pages >= self._max_pages:
            raise PortResponseLimitError(
                "EPA ECHO query exceeded the configured page limit.",
                provider=PROVIDER,
                operation=operation,
            )

    def _facility_records(
        self,
        results: Mapping[str, object],
        *,
        request: FacilityEvidenceRequest,
        center: GeoPoint,
        page_identity: str,
        source_uri: str,
        retrieved_at: datetime,
    ) -> tuple[FacilityRecord, ...]:
        facilities = require_list(
            results.get("Facilities", []),
            provider=PROVIDER,
            operation=request.operation_id,
        )
        parsed: list[FacilityRecord] = []
        for raw in facilities:
            item = require_mapping(
                raw, provider=PROVIDER, operation=request.operation_id
            )
            registry_id = require_string(
                item.get("RegistryID"),
                provider=PROVIDER,
                operation=request.operation_id,
            )
            facility_id = require_string(
                item.get("SourceID") or item.get("NPDESIDs"),
                provider=PROVIDER,
                operation=request.operation_id,
            )
            permit_id = self._optional_string(item.get("NPDESIDs")) or facility_id
            status = require_string(
                item.get("CWPPermitStatusDesc"),
                provider=PROVIDER,
                operation=request.operation_id,
            )
            statute = require_string(
                item.get("Statute"),
                provider=PROVIDER,
                operation=request.operation_id,
            )
            if statute != "CWA":
                raise invalid_response(PROVIDER, request.operation_id)
            point = self._optional_point(item, request.operation_id)
            qualifiers = ("missing_coordinates",) if point is None else ()
            identity = self._provider_id(registry_id, facility_id, permit_id)
            try:
                parsed.append(
                    FacilityRecord(
                        provider=PROVIDER,
                        channel="cwa_npdes_facility",
                        source_uri=source_uri,
                        retrieved_at=retrieved_at,
                        observed_time=retrieved_at,
                        valid_time_range=None,
                        location=EvidenceLocation(
                            point=point,
                            comid=None,
                            station_id=None,
                            provider_location_id=facility_id,
                            distance_to_target_km=None,
                        ),
                        provider_quality=ProviderQuality(
                            approval="official", qualifiers=qualifiers
                        ),
                        raw_unit=None,
                        provenance=Provenance(
                            operation_id=request.operation_id,
                            request_identity=page_identity,
                            page_identity=page_identity,
                            provider_record_id=identity,
                            facility_agent_radius_miles=str(
                                request.exact_radius_miles
                            ),
                            facility_provider_radius_miles=str(
                                request.provider_radius_miles
                            ),
                            facility_sample_centers=(center,),
                            facility_request_identities=(page_identity,),
                            facility_deduplicated_count=0,
                            facility_final_distance_km=None,
                        ),
                        spatially_in_scope=False,
                        registry_id=registry_id,
                        facility_id=facility_id,
                        permit_id=permit_id,
                        program="NPDES",
                        status=status,
                        public_attributes=self._public_attributes(item),
                    )
                )
            except (ValidationError, ValueError) as error:
                raise invalid_response(PROVIDER, request.operation_id) from error
        return tuple(parsed)

    def _finalize(
        self,
        records: list[FacilityRecord],
        *,
        request: FacilityEvidenceRequest,
        geometry: WaterbodyGeometry,
    ) -> tuple[FacilityRecord, ...]:
        deduplicated: dict[tuple[str, str, str], FacilityRecord] = {}
        for record in records:
            identity = (
                record.registry_id,
                record.facility_id,
                record.permit_id or "",
            )
            previous = deduplicated.get(identity)
            if previous is None:
                deduplicated[identity] = record
                continue
            if self._facility_facts(previous) != self._facility_facts(record):
                raise invalid_response(PROVIDER, request.operation_id)
            centers = self._unique(
                previous.provenance.facility_sample_centers
                + record.provenance.facility_sample_centers
            )
            identities = self._unique(
                previous.provenance.facility_request_identities
                + record.provenance.facility_request_identities
            )
            payload = previous.model_dump(mode="python")
            payload["provenance"] = previous.provenance.model_copy(
                update={
                    "facility_sample_centers": centers,
                    "facility_request_identities": identities,
                    "facility_deduplicated_count": (
                        previous.provenance.facility_deduplicated_count + 1
                    ),
                }
            ).model_dump(mode="python")
            payload["record_id"] = previous.record_id
            deduplicated[identity] = FacilityRecord.model_validate(payload)

        maximum_km = float(Decimal(str(request.exact_radius_miles)) * KM_PER_MILE)
        selected: list[FacilityRecord] = []
        for record in deduplicated.values():
            point = record.location.point
            if point is None:
                selected.append(record)
                continue
            distance = point_to_linestring_distance_km(
                point, geometry.full_coordinates
            )
            if distance > maximum_km + 1e-9:
                continue
            stable_distance = float(f"{distance:.9f}")
            payload = record.model_dump(mode="python")
            payload["spatially_in_scope"] = True
            payload["location"] = record.location.model_copy(
                update={"distance_to_target_km": stable_distance}
            ).model_dump(mode="python")
            payload["provenance"] = record.provenance.model_copy(
                update={"facility_final_distance_km": stable_distance}
            ).model_dump(mode="python")
            payload["record_id"] = record.record_id
            selected.append(FacilityRecord.model_validate(payload))
        return tuple(
            sorted(
                selected,
                key=lambda item: (
                    item.registry_id,
                    item.facility_id,
                    item.permit_id or "",
                ),
            )
        )

    @staticmethod
    def _recount(
        pages: list[CollectionPageManifest],
        records: tuple[FacilityRecord, ...],
    ) -> list[CollectionPageManifest]:
        counts: dict[str, int] = {}
        for record in records:
            identity = record.provenance.page_identity
            if identity is not None:
                counts[identity] = counts.get(identity, 0) + 1
        return [
            page.model_copy(update={"record_count": counts.get(page.page_identity, 0)})
            for page in pages
        ]

    async def _get(
        self,
        url: str,
        *,
        params: Mapping[str, object],
        operation: str,
        deadline: float | None,
        on_attempt,
    ) -> tuple[Mapping[str, object], int]:
        content = await self._http.get_bytes(
            url,
            provider=PROVIDER,
            operation=operation,
            params=params,
            headers={
                "Accept": "application/json",
                "User-Agent": "WaterAgents/0.1 EPA-ECHO-adapter",
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

    @staticmethod
    def _results(root: Mapping[str, object], operation: str) -> Mapping[str, object]:
        results = require_mapping(
            root.get("Results"), provider=PROVIDER, operation=operation
        )
        message = require_string(
            results.get("Message"), provider=PROVIDER, operation=operation
        )
        if not message.casefold().startswith("success"):
            raise invalid_response(PROVIDER, operation)
        return results

    @classmethod
    def _validate_page(
        cls,
        results: Mapping[str, object],
        *,
        query_id: str,
        page_number: int,
        operation: str,
    ) -> None:
        returned_query = results.get("QueryID")
        if returned_query is not None and str(returned_query) != query_id:
            raise invalid_response(PROVIDER, operation)
        if cls._integer(results.get("PageNo"), operation) != page_number:
            raise invalid_response(PROVIDER, operation)

    @staticmethod
    def _integer(value: object, operation: str) -> int:
        if isinstance(value, bool):
            raise invalid_response(PROVIDER, operation)
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError) as error:
            raise invalid_response(PROVIDER, operation) from error
        if not parsed.is_finite() or parsed < 0 or parsed != parsed.to_integral_value():
            raise invalid_response(PROVIDER, operation)
        return int(parsed)

    @staticmethod
    def _optional_point(
        item: Mapping[str, object], operation: str
    ) -> GeoPoint | None:
        latitude = EpaEchoFacilityAdapter._optional_string(item.get("FacLat"))
        longitude = EpaEchoFacilityAdapter._optional_string(item.get("FacLong"))
        if latitude is None and longitude is None:
            return None
        if latitude is None or longitude is None:
            raise invalid_response(PROVIDER, operation)
        try:
            return GeoPoint(latitude=float(latitude), longitude=float(longitude))
        except (ValidationError, ValueError) as error:
            raise invalid_response(PROVIDER, operation) from error

    @staticmethod
    def _public_attributes(
        item: Mapping[str, object],
    ) -> tuple[tuple[str, str], ...]:
        selected = (
            ("facility_name", "CWPName"),
            ("permit_name", "PermitName"),
            ("permit_type", "CWPPermitTypeDesc"),
            ("facility_type", "CWPFacilityTypeIndicator"),
            ("city", "CWPCity"),
            ("state", "CWPState"),
            ("receiving_waterbody", "CWPStateWaterBodyName"),
        )
        return tuple(
            (name, value)
            for name, provider_name in selected
            if (
                value := EpaEchoFacilityAdapter._optional_string(
                    item.get(provider_name)
                )
            )
            is not None
        )

    @staticmethod
    def _optional_string(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("optional EPA ECHO value must be text")
        stripped = value.strip()
        return stripped or None

    @staticmethod
    def _link_next(
        pages: list[CollectionPageManifest], next_identity: str
    ) -> None:
        if pages:
            pages[-1] = pages[-1].model_copy(
                update={"next_page_identity": next_identity}
            )

    @staticmethod
    def _facility_facts(record: FacilityRecord) -> tuple[object, ...]:
        return (
            record.location.point,
            record.status,
            record.program,
            record.public_attributes,
        )

    @staticmethod
    def _unique(values):
        result = []
        for value in values:
            if value not in result:
                result.append(value)
        return tuple(result)

    def _retrieved_at(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("EPA ECHO adapter clock must return an aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _provider_id(*values: str) -> str:
        return "sha256:" + sha256("\x1f".join(values).encode("utf-8")).hexdigest()

    @staticmethod
    def _canonical_uri(url: str, params: Mapping[str, object]) -> str:
        parsed = urlsplit(url)
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urlencode(sorted((name, str(value)) for name, value in params.items())),
                "",
            )
        )

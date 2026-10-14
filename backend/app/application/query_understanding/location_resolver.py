from __future__ import annotations

import re
from typing import Self

from pydantic import model_validator

from app.application.query_understanding.context_models import (
    CONUS_SUBDIVISION_CODES,
    ParsedIntentV1,
    ResolvedTarget,
    TargetSource,
)
from app.application.transcripts.models import AnalysisTargetV1
from app.domain import (
    AdministrativeLocation,
    DomainEnum,
    DomainModel,
    GeoPoint,
    GeocodingCandidate,
)
from app.ports import ComidLookupPort, GeocodingPort, TopologyDataPort


_COORDINATE_TEXT = re.compile(
    r"\s*(?:coordinates?\s*:\s*)?\(?\s*"
    r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*,\s*"
    r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*\)?\s*",
    re.IGNORECASE,
)


class LocationResolutionStatus(DomainEnum):
    SUCCESS = "success"
    LOCATION_REQUIRED = "location_required"
    LOCATION_AMBIGUOUS = "location_ambiguous"
    LOCATION_CONFLICT = "location_conflict"
    LOCATION_OUTSIDE_SUPPORTED_REGION = "location_outside_supported_region"
    LOCATION_NOT_SUPPORTED = "location_not_supported"
    TARGET_MISMATCH = "target_mismatch"


class LocationResolution(DomainModel):
    status: LocationResolutionStatus
    target: ResolvedTarget | None = None

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        succeeded = self.status is LocationResolutionStatus.SUCCESS
        if succeeded != (self.target is not None):
            raise ValueError("only successful location resolution may expose a target")
        return self


class LocationResolver:
    def __init__(
        self,
        geocoding: GeocodingPort,
        comid_lookup: ComidLookupPort,
        topology: TopologyDataPort,
    ) -> None:
        self._geocoding = geocoding
        self._comid_lookup = comid_lookup
        self._topology = topology

    async def resolve(
        self,
        frontend_target: AnalysisTargetV1 | None,
        parsed_intent: ParsedIntentV1,
    ) -> LocationResolution:
        frontend = (
            await self._resolve_frontend(frontend_target)
            if frontend_target is not None
            else None
        )
        if frontend is not None and frontend.status is not LocationResolutionStatus.SUCCESS:
            return frontend

        if (
            frontend is not None
            and frontend_target is not None
            and frontend_target.coordinates is not None
            and frontend_target.coordinates == parsed_intent.coordinate_mention
            and (
                parsed_intent.location_mention is None
                or self._is_coordinate_echo(
                    parsed_intent.location_mention,
                    parsed_intent.coordinate_mention,
                )
            )
        ):
            return frontend

        query = await self._resolve_query(parsed_intent)
        if frontend is None:
            return query
        if query.status is LocationResolutionStatus.LOCATION_REQUIRED:
            return frontend
        if query.status is not LocationResolutionStatus.SUCCESS:
            return query
        assert frontend.target is not None
        assert query.target is not None
        if frontend.target.comid != query.target.comid:
            return self._failure(LocationResolutionStatus.LOCATION_CONFLICT)
        return frontend

    async def _resolve_frontend(
        self,
        frontend_target: AnalysisTargetV1,
    ) -> LocationResolution:
        if frontend_target.coordinates is not None:
            result = await self._resolve_point(
                frontend_target.coordinates,
                TargetSource.FRONTEND,
            )
            if result.status is not LocationResolutionStatus.SUCCESS:
                return result
            assert result.target is not None
            if (
                frontend_target.comid is not None
                and frontend_target.comid != result.target.comid
            ):
                return self._failure(LocationResolutionStatus.TARGET_MISMATCH)
            return result
        assert frontend_target.comid is not None
        return await self._resolve_comid(
            frontend_target.comid,
            TargetSource.FRONTEND,
        )

    async def _resolve_query(
        self,
        parsed_intent: ParsedIntentV1,
    ) -> LocationResolution:
        text_result: LocationResolution | None = None
        coordinate_result: LocationResolution | None = None
        if (
            parsed_intent.location_mention is not None
            and not self._is_coordinate_echo(
                parsed_intent.location_mention,
                parsed_intent.coordinate_mention,
            )
        ):
            text_result = await self._resolve_text(parsed_intent.location_mention)
        if parsed_intent.coordinate_mention is not None:
            coordinate_result = await self._resolve_point(
                parsed_intent.coordinate_mention,
                TargetSource.QUERY,
            )
        if text_result is None and coordinate_result is None:
            return self._failure(LocationResolutionStatus.LOCATION_REQUIRED)
        if text_result is None:
            assert coordinate_result is not None
            return coordinate_result
        if coordinate_result is None:
            return text_result
        if text_result.status is not LocationResolutionStatus.SUCCESS:
            return text_result
        if coordinate_result.status is not LocationResolutionStatus.SUCCESS:
            return coordinate_result
        assert text_result.target is not None
        assert coordinate_result.target is not None
        if text_result.target.comid != coordinate_result.target.comid:
            return self._failure(LocationResolutionStatus.LOCATION_CONFLICT)
        return coordinate_result

    @staticmethod
    def _is_coordinate_echo(location_text: str, point: GeoPoint | None) -> bool:
        if point is None:
            return False
        match = _COORDINATE_TEXT.fullmatch(location_text)
        if match is None:
            return False
        first, second = (float(value) for value in match.groups())
        return (first, second) in (
            (point.longitude, point.latitude),
            (point.latitude, point.longitude),
        )

    async def _resolve_text(self, location_text: str) -> LocationResolution:
        candidates = await self._geocoding.resolve_text(location_text)
        if not candidates:
            return self._failure(LocationResolutionStatus.LOCATION_NOT_SUPPORTED)

        acceptable = tuple(
            candidate
            for candidate in candidates
            if self._is_supported_region(candidate.administrative_location)
        )
        if not acceptable:
            return self._failure(
                LocationResolutionStatus.LOCATION_OUTSIDE_SUPPORTED_REGION
            )

        resolved: list[ResolvedTarget] = []
        failures: list[LocationResolutionStatus] = []
        for candidate in acceptable:
            result = await self._resolve_geocoding_candidate(candidate)
            if result.target is not None:
                if all(item.comid != result.target.comid for item in resolved):
                    resolved.append(result.target)
            else:
                failures.append(result.status)
        if len(resolved) > 1:
            return self._failure(LocationResolutionStatus.LOCATION_AMBIGUOUS)
        if len(resolved) == 1:
            return LocationResolution(
                status=LocationResolutionStatus.SUCCESS,
                target=resolved[0],
            )
        if LocationResolutionStatus.LOCATION_OUTSIDE_SUPPORTED_REGION in failures:
            return self._failure(
                LocationResolutionStatus.LOCATION_OUTSIDE_SUPPORTED_REGION
            )
        return self._failure(LocationResolutionStatus.LOCATION_NOT_SUPPORTED)

    async def _resolve_geocoding_candidate(
        self,
        candidate: GeocodingCandidate,
    ) -> LocationResolution:
        return await self._resolve_point(
            candidate.point,
            TargetSource.QUERY,
            administrative_location=candidate.administrative_location,
        )

    async def _resolve_point(
        self,
        point: GeoPoint,
        source: TargetSource,
        *,
        administrative_location: AdministrativeLocation | None = None,
    ) -> LocationResolution:
        location = administrative_location or await self._geocoding.reverse(point)
        if location is None:
            return self._failure(LocationResolutionStatus.LOCATION_NOT_SUPPORTED)
        if not self._is_supported_region(location):
            return self._failure(
                LocationResolutionStatus.LOCATION_OUTSIDE_SUPPORTED_REGION
            )

        comid = await self._comid_lookup.lookup_position(point)
        if comid is None:
            return self._failure(LocationResolutionStatus.LOCATION_NOT_SUPPORTED)
        segment = await self._topology.get_segment(comid)
        if segment is None or segment.location is None:
            return self._failure(LocationResolutionStatus.LOCATION_NOT_SUPPORTED)
        segment_location = await self._geocoding.reverse(segment.location)
        if segment_location is None:
            return self._failure(LocationResolutionStatus.LOCATION_NOT_SUPPORTED)
        if not self._is_supported_region(segment_location):
            return self._failure(
                LocationResolutionStatus.LOCATION_OUTSIDE_SUPPORTED_REGION
            )
        return LocationResolution(
            status=LocationResolutionStatus.SUCCESS,
            target=ResolvedTarget(
                comid=comid,
                point=point,
                display_name=location.display_name,
                country_code="US",
                subdivision_code=location.subdivision_code,
                source=source,
            ),
        )

    async def _resolve_comid(
        self,
        comid: str,
        source: TargetSource,
    ) -> LocationResolution:
        segment = await self._topology.get_segment(comid)
        if segment is None or segment.location is None:
            return self._failure(LocationResolutionStatus.LOCATION_NOT_SUPPORTED)
        location = await self._geocoding.reverse(segment.location)
        if location is None:
            return self._failure(LocationResolutionStatus.LOCATION_NOT_SUPPORTED)
        if not self._is_supported_region(location):
            return self._failure(
                LocationResolutionStatus.LOCATION_OUTSIDE_SUPPORTED_REGION
            )
        return LocationResolution(
            status=LocationResolutionStatus.SUCCESS,
            target=ResolvedTarget(
                comid=comid,
                point=segment.location,
                display_name=location.display_name,
                country_code="US",
                subdivision_code=location.subdivision_code,
                source=source,
            ),
        )

    @staticmethod
    def _is_supported_region(location: AdministrativeLocation) -> bool:
        return (
            location.country_code == "US"
            and location.subdivision_code in CONUS_SUBDIVISION_CODES
        )

    @staticmethod
    def _failure(status: LocationResolutionStatus) -> LocationResolution:
        return LocationResolution(status=status)

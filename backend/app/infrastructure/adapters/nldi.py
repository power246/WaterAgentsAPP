from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from urllib.parse import quote

from app.domain.hydrology import (
    GeoPoint,
    MonitoringStation,
    RiverSegment,
    WaterbodyGeometry,
)
from app.infrastructure.adapters._parsing import (
    invalid_response,
    require_list,
    require_mapping,
    require_number,
    require_string,
)
from app.infrastructure.http import ProviderHttpClient

PROVIDER = "usgs_nldi"
DEFAULT_BASE_URL = "https://api.water.usgs.gov/nldi/linked-data"


class NldiAdapter:
    def __init__(
        self,
        http: ProviderHttpClient,
        *,
        base_url: str = DEFAULT_BASE_URL,
        site_association_distance_km: float = 0.001,
    ) -> None:
        if (
            not isfinite(site_association_distance_km)
            or site_association_distance_km <= 0
        ):
            raise ValueError("site_association_distance_km must be finite and positive")
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._site_association_distance_km = site_association_distance_km

    async def lookup_position(
        self,
        point: GeoPoint,
        *,
        deadline: float | None = None,
    ) -> str | None:
        operation = "lookup_position"
        features = await self._features(
            "/comid/position",
            operation=operation,
            params={
                "f": "json",
                "coords": f"POINT({point.longitude} {point.latitude})",
            },
            deadline=deadline,
        )
        comids = {
            self._feature_comid(feature, operation)
            for feature in features
        }
        if not comids:
            return None
        if len(comids) != 1:
            raise invalid_response(PROVIDER, operation)
        return next(iter(comids))

    async def get_segment(
        self,
        comid: str,
        *,
        deadline: float | None = None,
    ) -> RiverSegment | None:
        operation = "get_segment"
        features = await self._segment_features(
            comid,
            operation=operation,
            deadline=deadline,
        )
        if not features:
            return None
        if len(features) != 1:
            raise invalid_response(PROVIDER, operation)
        feature = features[0]
        feature_comid = self._feature_comid(feature, operation)
        if feature_comid != comid:
            raise invalid_response(PROVIDER, operation)
        coordinates = self._line_coordinates(feature, operation)
        geometry = self._waterbody_geometry(feature_comid, coordinates, operation)
        return RiverSegment(
            comid=feature_comid,
            location=geometry.representative_point,
            geometry_ref=geometry.geometry_ref,
            geometry_summary=geometry.summary(),
            geometry=geometry,
        )

    async def get_direct_upstreams(
        self,
        comid: str,
        max_distance_km: float,
        *,
        deadline: float | None = None,
    ) -> tuple[RiverSegment, ...]:
        self._require_comid(comid)
        if (
            isinstance(max_distance_km, bool)
            or not isfinite(max_distance_km)
            or max_distance_km <= 0
        ):
            raise ValueError("max_distance_km must be finite and positive")
        operation = "get_direct_upstreams"
        origin_features = await self._segment_features(
            comid,
            operation=operation,
            deadline=deadline,
        )
        if not origin_features:
            return ()
        if len(origin_features) != 1:
            raise invalid_response(PROVIDER, operation)
        if self._feature_comid(origin_features[0], operation) != comid:
            raise invalid_response(PROVIDER, operation)
        origin_coordinates = self._line_coordinates(origin_features[0], operation)
        self._waterbody_geometry(comid, origin_coordinates, operation)
        origin_endpoints = self._endpoint_keys(origin_coordinates)
        navigation = await self._features(
            f"/comid/{quote(comid, safe='')}/navigation/UT/flowlines",
            operation=operation,
            params={
                "distance": max_distance_km,
                "excludeGeometry": False,
                "f": "json",
            },
            deadline=deadline,
        )

        candidates: dict[str, tuple[tuple[float, float], ...]] = {}
        for feature in navigation:
            candidate_comid = self._feature_comid(feature, operation)
            coordinates = self._line_coordinates(feature, operation)
            if candidate_comid == comid:
                continue
            if not (origin_endpoints & self._endpoint_keys(coordinates)):
                continue
            previous = candidates.get(candidate_comid)
            if previous is not None and previous != coordinates:
                raise invalid_response(PROVIDER, operation)
            candidates[candidate_comid] = coordinates

        return tuple(
            self._upstream_segment(
                candidate_comid,
                coordinates,
                downstream_comid=comid,
                operation=operation,
            )
            for candidate_comid, coordinates in sorted(
                candidates.items(),
                key=lambda item: int(item[0]),
            )
        )

    async def get_monitoring_sites(
        self,
        comid: str,
        *,
        deadline: float | None = None,
    ) -> tuple[MonitoringStation, ...]:
        self._require_comid(comid)
        operation = "get_monitoring_sites"
        features = await self._features(
            f"/comid/{quote(comid, safe='')}/navigation/UT/nwissite",
            operation=operation,
            params={
                "distance": self._site_association_distance_km,
                "excludeGeometry": False,
                "f": "json",
            },
            deadline=deadline,
        )
        stations: dict[str, MonitoringStation] = {}
        for feature in features:
            properties = self._properties(feature, operation)
            associated = self._identifier(properties.get("comid"), operation)
            if associated != comid:
                continue
            station_id = require_string(
                properties.get("identifier"),
                provider=PROVIDER,
                operation=operation,
            )
            source = properties.get("source")
            if not isinstance(source, str) or not source.strip():
                source = properties.get("sourceName")
            station = MonitoringStation(
                station_id=station_id,
                source=require_string(
                    source,
                    provider=PROVIDER,
                    operation=operation,
                ),
                name=(
                    properties["name"].strip()
                    if isinstance(properties.get("name"), str)
                    and properties["name"].strip()
                    else None
                ),
                location=self._optional_point(feature, operation),
                associated_comid=comid,
            )
            previous = stations.get(station_id)
            if previous is not None and previous != station:
                raise invalid_response(PROVIDER, operation)
            stations[station_id] = station
        return tuple(stations[key] for key in sorted(stations))

    async def _segment_features(
        self,
        comid: str,
        *,
        operation: str,
        deadline: float | None,
    ) -> list[Mapping[str, object]]:
        self._require_comid(comid)
        return await self._features(
            f"/comid/{quote(comid, safe='')}",
            operation=operation,
            params={"excludeGeometry": False, "f": "json"},
            deadline=deadline,
        )

    def _upstream_segment(
        self,
        comid: str,
        coordinates: tuple[tuple[float, float], ...],
        *,
        downstream_comid: str,
        operation: str,
    ) -> RiverSegment:
        geometry = self._waterbody_geometry(comid, coordinates, operation)
        return RiverSegment(
            comid=comid,
            location=geometry.representative_point,
            geometry_ref=geometry.geometry_ref,
            geometry_summary=geometry.summary(),
            geometry=geometry,
            distance_to_downstream_km=float(geometry.length_km),
            downstream_comids=(downstream_comid,),
        )

    @staticmethod
    def _waterbody_geometry(
        comid: str,
        coordinates: tuple[tuple[float, float], ...],
        operation: str,
    ) -> WaterbodyGeometry:
        try:
            return WaterbodyGeometry.from_coordinates(
                source_comid=comid,
                coordinates=coordinates,
            )
        except ValueError:
            raise invalid_response(PROVIDER, operation) from None

    async def _features(
        self,
        path: str,
        *,
        operation: str,
        params: Mapping[str, str | int | float | bool],
        deadline: float | None,
    ) -> list[Mapping[str, object]]:
        payload = await self._http.get_json(
            f"{self._base_url}{path}",
            provider=PROVIDER,
            operation=operation,
            params=params,
            deadline=deadline,
        )
        root = require_mapping(payload, provider=PROVIDER, operation=operation)
        if root.get("type") != "FeatureCollection":
            raise invalid_response(PROVIDER, operation)
        values = require_list(
            root.get("features"),
            provider=PROVIDER,
            operation=operation,
        )
        return [
            require_mapping(value, provider=PROVIDER, operation=operation)
            for value in values
        ]

    def _feature_comid(
        self,
        feature: Mapping[str, object],
        operation: str,
    ) -> str:
        properties = self._properties(feature, operation)
        value = properties.get("nhdplus_comid", properties.get("comid"))
        comid = self._identifier(value, operation)
        self._require_comid(comid)
        return comid

    @staticmethod
    def _identifier(value: object, operation: str) -> str:
        if isinstance(value, bool):
            raise invalid_response(PROVIDER, operation)
        if isinstance(value, int):
            return str(value)
        return require_string(value, provider=PROVIDER, operation=operation)

    @staticmethod
    def _require_comid(comid: str) -> None:
        if not comid or not comid.isdecimal():
            raise ValueError("comid must contain decimal digits")

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

    def _line_coordinates(
        self,
        feature: Mapping[str, object],
        operation: str,
    ) -> tuple[tuple[float, float], ...]:
        geometry = require_mapping(
            feature.get("geometry"),
            provider=PROVIDER,
            operation=operation,
        )
        if geometry.get("type") != "LineString":
            raise invalid_response(PROVIDER, operation)
        raw_coordinates = require_list(
            geometry.get("coordinates"),
            provider=PROVIDER,
            operation=operation,
        )
        if len(raw_coordinates) < 2:
            raise invalid_response(PROVIDER, operation)
        coordinates = tuple(
            self._coordinate(value, operation)
            for value in raw_coordinates
        )
        return coordinates

    def _optional_point(
        self,
        feature: Mapping[str, object],
        operation: str,
    ) -> GeoPoint | None:
        if feature.get("geometry") is None:
            return None
        geometry = require_mapping(
            feature.get("geometry"),
            provider=PROVIDER,
            operation=operation,
        )
        if geometry.get("type") != "Point":
            raise invalid_response(PROVIDER, operation)
        longitude, latitude = self._coordinate(
            geometry.get("coordinates"),
            operation,
        )
        return GeoPoint(latitude=latitude, longitude=longitude)

    @staticmethod
    def _coordinate(value: object, operation: str) -> tuple[float, float]:
        pair = require_list(value, provider=PROVIDER, operation=operation)
        if len(pair) < 2:
            raise invalid_response(PROVIDER, operation)
        longitude = require_number(
            pair[0],
            provider=PROVIDER,
            operation=operation,
        )
        latitude = require_number(
            pair[1],
            provider=PROVIDER,
            operation=operation,
        )
        try:
            GeoPoint(latitude=latitude, longitude=longitude)
        except ValueError:
            raise invalid_response(PROVIDER, operation) from None
        return longitude, latitude

    @staticmethod
    def _endpoint_keys(
        coordinates: tuple[tuple[float, float], ...],
    ) -> set[tuple[float, float]]:
        return {
            (round(coordinates[0][0], 7), round(coordinates[0][1], 7)),
            (round(coordinates[-1][0], 7), round(coordinates[-1][1], 7)),
        }

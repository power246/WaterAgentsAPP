from collections.abc import Mapping
from itertools import pairwise
from math import cos, radians

from app.domain.evidence.aggregate_payload import AggregatePayload
from app.domain.evidence.collection_status import CollectionStatus
from app.domain.evidence.conversion_payload import ConversionPayload
from app.domain.evidence.direct_record_payload import DirectRecordPayload
from app.domain.evidence.evidence_fragment import EvidenceFragment
from app.domain.evidence.evidence_quality_class import EvidenceQualityClass
from app.domain.evidence.measured_anchor import MeasuredAnchor
from app.domain.evidence.upstream_result_payload import UpstreamResultPayload
from app.domain.hydrology._geometry_types import EARTH_RADIUS_KM
from app.domain.hydrology.hydrology_graph import HydrologyGraph
from app.domain.hydrology.location import GeoPoint
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.hydrology.waterbody_geometry import geodesic_distance_km

_MAX_POINT_OFFSET_KM = 0.1
_MAX_JUNCTION_GAP_KM = 0.05
_AMBIGUOUS_OFFSET_MARGIN_KM = 0.02
_AMBIGUOUS_MEASURE_SEPARATION_KM = 0.25


def derive_measured_anchors(
    *,
    parameter: ParameterKey,
    target_comid: str,
    evidence_ids: tuple[str, ...],
    fragments: Mapping[str, EvidenceFragment],
) -> tuple[MeasuredAnchor, ...]:
    """Trace cited evidence to original measurements of the requested parameter."""
    anchors: list[MeasuredAnchor] = []
    visited: set[str] = set()

    def visit(evidence_id: str) -> None:
        if evidence_id in visited:
            return
        visited.add(evidence_id)
        fragment = fragments.get(evidence_id)
        if (
            fragment is None
            or fragment.collection_status is not CollectionStatus.AVAILABLE
            or fragment.quality_class is EvidenceQualityClass.INVALID
        ):
            return
        payload = fragment.payload
        if isinstance(payload, (DirectRecordPayload, AggregatePayload)):
            if parameter not in fragment.parameter_refs:
                return
            origin = fragment.location.comid or fragment.target_comid
            if origin != target_comid:
                return
            anchors.append(
                MeasuredAnchor(
                    parameter=parameter,
                    source_record_refs=fragment.source_record_refs,
                    origin_comid=origin,
                    station_id=fragment.location.station_id,
                    evidence_time_range=fragment.time_range,
                    path_comids=(origin,),
                    distance_km=fragment.location.distance_to_target_km,
                    origin_point=fragment.location.point,
                    distance_basis=fragment.location.distance_basis,
                )
            )
        elif isinstance(payload, UpstreamResultPayload):
            projection = payload.cell_projection
            if projection is not None:
                anchors.extend(
                    anchor
                    for anchor in projection.measured_anchors
                    if anchor.parameter == parameter
                    and anchor.path_comids[-1] == target_comid
                )
        elif isinstance(payload, ConversionPayload):
            visit(payload.input_evidence_ref.evidence_id)
        else:
            for reference in getattr(payload, "input_evidence_refs", ()):
                visit(reference.evidence_id)

    for evidence_id in evidence_ids:
        visit(evidence_id)
    unique = {anchor.model_dump_json(): anchor for anchor in anchors}
    return tuple(unique[key] for key in sorted(unique))


def transfer_measured_anchors(
    anchors: tuple[MeasuredAnchor, ...],
    *,
    upstream_comid: str,
    downstream_comid: str,
    distance_km: float | None,
    topology: HydrologyGraph | None = None,
    destination_point: GeoPoint | None = None,
) -> tuple[MeasuredAnchor, ...]:
    transferred = []
    for anchor in anchors:
        if anchor.path_comids[-1] != upstream_comid:
            raise ValueError("measured anchor path does not end at upstream waterbody")
        values = anchor.model_dump(mode="python")
        path = (*anchor.path_comids, downstream_comid)
        mapped_distance = (
            estimate_mapped_channel_distance_km(
                topology, path, anchor.origin_point, destination_point
            )
            if topology is not None
            and anchor.origin_point is not None
            and destination_point is not None
            else None
        )
        values.update(
            path_comids=path,
            distance_km=(
                mapped_distance
                if anchor.origin_point is not None and topology is not None
                else anchor.distance_km + distance_km
                if anchor.distance_km is not None and distance_km is not None
                else None
            ),
            distance_basis=(
                "mapped_channel_estimate" if mapped_distance is not None else None
            ),
        )
        transferred.append(MeasuredAnchor.model_validate(values))
    return tuple(transferred)


def estimate_mapped_channel_distance_km(
    topology: HydrologyGraph,
    path_comids: tuple[str, ...],
    origin_point: GeoPoint | None,
    destination_point: GeoPoint | None,
) -> float | None:
    """Estimate travel length between two points along saved, connected reaches."""
    if not path_comids or origin_point is None or destination_point is None:
        return None
    nodes = {node.comid: node for node in topology.nodes}
    geometries = []
    for comid in path_comids:
        node = nodes.get(comid)
        geometry = (
            topology.geometries.get(node.geometry_ref)
            if node is not None and node.geometry_ref is not None
            else None
        )
        if geometry is None or geometry.source_comid != comid:
            return None
        geometries.append(geometry.full_coordinates)
    edge_pairs = {
        (edge.upstream_comid, edge.downstream_comid)
        for edge in topology.edges
        if edge.participating
    }
    if any(
        (upstream, downstream) not in edge_pairs
        for upstream, downstream in pairwise(path_comids)
    ):
        return None
    origin_measure = _project_onto_reach(origin_point, geometries[0])
    destination_measure = _project_onto_reach(destination_point, geometries[-1])
    if origin_measure is None or destination_measure is None:
        return None
    if len(geometries) == 1:
        return float(f"{abs(destination_measure - origin_measure):.6f}")
    junctions: list[tuple[int, int]] = []
    for upstream, downstream in pairwise(geometries):
        candidates = (
            (
                geodesic_distance_km(upstream[up_index], downstream[down_index]),
                up_index,
                down_index,
            )
            for up_index in (0, -1)
            for down_index in (0, -1)
        )
        gap, up_index, down_index = min(candidates)
        if gap > _MAX_JUNCTION_GAP_KM:
            return None
        junctions.append((up_index, down_index))
    lengths = tuple(_reach_length(coordinates) for coordinates in geometries)
    start_exit = 0.0 if junctions[0][0] == 0 else lengths[0]
    end_entry = 0.0 if junctions[-1][1] == 0 else lengths[-1]
    distance = abs(start_exit - origin_measure) + abs(destination_measure - end_entry)
    for index in range(1, len(geometries) - 1):
        if junctions[index - 1][1] == junctions[index][0]:
            return None
        distance += lengths[index]
    return float(f"{distance:.6f}")


def _reach_length(coordinates: tuple[tuple[float, float], ...]) -> float:
    return sum(geodesic_distance_km(start, end) for start, end in pairwise(coordinates))


def _project_onto_reach(
    point: GeoPoint,
    coordinates: tuple[tuple[float, float], ...],
) -> float | None:
    best_offset = float("inf")
    best_measure = 0.0
    candidates: list[tuple[float, float]] = []
    traversed = 0.0
    latitude_scale = EARTH_RADIUS_KM * radians(1)
    longitude_scale = latitude_scale * cos(radians(point.latitude))
    for start, end in pairwise(coordinates):
        segment_length = geodesic_distance_km(start, end)
        delta_longitude = ((end[0] - start[0] + 540) % 360) - 180
        x = delta_longitude * longitude_scale
        y = (end[1] - start[1]) * latitude_scale
        point_x = (((point.longitude - start[0] + 540) % 360) - 180) * longitude_scale
        point_y = (point.latitude - start[1]) * latitude_scale
        denominator = x * x + y * y
        fraction = (
            min(1.0, max(0.0, (point_x * x + point_y * y) / denominator))
            if denominator
            else 0.0
        )
        projection = (
            ((start[0] + fraction * delta_longitude + 180) % 360) - 180,
            start[1] + fraction * (end[1] - start[1]),
        )
        offset = geodesic_distance_km((point.longitude, point.latitude), projection)
        candidates.append((offset, traversed + fraction * segment_length))
        if offset < best_offset:
            best_offset = offset
            best_measure = traversed + fraction * segment_length
        traversed += segment_length
    if best_offset > _MAX_POINT_OFFSET_KM or any(
        offset <= best_offset + _AMBIGUOUS_OFFSET_MARGIN_KM
        and abs(measure - best_measure) > _AMBIGUOUS_MEASURE_SEPARATION_KM
        for offset, measure in candidates
    ):
        return None
    return best_measure

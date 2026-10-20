from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field

from app.application.evidence_tools.measurement_provenance import (
    estimate_mapped_channel_distance_km,
)
from app.application.query_understanding.context_models import AnalysisContext
from app.application.transcripts.read_models import ReadView
from app.application.transcripts.reader import (
    TranscriptContentUnreadableError,
    TranscriptItemNotFoundError,
    TranscriptReader,
)
from app.domain import (
    DischargeObservationRecord,
    EvidenceSourceRecords,
    HydrologyGraph,
    InferenceResult,
    WaterObservationRecord,
)
from app.domain.common import DomainFloat, DomainModel, NonEmptyString
from app.domain.evidence.measured_anchor import MeasuredAnchor
from app.domain.hydrology.location import GeoPoint


class AnchorDistanceEstimate(DomainModel):
    cell_id: NonEmptyString
    anchor_index: int = Field(ge=0)
    distance_km: DomainFloat = Field(ge=0)
    distance_basis: Literal["mapped_channel_estimate"] = "mapped_channel_estimate"


class AnchorDistanceEstimatesResponse(DomainModel):
    result_item_id: UUID
    estimates: tuple[AnchorDistanceEstimate, ...] = ()


async def read_anchor_distance_estimates(
    reader: TranscriptReader,
    run_id: UUID,
    result_item_id: UUID,
) -> AnchorDistanceEstimatesResponse:
    result_items = await reader.read_registered_items_persisted(
        run_id, (result_item_id,), ReadView.HISTORY
    )
    if not result_items:
        raise TranscriptItemNotFoundError(run_id, result_item_id)
    result_item = result_items[0]
    if result_item.body is None:
        raise TranscriptContentUnreadableError(run_id, result_item_id)
    result = result_item.body
    if not isinstance(result, InferenceResult) or result.run_id != run_id:
        raise TranscriptItemNotFoundError(run_id, result_item_id)
    candidates = tuple(
        (point.cell_id, index, anchor)
        for series in result.series
        for point in series.points
        for index, anchor in enumerate(point.measured_anchors)
        if anchor.distance_km is None
    )
    empty = AnchorDistanceEstimatesResponse(result_item_id=result_item_id)
    if not candidates:
        return empty
    context_ids = await reader.list_registered_run_item_ids(run_id, "query_understood")
    topology_ids = await reader.list_registered_run_item_ids(run_id, "topology_ready")
    if len(context_ids) != 1 or len(topology_ids) != 1:
        return empty
    saved = await reader.read_registered_items_persisted(
        run_id, (context_ids[0], topology_ids[0]), ReadView.HISTORY
    )
    by_id = {item.item.item_id: item.body for item in saved}
    context = by_id.get(context_ids[0])
    topology = by_id.get(topology_ids[0])
    if (
        not isinstance(context, AnalysisContext)
        or not isinstance(topology, HydrologyGraph)
        or context.target.comid != topology.target_comid
    ):
        return empty
    if result.target_comid == topology.target_comid:
        destination_point = context.target.point
    else:
        destination_point = next(
            (
                node.point
                for node in topology.nodes
                if node.comid == result.target_comid
            ),
            None,
        )
    if destination_point is None:
        return empty
    source_item_ids: set[UUID] = set()
    for _, _, anchor in candidates:
        if anchor.origin_point is not None:
            continue
        for reference in anchor.source_record_refs:
            try:
                source_item_ids.add(UUID(reference.item_id))
            except ValueError:
                continue
    sources = await reader.read_registered_items_persisted(
        run_id, tuple(sorted(source_item_ids)), ReadView.HISTORY
    )
    source_bodies = {
        str(item.item.item_id): item.body
        for item in sources
        if isinstance(item.body, EvidenceSourceRecords)
    }
    estimates = []
    for cell_id, index, anchor in candidates:
        if anchor.path_comids[-1] != result.target_comid:
            continue
        origin_point = anchor.origin_point or _original_station_point(
            anchor, source_bodies, topology
        )
        distance = estimate_mapped_channel_distance_km(
            topology, anchor.path_comids, origin_point, destination_point
        )
        if distance is not None:
            estimates.append(
                AnchorDistanceEstimate(
                    cell_id=cell_id,
                    anchor_index=index,
                    distance_km=distance,
                )
            )
    return AnchorDistanceEstimatesResponse(
        result_item_id=result_item_id, estimates=tuple(estimates)
    )


def _original_station_point(
    anchor: MeasuredAnchor,
    source_bodies: dict[str, EvidenceSourceRecords],
    topology: HydrologyGraph,
) -> GeoPoint | None:
    point = None
    origin_node = next(
        (node for node in topology.nodes if node.comid == anchor.origin_comid), None
    )
    if origin_node is None:
        return None
    station_on_origin = any(
        station.station_id == anchor.station_id for station in origin_node.station_refs
    )
    for reference in anchor.source_record_refs:
        body = source_bodies.get(reference.item_id)
        if body is None:
            return None
        record = next(
            (item for item in body.records if item.record_id == reference.record_id),
            None,
        )
        if (
            not isinstance(record, WaterObservationRecord | DischargeObservationRecord)
            or record.parameter != anchor.parameter
            or record.location.station_id != anchor.station_id
            or record.location.point is None
        ):
            return None
        if record.location.comid is not None:
            if record.location.comid != anchor.origin_comid:
                return None
        elif not station_on_origin:
            return None
        if point is None:
            point = record.location.point
        elif point != record.location.point:
            return None
    return point

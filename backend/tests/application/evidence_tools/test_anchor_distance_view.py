import asyncio
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from app.api.http.analysis_runs import router
from app.application.evidence_tools.anchor_distance_view import (
    read_anchor_distance_estimates,
)
from app.application.transcripts.reader import TranscriptItemNotFoundError
from app.domain import EvidenceSourceRecords, GeoPoint, SourceRecordReference
from app.domain.evidence.measured_anchor import MeasuredAnchor
from fastapi import FastAPI
from tests.application.evidence_tools.conftest import make_context
from tests.application.evidence_tools.test_measurement_provenance import (
    _mapped_topology,
)
from tests.application.evidence_tools.test_upstream_result_store import (
    PARAMETER,
    RUN_ID,
    inference_result,
)
from tests.domain.test_evidence import valid_source_records

RESULT_ID = UUID("50000000-0000-0000-0000-000000000300")
CONTEXT_ID = UUID("60000000-0000-0000-0000-000000000001")
TOPOLOGY_ID = UUID("60000000-0000-0000-0000-000000000002")
SOURCE_ID = UUID("60000000-0000-0000-0000-000000000003")


class SavedItemsReader:
    def __init__(self, bodies):
        self.bodies = bodies
        self.calls = []

    async def read_registered_items_persisted(self, run_id, item_ids, view):
        self.calls.append((run_id, item_ids, view.value))
        return tuple(
            SimpleNamespace(
                item=SimpleNamespace(item_id=item_id),
                body=self.bodies[(run_id, item_id)],
            )
            for item_id in item_ids
            if (run_id, item_id) in self.bodies
        )

    async def list_registered_run_item_ids(self, run_id, content_type):
        self.calls.append((run_id, content_type))
        if content_type == "query_understood":
            return (CONTEXT_ID,) if (run_id, CONTEXT_ID) in self.bodies else ()
        if content_type == "topology_ready":
            return (TOPOLOGY_ID,) if (run_id, TOPOLOGY_ID) in self.bodies else ()
        return ()


def _saved_items(*, source_comid="100", station_id="USGS-100"):
    source = valid_source_records()[0]
    station = GeoPoint(latitude=0.0001, longitude=0.004)
    source = source.model_copy(
        update={
            "parameter": PARAMETER,
            "location": source.location.model_copy(
                update={
                    "point": station,
                    "comid": source_comid,
                    "station_id": station_id,
                }
            ),
        }
    )
    ref = SourceRecordReference(item_id=str(SOURCE_ID), record_id=source.record_id)
    session_id = UUID("30000000-0000-0000-0000-000000000300")
    result = inference_result("300", session_id)
    anchor = MeasuredAnchor(
        parameter=PARAMETER,
        source_record_refs=(ref,),
        origin_comid="100",
        station_id=source.location.station_id,
        evidence_time_range=result.time_range,
        path_comids=("100", "200", "300"),
    )
    series = result.series[0]
    point = series.points[0].model_copy(update={"measured_anchors": (anchor,)})
    result = result.model_copy(
        update={
            "series": (
                series.model_copy(update={"points": (point, *series.points[1:])}),
            ),
        }
    )
    context = make_context()
    context = context.model_copy(
        update={
            "target": context.target.model_copy(
                update={
                    "comid": "300",
                    "point": GeoPoint(latitude=0.0001, longitude=0.027),
                }
            ),
        }
    )
    bodies = {
        (RUN_ID, RESULT_ID): result,
        (RUN_ID, CONTEXT_ID): context,
        (RUN_ID, TOPOLOGY_ID): _mapped_topology(),
        (RUN_ID, SOURCE_ID): EvidenceSourceRecords.model_construct(records=(source,)),
    }
    return bodies, point.cell_id, result


def test_old_result_distance_is_derived_from_saved_items_without_mutation() -> None:
    bodies, cell_id, result = _saved_items()
    original_result_json = result.model_dump_json()
    reader = SavedItemsReader(bodies)

    response = asyncio.run(read_anchor_distance_estimates(reader, RUN_ID, RESULT_ID))

    assert response.result_item_id == RESULT_ID
    assert len(response.estimates) == 1
    estimate = response.estimates[0]
    assert (estimate.cell_id, estimate.anchor_index) == (cell_id, 0)
    assert estimate.distance_basis == "mapped_channel_estimate"
    assert estimate.distance_km == pytest.approx(2.558, abs=0.005)
    assert result.model_dump_json() == original_result_json
    assert reader.calls[0][1] == (RESULT_ID,)
    assert reader.calls[-1][1] == (SOURCE_ID,)


def test_missing_or_foreign_run_source_remains_unknown() -> None:
    bodies, _, _ = _saved_items()
    source = bodies.pop((RUN_ID, SOURCE_ID))
    bodies[(UUID("10000000-0000-0000-0000-000000000002"), SOURCE_ID)] = source
    response = asyncio.run(
        read_anchor_distance_estimates(SavedItemsReader(bodies), RUN_ID, RESULT_ID)
    )
    assert response.estimates == ()


def test_source_comid_must_match_anchor_origin() -> None:
    bodies, _, _ = _saved_items(source_comid="200")

    response = asyncio.run(
        read_anchor_distance_estimates(SavedItemsReader(bodies), RUN_ID, RESULT_ID)
    )

    assert response.estimates == ()


def test_missing_source_comid_requires_station_on_origin_node() -> None:
    bodies, _, _ = _saved_items(source_comid=None)

    matching = asyncio.run(
        read_anchor_distance_estimates(SavedItemsReader(bodies), RUN_ID, RESULT_ID)
    )
    assert len(matching.estimates) == 1

    bodies, _, _ = _saved_items(source_comid=None, station_id="USGS-OTHER")
    unmatched = asyncio.run(
        read_anchor_distance_estimates(SavedItemsReader(bodies), RUN_ID, RESULT_ID)
    )
    assert unmatched.estimates == ()


def test_result_item_from_another_run_is_not_read() -> None:
    bodies, _, _ = _saved_items()
    other_run = UUID("10000000-0000-0000-0000-000000000002")
    with pytest.raises(TranscriptItemNotFoundError):
        asyncio.run(
            read_anchor_distance_estimates(
                SavedItemsReader(bodies), other_run, RESULT_ID
            )
        )


def test_result_with_foreign_run_identity_is_rejected() -> None:
    bodies, _, result = _saved_items()
    bodies[(RUN_ID, RESULT_ID)] = result.model_copy(
        update={"run_id": UUID("10000000-0000-0000-0000-000000000002")}
    )
    with pytest.raises(TranscriptItemNotFoundError):
        asyncio.run(
            read_anchor_distance_estimates(SavedItemsReader(bodies), RUN_ID, RESULT_ID)
        )


def test_distance_endpoint_returns_only_transient_estimates() -> None:
    async def exercise():
        bodies, cell_id, result = _saved_items()
        before = result.model_dump_json()
        application = FastAPI()
        application.include_router(router)
        application.state.transcript_reader = SavedItemsReader(bodies)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application),
            base_url="http://test",
        ) as client:
            url = (
                f"/api/analysis/runs/{RUN_ID}/transcript/{RESULT_ID}"
                "/anchor-distance-estimates?view=history"
            )
            response = await client.get(url)
            assert response.status_code == 200
            payload = response.json()
            assert payload["result_item_id"] == str(RESULT_ID)
            assert payload["estimates"][0]["cell_id"] == cell_id
            assert payload["estimates"][0]["anchor_index"] == 0
            assert (
                payload["estimates"][0]["distance_basis"] == "mapped_channel_estimate"
            )
            assert (
                await client.get(url.replace("view=history", "view=auto"))
            ).status_code == 422
        assert result.model_dump_json() == before

    asyncio.run(exercise())

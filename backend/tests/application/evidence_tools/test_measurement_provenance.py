import asyncio
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.application.analysis_runs.topology_result_assembler import (
    TopologyResultAssembler,
)
from app.application.evidence_tools.measurement_provenance import (
    _project_onto_reach,
    derive_measured_anchors,
    estimate_mapped_channel_distance_km,
    transfer_measured_anchors,
)
from app.application.evidence_tools.upstream_projection import project_upstream_results
from app.domain import (
    DataQualityFlag,
    EvidenceFragment,
    EvidenceQualityClass,
    GeoPoint,
    OutcomeStatus,
    ParameterKey,
    UpstreamResultPayload,
    WaterbodyGeometry,
)
from app.domain.evidence.upstream_result_cell_projection import (
    UpstreamResultCellProjection,
)
from app.domain.inference.time_window_result import TimeWindowResult
from tests.application.analysis_runs.test_topology_result_assembler import (
    committed_session,
)
from tests.application.evidence_tools.test_upstream_result_store import (
    RUN_ID,
    FakeResultReader,
    dependency_outcome,
    inference_result,
    request,
    resolved_result,
    store_for,
    topology,
)
from tests.domain.test_evidence import sample_fragments


def _local_fragment():
    direct = sample_fragments()[0]
    return direct.model_copy(update={
        "target_comid": "100",
        "location": direct.location.model_copy(update={"comid": "100"}),
        "parameter_refs": (ParameterKey(code_system="usgs", code="00010"),),
    })


def _anchors(fragment):
    return derive_measured_anchors(
        parameter=fragment.parameter_refs[0],
        target_comid=fragment.target_comid,
        evidence_ids=(fragment.evidence_id,),
        fragments={fragment.evidence_id: fragment},
    )


def _mapped_topology(*, disconnected=False):
    graph = topology("chain")
    coordinates = {
        "100": ((0.0, 0.0), (0.01, 0.0)),
        "200": ((0.01, 0.0), (0.02, 0.0)),
        "300": (
            (0.03 if disconnected else 0.02, 0.0),
            (0.04 if disconnected else 0.03, 0.0),
        ),
    }
    geometries = {
        f"geometry:{comid}": WaterbodyGeometry.from_coordinates(
            source_comid=comid, coordinates=points
        )
        for comid, points in coordinates.items()
    }
    nodes = tuple(
        node.model_copy(update={
            "geometry_ref": f"geometry:{node.comid}",
            "geometry_summary": geometries[f"geometry:{node.comid}"].summary(),
            "point": geometries[f"geometry:{node.comid}"].representative_point,
        })
        for node in graph.nodes
    )
    return graph.model_copy(update={"nodes": nodes, "geometries": geometries})


def test_mapped_distance_uses_partial_reaches_and_original_target_point() -> None:
    graph = _mapped_topology()
    station = GeoPoint(latitude=0.0001, longitude=0.004)
    target = GeoPoint(latitude=0.0001, longitude=0.027)
    direct = _local_fragment()
    direct = direct.model_copy(update={
        "location": direct.location.model_copy(update={
            "point": station,
            "distance_to_target_km": None,
        }),
    })
    anchor = _anchors(direct)[0]
    assert anchor.origin_point == station
    middle = transfer_measured_anchors(
        (anchor,), upstream_comid="100", downstream_comid="200",
        distance_km=100,
        topology=graph,
        destination_point=graph.nodes[1].point,
    )[0]
    final = transfer_measured_anchors(
        (middle,), upstream_comid="200", downstream_comid="300",
        distance_km=100,
        topology=graph,
        destination_point=target,
    )[0]
    assert middle.distance_basis == "mapped_channel_estimate"
    assert final.distance_basis == "mapped_channel_estimate"
    assert final.path_comids == ("100", "200", "300")
    assert final.distance_km == pytest.approx(2.558, abs=0.005)
    assert final.distance_km != pytest.approx(200)
    assert final.distance_km != estimate_mapped_channel_distance_km(
        graph, final.path_comids, station, graph.nodes[0].point
    )


def test_mapped_distance_keeps_each_route_and_rejects_unknown_geometry() -> None:
    graph = _mapped_topology()
    first = GeoPoint(latitude=0.0, longitude=0.004)
    second = GeoPoint(latitude=0.0, longitude=0.008)
    target = GeoPoint(latitude=0.0, longitude=0.027)
    direct = _local_fragment()
    anchors = tuple(
        _anchors(direct.model_copy(update={
            "location": direct.location.model_copy(update={
                "point": point, "distance_to_target_km": None,
            }),
        }))[0]
        for point in (first, second)
    )
    transferred = transfer_measured_anchors(
        anchors, upstream_comid="100", downstream_comid="200",
        distance_km=100, topology=graph, destination_point=graph.nodes[1].point,
    )
    final = transfer_measured_anchors(
        transferred, upstream_comid="200", downstream_comid="300",
        distance_km=100, topology=graph, destination_point=target,
    )
    assert final[0].distance_km == pytest.approx(2.558, abs=0.005)
    assert final[1].distance_km == pytest.approx(2.113, abs=0.005)
    assert all(anchor.distance_basis == "mapped_channel_estimate" for anchor in final)
    assert estimate_mapped_channel_distance_km(
        _mapped_topology(disconnected=True), ("100", "200", "300"), first, target
    ) is None
    assert estimate_mapped_channel_distance_km(
        graph, ("100", "200", "300"),
        GeoPoint(latitude=1.0, longitude=0.004), target,
    ) is None
    assert estimate_mapped_channel_distance_km(
        graph.model_copy(update={"geometries": {}}),
        ("100", "200", "300"), first, target,
    ) is None


def test_mapped_distance_on_local_reach_uses_station_and_target_positions() -> None:
    graph = _mapped_topology()
    station = GeoPoint(latitude=0.0001, longitude=0.024)
    target = GeoPoint(latitude=0.0001, longitude=0.027)
    assert estimate_mapped_channel_distance_km(
        graph, ("300",), station, target
    ) == pytest.approx(0.333585, abs=0.002)


def test_mapped_distance_rejects_ambiguous_nearby_channel_bends() -> None:
    point = GeoPoint(latitude=0.00005, longitude=0.004)
    folded_reach = (
        (0.0, 0.0), (0.01, 0.0), (0.01, 0.0001), (0.0, 0.0001)
    )
    assert _project_onto_reach(point, folded_reach) is None


def _result_with_metadata(comid, *, anchors=(), range_only=False, quality_flags=()):
    session_id = UUID(f"30000000-0000-0000-0000-{int(comid):012d}")
    result = inference_result(comid, session_id, statuses=(OutcomeStatus.QUANTIFIED,) * 2)
    points = []
    for point in result.series[0].points:
        values = point.model_dump(mode="python")
        values.update(
            inference_method="model_estimate",
            inference_basis="same_parameter_measurement" if anchors else "general_knowledge",
            measured_anchors=anchors,
            quality_flags=quality_flags,
            value_lower="0",
            value_upper="20",
        )
        if range_only:
            values.update(
                outcome_status=OutcomeStatus.RANGE_ESTIMATE,
                representative_value=None,
                aggregation_method=None,
            )
        points.append(TimeWindowResult.model_validate(values))
    return result.model_copy(update={
        "series": (result.series[0].model_copy(update={"points": tuple(points)}),)
    })


def test_anchor_trace_follows_saved_conversion_and_rejects_other_parameter() -> None:
    direct, _, conversion, *_ = sample_fragments()
    derived = derive_measured_anchors(
        parameter=direct.parameter_refs[0],
        target_comid=direct.target_comid,
        evidence_ids=(conversion.evidence_id,),
        fragments={direct.evidence_id: direct, conversion.evidence_id: conversion},
    )
    assert len(derived) == 1
    assert derived[0].source_record_refs == direct.source_record_refs
    assert derived[0].station_id == direct.location.station_id
    assert derived[0].distance_km == 0
    assert derive_measured_anchors(
        parameter=ParameterKey(code_system="usgs", code="00400"),
        target_comid=direct.target_comid,
        evidence_ids=(conversion.evidence_id,),
        fragments={direct.evidence_id: direct, conversion.evidence_id: conversion},
    ) == ()
    assert _anchors(direct.model_copy(update={"quality_class": EvidenceQualityClass.INVALID})) == ()


def test_local_anchor_retains_measured_station_distance_or_unknown() -> None:
    direct = _local_fragment()
    nearby = direct.model_copy(update={
        "location": direct.location.model_copy(update={"distance_to_target_km": 4.25}),
    })
    local_anchor = _anchors(nearby)[0]
    assert local_anchor.distance_km == 4.25
    assert transfer_measured_anchors(
        (local_anchor,), upstream_comid="100", downstream_comid="200",
        distance_km=12,
    )[0].distance_km == 16.25

    unknown = direct.model_copy(update={
        "location": direct.location.model_copy(update={"distance_to_target_km": None}),
    })
    assert _anchors(unknown)[0].distance_km is None
    assert transfer_measured_anchors(
        _anchors(unknown), upstream_comid="100", downstream_comid="200",
        distance_km=12,
    )[0].distance_km is None


def test_upstream_transfer_preserves_original_measurements_and_cumulative_distance() -> None:
    direct = _local_fragment()
    original = _anchors(direct)
    at_middle = transfer_measured_anchors(
        original, upstream_comid="100", downstream_comid="200", distance_km=120,
    )
    result = _result_with_metadata(
        "200", anchors=at_middle,
        quality_flags=(DataQualityFlag.ESTIMATED, DataQualityFlag.ANOMALOUS),
    )
    outcome = dependency_outcome("200", result.session_id, result=result)
    projected = project_upstream_results(
        request(agent_comid="300", upstreams=("200",)),
        ((outcome, resolved_result(result)),),
        edge_distances={("200", "300"): 180},
    ).records[0].cell_projection
    assert projected is not None
    anchor = projected.measured_anchors[0]
    assert anchor.origin_comid == "100"
    assert anchor.path_comids == ("100", "200", "300")
    assert anchor.distance_km == 300
    assert anchor.source_record_refs == original[0].source_record_refs
    assert projected.inference_method == "model_estimate"
    assert projected.source.value == "inferred"
    assert projected.quality_flags == (
        DataQualityFlag.ESTIMATED, DataQualityFlag.ANOMALOUS,
    )
    legacy_projection = projected.model_dump(mode="python")
    del legacy_projection["quality_flags"]
    assert (
        UpstreamResultCellProjection.model_validate(legacy_projection).quality_flags
        == ()
    )


def test_range_is_usable_upstream_but_never_creates_measured_anchor() -> None:
    async def exercise():
        result = _result_with_metadata("200", range_only=True)
        reader = FakeResultReader()
        session = committed_session("200", result)
        reader.values[session.result_ref.item_id] = resolved_result(result)
        outcome = await TopologyResultAssembler(reader).dependency_outcome(RUN_ID, session)
        assert outcome.status.value == "usable"
        store = store_for(topology("chain"), reader)
        await store.publish(outcome)
        record = (await store.collect(request(agent_comid="300", upstreams=("200",)))).records[0]
        projection = record.cell_projection
        assert projection is not None
        assert projection.outcome_status is OutcomeStatus.RANGE_ESTIMATE
        assert projection.representative_value is None
        assert projection.value_lower == "0" and projection.value_upper == "20"
        assert projection.measured_anchors == ()
        upstream = sample_fragments()[5]
        values = upstream.model_dump(mode="python")
        values.update(
            target_comid="300",
            parameter_refs=(record.parameter,),
            payload=UpstreamResultPayload(
                upstream_session_id=record.upstream_session_id,
                result_ref=record.result_ref,
                closure_refs=record.evidence_closure_refs,
                cell_projection=projection,
            ),
        )
        fragment = EvidenceFragment.model_validate(values)
        assert _anchors(fragment) == ()
        promoted = projection.model_dump(mode="python")
        promoted.update(outcome_status=OutcomeStatus.QUANTIFIED, representative_value="10")
        with pytest.raises(ValidationError, match="requires original measured anchors"):
            UpstreamResultCellProjection.model_validate(promoted)

    asyncio.run(exercise())


def test_range_without_original_measurement_stays_range_only_across_two_hops() -> None:
    upstream = _result_with_metadata("100", range_only=True)
    first = project_upstream_results(
        request(agent_comid="200", upstreams=("100",)),
        ((dependency_outcome("100", upstream.session_id, result=upstream),
          resolved_result(upstream)),),
        edge_distances={("100", "200"): 120},
    ).records[0].cell_projection
    assert first is not None
    assert first.outcome_status is OutcomeStatus.RANGE_ESTIMATE
    assert first.measured_anchors == ()

    middle = _result_with_metadata(
        "200", anchors=first.measured_anchors, range_only=True
    )
    second = project_upstream_results(
        request(agent_comid="300", upstreams=("200",)),
        ((dependency_outcome("200", middle.session_id, result=middle),
          resolved_result(middle)),),
        edge_distances={("200", "300"): 180},
    ).records[0].cell_projection
    assert second is not None
    assert second.outcome_status is OutcomeStatus.RANGE_ESTIMATE
    assert second.representative_value is None
    assert (second.value_lower, second.value_upper) == (
        first.value_lower, first.value_upper
    )
    assert second.measured_anchors == ()
    with pytest.raises(ValidationError, match="requires original measured anchors"):
        UpstreamResultCellProjection.model_validate({
            **second.model_dump(mode="python"),
            "outcome_status": OutcomeStatus.QUANTIFIED,
            "representative_value": "10",
        })

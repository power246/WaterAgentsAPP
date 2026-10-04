from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.application.analysis_runs.models import (
    AgentSessionStatus,
    PublicError,
    SessionResultReference,
    WaterbodySessionResultSummary,
)
from app.application.analysis_runs.topology_result_models import (
    DependencyDischargeState,
    DependencyOutcome,
    DependencyStatus,
    GraphCoverage,
    OutcomeCounts,
    StableSourceReference,
    TopologyRunResult,
    TopologySummaryResult,
    WaterbodyDependency,
    WaterbodyDependencyManifest,
    WaterbodyRunEntry,
)
from app.application.canonical_json import canonical_json_digest
from app.application.transcripts.models import TranscriptContentRef

RUN_ID = UUID("5c40b92c-fffb-43a7-9ee4-28a0c73f9d31")
OTHER_RUN_ID = UUID("6c40b92c-fffb-43a7-9ee4-28a0c73f9d32")
UPSTREAM_SESSION_ID = UUID("7c40b92c-fffb-43a7-9ee4-28a0c73f9d33")
TARGET_SESSION_ID = UUID("8c40b92c-fffb-43a7-9ee4-28a0c73f9d34")
NOW = datetime(2026, 10, 3, 1, tzinfo=timezone.utc)


def _item(item_id: str, *, run_id: UUID = RUN_ID) -> TranscriptContentRef:
    return TranscriptContentRef(run_id=run_id, item_id=UUID(item_id))


def _result_ref(item_id: str, *, run_id: UUID = RUN_ID) -> SessionResultReference:
    return SessionResultReference(run_id=run_id, item_id=UUID(item_id))


def _result_summary(comid: str) -> WaterbodySessionResultSummary:
    return WaterbodySessionResultSummary(
        result_id=f"result:{comid}",
        target_comid=comid,
        risk_level="low",
        availability="partial",
        overall_confidence=0.7,
    )


def _source_ref(
    comid: str = "100",
    *,
    run_id: UUID = RUN_ID,
    session_id: UUID = UPSTREAM_SESSION_ID,
    cell_id: str = "cell:100:00060:w1",
) -> StableSourceReference:
    return StableSourceReference(
        run_id=run_id,
        session_id=session_id,
        item_id=UUID("9c40b92c-fffb-43a7-9ee4-28a0c73f9d35"),
        content_type="inference_result",
        content_schema_version=1,
        result_id=f"result:{comid}",
        cell_id=cell_id,
        window_id="w1",
    )


def _manifest(**changes: object) -> WaterbodyDependencyManifest:
    values: dict[str, object] = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "session_id": TARGET_SESSION_ID,
        "target_comid": "300",
        "topology_ref": _item("ac40b92c-fffb-43a7-9ee4-28a0c73f9d36"),
        "dependencies": (
            WaterbodyDependency(
                upstream_comid="100",
                upstream_session_id=UPSTREAM_SESSION_ID,
                dependency_status=DependencyStatus.USABLE,
                result_ref=_result_ref("bc40b92c-fffb-43a7-9ee4-28a0c73f9d37"),
                usable_cell_refs=(_source_ref(),),
                discharge_states=(
                    DependencyDischargeState(window_id="w1", outcome="quantified"),
                ),
            ),
            WaterbodyDependency(
                upstream_comid="200",
                upstream_session_id=UUID("cc40b92c-fffb-43a7-9ee4-28a0c73f9d38"),
                dependency_status=DependencyStatus.INSUFFICIENT,
            ),
        ),
    }
    values.update(changes)
    canonical_values = {
        **values,
        "dependencies": tuple(
            sorted(values["dependencies"], key=lambda item: item.upstream_comid)
        ),
    }
    values["manifest_digest"] = canonical_json_digest(canonical_values)
    return WaterbodyDependencyManifest.model_validate(values)


def _entry(
    position: int,
    comid: str,
    session_id: UUID,
    result_item: str,
) -> WaterbodyRunEntry:
    return WaterbodyRunEntry(
        graph_position=position,
        target_comid=comid,
        session_id=session_id,
        dependency_status=DependencyStatus.USABLE,
        session_status=AgentSessionStatus.COMPLETED,
        result_ref=_result_ref(result_item),
        result_summary=_result_summary(comid),
    )


def _topology_result(**changes: object) -> TopologyRunResult:
    upstream = _entry(
        0,
        "100",
        UPSTREAM_SESSION_ID,
        "dc40b92c-fffb-43a7-9ee4-28a0c73f9d39",
    )
    target = _entry(
        1,
        "300",
        TARGET_SESSION_ID,
        "ec40b92c-fffb-43a7-9ee4-28a0c73f9d40",
    )
    values: dict[str, object] = {
        "run_id": RUN_ID,
        "analysis_context_ref": _item("0d40b92c-fffb-43a7-9ee4-28a0c73f9d42"),
        "topology_ref": _item("1d40b92c-fffb-43a7-9ee4-28a0c73f9d43"),
        "target_comid": "300",
        "target_result_ref": target.result_ref,
        "target_result_summary": target.result_summary,
        "graph_coverage": GraphCoverage.COMPLETE,
        "node_outcomes": (target, upstream),
        "outcome_counts": OutcomeCounts(usable=2),
        "summary": TopologySummaryResult(
            status="completed",
            session_id=UUID("2d40b92c-fffb-43a7-9ee4-28a0c73f9d44"),
            result_ref=_result_ref("fc40b92c-fffb-43a7-9ee4-28a0c73f9d41"),
        ),
        "generated_at": NOW,
    }
    values.update(changes)
    return TopologyRunResult.model_validate(values)


def test_dependency_outcome_enforces_all_four_terminal_shapes() -> None:
    usable = DependencyOutcome(
        run_id=RUN_ID,
        target_comid="100",
        session_id=UPSTREAM_SESSION_ID,
        status="usable",
        result_ref=_result_ref("3d40b92c-fffb-43a7-9ee4-28a0c73f9d45"),
        result_summary=_result_summary("100"),
        usable_cell_refs=(_source_ref(),),
        discharge_states=(
            DependencyDischargeState(window_id="w1", outcome="quantified"),
        ),
        completed_at=NOW,
    )
    insufficient = DependencyOutcome(
        run_id=RUN_ID,
        target_comid="100",
        session_id=UPSTREAM_SESSION_ID,
        status="insufficient",
        result_ref=usable.result_ref,
        result_summary=usable.result_summary,
        completed_at=NOW,
    )
    invalid = DependencyOutcome(
        run_id=RUN_ID,
        target_comid="100",
        session_id=UPSTREAM_SESSION_ID,
        status="invalid",
        error_ref=_result_ref("4d40b92c-fffb-43a7-9ee4-28a0c73f9d46"),
        completed_at=NOW,
    )
    cancelled = DependencyOutcome(
        run_id=RUN_ID,
        target_comid="100",
        session_id=UPSTREAM_SESSION_ID,
        status="cancelled",
        completed_at=NOW,
    )

    assert [item.status.value for item in (usable, insufficient, invalid, cancelled)] == [
        "usable",
        "insufficient",
        "invalid",
        "cancelled",
    ]
    with pytest.raises(ValidationError, match="outside|invalid dependency"):
        DependencyOutcome(
            run_id=RUN_ID,
            target_comid="100",
            session_id=UPSTREAM_SESSION_ID,
            status="invalid",
            usable_cell_refs=(_source_ref(),),
            completed_at=NOW,
        )


def test_dependency_manifest_is_canonical_and_digest_bound() -> None:
    manifest = _manifest()
    reordered = _manifest(dependencies=tuple(reversed(manifest.dependencies)))

    assert tuple(item.upstream_comid for item in manifest.dependencies) == ("100", "200")
    assert reordered.manifest_digest == manifest.manifest_digest
    with pytest.raises(ValidationError, match="dependency COMIDs"):
        _manifest(dependencies=(manifest.dependencies[0], manifest.dependencies[0]))
    with pytest.raises(ValidationError, match="canonical manifest"):
        WaterbodyDependencyManifest.model_validate(
            {**manifest.model_dump(), "manifest_digest": "sha256:bad"}
        )


def test_manifest_rejects_cross_run_and_nonusable_evidence() -> None:
    manifest = _manifest()
    with pytest.raises(ValidationError, match="remain in the run"):
        _manifest(
            topology_ref=_item(
                "5d40b92c-fffb-43a7-9ee4-28a0c73f9d47",
                run_id=OTHER_RUN_ID,
            )
        )
    with pytest.raises(ValidationError, match="non-usable"):
        WaterbodyDependency(
            upstream_comid="200",
            upstream_session_id=UPSTREAM_SESSION_ID,
            dependency_status="insufficient",
            usable_cell_refs=(manifest.dependencies[0].usable_cell_refs[0],),
        )
    with pytest.raises(ValidationError, match="inference_result v1"):
        WaterbodyDependency(
            upstream_comid="100",
            upstream_session_id=UPSTREAM_SESSION_ID,
            dependency_status="usable",
            result_ref=manifest.dependencies[0].result_ref,
            usable_cell_refs=(
                _source_ref().model_copy(update={"content_type": "evidence_fragment"}),
            ),
        )


def test_topology_result_canonicalizes_complete_outcomes() -> None:
    result = _topology_result()

    assert tuple(item.target_comid for item in result.node_outcomes) == ("100", "300")
    assert result.graph_coverage is GraphCoverage.COMPLETE
    assert result.outcome_counts == OutcomeCounts(usable=2)


def test_topology_result_supports_partial_non_target_failure() -> None:
    complete = _topology_result()
    target = complete.node_outcomes[1]
    failed = WaterbodyRunEntry(
        graph_position=0,
        target_comid="100",
        session_id=UPSTREAM_SESSION_ID,
        dependency_status="invalid",
        session_status="failed",
    )
    partial = _topology_result(
        node_outcomes=(target, failed),
        graph_coverage="partial",
        outcome_counts=OutcomeCounts(usable=1, invalid=1),
    )

    assert partial.graph_coverage is GraphCoverage.PARTIAL
    assert partial.outcome_counts.invalid == 1


def test_topology_result_rejects_missing_target_and_cross_run_refs() -> None:
    with pytest.raises(ValidationError, match="exactly one target"):
        _topology_result(target_comid="999")
    with pytest.raises(ValidationError, match="contiguous"):
        _topology_result(node_outcomes=(_topology_result().node_outcomes[1],))
    with pytest.raises(ValidationError, match="remain in the run"):
        _topology_result(
            analysis_context_ref=_item(
                "6d40b92c-fffb-43a7-9ee4-28a0c73f9d48",
                run_id=OTHER_RUN_ID,
            )
        )
    target = _entry(
        0,
        "300",
        TARGET_SESSION_ID,
        "9d40b92c-fffb-43a7-9ee4-28a0c73f9d51",
    )
    upstream = _entry(
        1,
        "100",
        UPSTREAM_SESSION_ID,
        "ad40b92c-fffb-43a7-9ee4-28a0c73f9d52",
    )
    with pytest.raises(ValidationError, match="final stable graph entry"):
        _topology_result(
            target_result_ref=target.result_ref,
            target_result_summary=target.result_summary,
            node_outcomes=(target, upstream),
        )


def test_target_failure_cannot_be_encoded_as_a_normal_topology_result() -> None:
    with pytest.raises(ValidationError, match="dependency status must match"):
        WaterbodyRunEntry(
            graph_position=1,
            target_comid="300",
            session_id=TARGET_SESSION_ID,
            dependency_status="invalid",
            session_status="failed",
            result_ref=_result_ref("7d40b92c-fffb-43a7-9ee4-28a0c73f9d49"),
            result_summary=_result_summary("300"),
        )


def test_failed_summary_requires_a_safe_public_error() -> None:
    failed = TopologySummaryResult(
        status="failed",
        session_id=TARGET_SESSION_ID,
        public_error=PublicError(
            error_id=UUID("8d40b92c-fffb-43a7-9ee4-28a0c73f9d50"),
            code="summary_failed",
            message="The run summary could not be generated.",
            retryable=True,
        ),
    )

    assert failed.result_ref is None

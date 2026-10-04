from uuid import UUID

import pytest

from app.application.analysis_runs.models import SessionResultReference
from app.application.analysis_runs.node_launch import (
    NodeLaunchAction,
    NodeLaunchFacts,
    NodeLaunchPolicy,
)
from app.application.analysis_runs.topology_result_models import (
    DependencyStatus,
    StableSourceReference,
    WaterbodyDependency,
    WaterbodyDependencyManifest,
)
from app.application.canonical_json import canonical_json_digest
from app.application.transcripts.models import TranscriptContentRef


RUN_ID = UUID("10000000-0000-0000-0000-000000000001")
SESSION_ID = UUID("20000000-0000-0000-0000-000000000001")
TOPOLOGY_ITEM_ID = UUID("30000000-0000-0000-0000-000000000001")


def dependency(comid: str, status: DependencyStatus) -> WaterbodyDependency:
    session_id = UUID(f"40000000-0000-0000-0000-{int(comid):012d}")
    if status is DependencyStatus.USABLE:
        item_id = UUID(f"50000000-0000-0000-0000-{int(comid):012d}")
        return WaterbodyDependency(
            upstream_comid=comid,
            upstream_session_id=session_id,
            dependency_status=status,
            result_ref=SessionResultReference(run_id=RUN_ID, item_id=item_id),
            usable_cell_refs=(
                StableSourceReference(
                    run_id=RUN_ID,
                    session_id=session_id,
                    item_id=item_id,
                    content_type="inference_result",
                    content_schema_version=1,
                    result_id=f"result:{comid}",
                    cell_id=f"cell:{comid}",
                    window_id="window-000",
                ),
            ),
        )
    return WaterbodyDependency(
        upstream_comid=comid,
        upstream_session_id=(
            session_id if status is DependencyStatus.INSUFFICIENT else None
        ),
        dependency_status=status,
    )


def manifest(*dependencies: WaterbodyDependency) -> WaterbodyDependencyManifest:
    values = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "session_id": SESSION_ID,
        "target_comid": "300",
        "topology_ref": TranscriptContentRef(
            run_id=RUN_ID,
            item_id=TOPOLOGY_ITEM_ID,
        ),
        "dependencies": tuple(dependencies),
    }
    return WaterbodyDependencyManifest(
        **values,
        manifest_digest=canonical_json_digest(values),
    )


def facts(
    *,
    station_backed: bool,
    upstreams: tuple[str, ...] = (),
    collected_upstreams: tuple[str, ...] = (),
    target: bool = False,
    discharge: bool = False,
    blocked: bool = False,
    cancelled: bool = False,
    stop: bool = False,
) -> NodeLaunchFacts:
    return NodeLaunchFacts(
        run_id=RUN_ID,
        session_id=SESSION_ID,
        target_comid="300",
        topology_item_id=TOPOLOGY_ITEM_ID,
        station_backed=station_backed,
        direct_upstream_comids=upstreams,
        collected_usable_upstream_comids=collected_upstreams,
        local_target_usable=target,
        local_discharge_usable=discharge,
        preflight_blocked=blocked,
        preflight_cancelled=cancelled,
        stop_before_model=stop,
    )


@pytest.mark.parametrize(
    ("local_target", "local_discharge", "dependencies", "expected", "stop"),
    (
        (True, False, (), NodeLaunchAction.RUN_AGENT, False),
        (
            False,
            True,
            (dependency("200", DependencyStatus.USABLE),),
            NodeLaunchAction.RUN_AGENT,
            False,
        ),
        (
            False,
            False,
            (dependency("200", DependencyStatus.INSUFFICIENT),),
            NodeLaunchAction.DETERMINISTIC_INSUFFICIENT,
            True,
        ),
        (True, True, (), NodeLaunchAction.RUN_AGENT, False),
    ),
)
def test_station_backed_paths_include_source_nodes(
    local_target,
    local_discharge,
    dependencies,
    expected,
    stop,
) -> None:
    upstreams = tuple(item.upstream_comid for item in dependencies)

    decision = NodeLaunchPolicy().decide(
        facts(
            station_backed=True,
            upstreams=upstreams,
            collected_upstreams=tuple(
                item.upstream_comid
                for item in dependencies
                if item.dependency_status is DependencyStatus.USABLE
            ),
            target=local_target,
            discharge=local_discharge,
            stop=stop,
        ),
        manifest(*dependencies),
    )

    assert decision.action is expected


def test_stationless_usable_branch_runs_while_invalid_branch_stays_redacted() -> None:
    usable = dependency("100", DependencyStatus.USABLE)
    invalid = dependency("200", DependencyStatus.INVALID)
    current = manifest(usable, invalid)

    decision = NodeLaunchPolicy().decide(
        facts(
            station_backed=False,
            upstreams=("100", "200"),
            collected_upstreams=("100",),
        ),
        current,
    )

    assert decision.action is NodeLaunchAction.RUN_AGENT
    assert decision.usable_upstream_comids == ("100",)
    assert current.dependencies[1].result_ref is None
    assert current.dependencies[1].usable_cell_refs == ()


def test_stationless_all_unusable_is_deterministic_insufficient() -> None:
    current = manifest(
        dependency("100", DependencyStatus.INSUFFICIENT),
        dependency("200", DependencyStatus.INVALID),
    )

    decision = NodeLaunchPolicy().decide(
        facts(
            station_backed=False,
            upstreams=("100", "200"),
            stop=True,
        ),
        current,
    )

    assert decision.action is NodeLaunchAction.DETERMINISTIC_INSUFFICIENT
    assert decision.reason_codes == ("upstream_dependencies_insufficient",)


def test_cancel_and_technical_preflight_win_without_starting_agent() -> None:
    current = manifest(dependency("200", DependencyStatus.INVALID))

    cancelled = NodeLaunchPolicy().decide(
        facts(
            station_backed=False,
            upstreams=("200",),
            cancelled=True,
        ),
        current,
    )
    blocked = NodeLaunchPolicy().decide(
        facts(
            station_backed=False,
            upstreams=("200",),
            blocked=True,
        ),
        current,
    )

    assert cancelled.action is NodeLaunchAction.CANCELLED
    assert blocked.action is NodeLaunchAction.INVALID


def test_manifest_scope_and_acquisition_gate_mismatches_are_invalid() -> None:
    current = manifest(dependency("200", DependencyStatus.INSUFFICIENT))

    wrong_scope = NodeLaunchPolicy().decide(
        facts(
            station_backed=False,
            upstreams=("999",),
            stop=True,
        ),
        current,
    )
    wrong_gate = NodeLaunchPolicy().decide(
        facts(
            station_backed=False,
            upstreams=("200",),
            stop=False,
        ),
        current,
    )
    usable = manifest(dependency("200", DependencyStatus.USABLE))
    missing_collection = NodeLaunchPolicy().decide(
        facts(
            station_backed=False,
            upstreams=("200",),
        ),
        usable,
    )

    assert wrong_scope.action is NodeLaunchAction.INVALID
    assert wrong_gate.action is NodeLaunchAction.INVALID
    assert missing_collection.action is NodeLaunchAction.INVALID
    assert missing_collection.reason_codes == ("upstream_collection_mismatch",)

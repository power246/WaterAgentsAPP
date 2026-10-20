from types import SimpleNamespace

from app.application.reasoning.topology_coverage import topology_coverage_facts
from app.domain import (
    BranchOutcome,
    BranchOutcomeReason,
    BranchOutcomeStatus,
    CoverageUnit,
    ParameterKey,
)

PH = ParameterKey(code_system="usgs", code="00400")
FLOW = ParameterKey(code_system="usgs", code="00060")


def _outcome(root, parameter, index, status, reason, path):
    return BranchOutcome(
        branch_root_comid=root,
        coverage_unit=CoverageUnit(
            parameter=parameter, segment_index=0, window_index=index
        ),
        outcome=status,
        stop_comid=path[0],
        path_to_target=path,
        reason=reason,
    )


def _scope(agent):
    gaps = tuple(
        _outcome(
            root,
            parameter,
            index,
            BranchOutcomeStatus.NO_QUALIFYING_SOURCE,
            BranchOutcomeReason.NATURAL_HEADWATER,
            (root, "1777628", "1777642"),
        )
        for root in ("1776712", "1776714")
        for parameter in (PH, FLOW)
        for index in range(4)
    )
    backup = _outcome(
        "1777616",
        PH,
        0,
        BranchOutcomeStatus.BACKUP_SEARCH_ENDED,
        BranchOutcomeReason.MAX_NODES_REACHED,
        ("1777616", "1777630", "1777642"),
    )
    return SimpleNamespace(
        agent_comid=agent,
        topology=SimpleNamespace(branch_outcomes=(*gaps, backup)),
        topology_ref=SimpleNamespace(item_id="saved-topology-id"),
        time_windows=tuple(
            SimpleNamespace(window_id=f"window-{index:03d}", segment_index=0)
            for index in range(4)
        ),
    )


def test_gaps_reach_only_downstream_agents_and_do_not_count_backup_search() -> None:
    joined = topology_coverage_facts(_scope("1777628"))
    target = topology_coverage_facts(_scope("1777642"))
    unaffected = topology_coverage_facts(_scope("1777630"))

    assert len(joined) == len(target) == 2
    assert unaffected == ()
    ph = next(fact for fact in joined if "00400" in fact.fact_id)
    assert "1776712" in ph.summary and "1776714" in ph.summary
    assert "window-000" in ph.summary and "window-003" in ph.summary
    assert "saved-topology-id" in ph.summary
    assert "not a zero reading" in ph.summary
    assert "1777616" not in ph.summary


def test_numeric_gap_is_scoped_to_its_parameter_and_window() -> None:
    (fact,) = topology_coverage_facts(
        _scope("1777628"), parameter=PH, window_id="window-002"
    )

    assert "usgs:00400" in fact.summary
    assert "window-002" in fact.summary
    assert "window-000" not in fact.summary
    assert (
        topology_coverage_facts(_scope("1777630"), parameter=PH, window_id="window-002")
        == ()
    )


def test_different_branch_window_gaps_are_not_conflated() -> None:
    scope = _scope("1777642")
    scope.topology.branch_outcomes = tuple(
        outcome
        for outcome in scope.topology.branch_outcomes
        if outcome.coverage_unit.parameter == PH
        and (
            outcome.branch_root_comid == "1776712"
            and outcome.coverage_unit.window_index == 0
            or outcome.branch_root_comid == "1776714"
            and outcome.coverage_unit.window_index == 1
        )
    )

    (fact,) = topology_coverage_facts(scope)
    assert "branch roots 1776712 in window-000" in fact.summary
    assert "branch roots 1776714 in window-001" in fact.summary

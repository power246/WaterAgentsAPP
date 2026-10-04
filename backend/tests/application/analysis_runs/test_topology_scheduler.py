import asyncio

import pytest

from app.application.analysis_runs.topology_result_models import DependencyStatus
from app.application.analysis_runs.topology_scheduler import (
    TopologyExecutionInvalidError,
    TopologyScheduler,
    WaterbodyWorkflowLimiter,
    build_topology_execution_view,
)
from app.infrastructure.config import AppSettings
from tests.application.analysis_runs._topology_scheduler_support import (
    RUN_ID,
    RecordingOutcomePort,
    RecordingRunner,
    large_topology,
    topology,
)


def run_scheduler(graph, runner, *, concurrency=4):
    async def exercise():
        port = RecordingOutcomePort()
        result = await TopologyScheduler(
            run_id=RUN_ID,
            topology=graph,
            runner=runner,
            outcome_port=port,
            limiter=WaterbodyWorkflowLimiter(concurrency),
        ).run()
        return result, port

    return asyncio.run(exercise())


@pytest.mark.parametrize(
    ("graph", "expected"),
    (
        (
            topology(("100", "200", "300")),
            ("100", "200", "300"),
        ),
        (
            topology(("100", "300"), ("200", "300")),
            ("100", "200", "300"),
        ),
        (
            topology(
                ("100", "200", "400"),
                ("100", "300", "400"),
            ),
            ("100", "200", "300", "400"),
        ),
    ),
)
def test_chain_fork_and_diamond_execute_each_active_node_once(
    graph,
    expected,
) -> None:
    runner = RecordingRunner()

    result, port = run_scheduler(graph, runner)

    assert tuple(item.node.comid for item in runner.requests) == expected
    assert tuple(item.target_comid for item in result.outcomes) == expected
    assert {item.target_comid for item in port.outcomes} == set(expected)


def test_merge_waits_for_all_dependencies_and_preserves_statuses() -> None:
    graph = topology(("100", "300"), ("200", "300"))
    runner = RecordingRunner(
        statuses={
            "100": DependencyStatus.USABLE,
            "200": DependencyStatus.INVALID,
            "300": DependencyStatus.INSUFFICIENT,
        }
    )

    result, _ = run_scheduler(graph, runner)

    target = next(item for item in runner.requests if item.node.comid == "300")
    assert tuple(item.target_comid for item in target.dependency_outcomes) == (
        "100",
        "200",
    )
    assert tuple(item.status for item in target.dependency_outcomes) == (
        DependencyStatus.USABLE,
        DependencyStatus.INVALID,
    )
    assert result.outcomes[-1].target_comid == "300"


def test_all_insufficient_dependencies_still_settle_downstream() -> None:
    graph = topology(("100", "300"), ("200", "300"))
    runner = RecordingRunner()

    result, _ = run_scheduler(graph, runner)

    target = runner.requests[-1]
    assert target.node.comid == "300"
    assert {
        item.status for item in target.dependency_outcomes
    } == {DependencyStatus.INSUFFICIENT}
    assert len(result.outcomes) == 3


def test_runner_exception_becomes_invalid_and_does_not_stop_other_nodes() -> None:
    graph = topology(("100", "300"), ("200", "300"))
    runner = RecordingRunner(
        failures={"100"},
        statuses={"200": DependencyStatus.USABLE},
    )

    result, _ = run_scheduler(graph, runner)

    assert tuple(item.node.comid for item in runner.failure_requests) == ("100",)
    assert next(
        item.status for item in result.outcomes if item.target_comid == "100"
    ) is DependencyStatus.INVALID
    target = runner.requests[-1]
    assert target.node.comid == "300"
    assert {item.status for item in target.dependency_outcomes} == {
        DependencyStatus.INVALID,
        DependencyStatus.USABLE,
    }


def test_maximum_saved_topology_is_fully_covered_without_scope_expansion() -> None:
    graph = large_topology()
    runner = RecordingRunner()

    result, _ = run_scheduler(graph, runner, concurrency=32)

    active = {
        node.comid
        for node in graph.nodes
        if node.participation.value == "active"
    }
    assert len(active) == graph.limits.max_nodes == 128
    assert {request.node.comid for request in runner.requests} == active
    assert {item.target_comid for item in result.outcomes} == active


def test_per_session_failure_does_not_create_a_run_wide_budget() -> None:
    graph = topology(("100", "300"), ("200", "300"))
    runner = RecordingRunner(
        statuses={
            "100": DependencyStatus.INVALID,
            "200": DependencyStatus.USABLE,
        },
    )

    result, _ = run_scheduler(graph, runner, concurrency=1)

    assert len(result.outcomes) == 3
    assert runner.requests[-1].node.comid == "300"


def test_scheduler_revalidates_the_saved_graph_before_execution() -> None:
    graph = topology(("100", "200", "300"))
    corrupted = graph.model_construct(
        **{
            **graph.__dict__,
            "execution_layers": tuple(reversed(graph.execution_layers)),
        }
    )

    with pytest.raises(
        TopologyExecutionInvalidError,
        match="saved topology failed execution validation",
    ):
        build_topology_execution_view(corrupted)


def test_workflow_concurrency_setting_has_strict_bounds() -> None:
    assert AppSettings(_env_file=None).waterbody_workflow_max_concurrency == 4
    assert (
        AppSettings(
            _env_file=None,
            waterbody_workflow_max_concurrency=32,
        ).waterbody_workflow_max_concurrency
        == 32
    )
    for invalid in (0, 33, True, 4.5):
        with pytest.raises(ValueError):
            AppSettings(
                _env_file=None,
                waterbody_workflow_max_concurrency=invalid,
            )

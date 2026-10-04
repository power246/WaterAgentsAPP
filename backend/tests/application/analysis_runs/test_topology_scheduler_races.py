import asyncio
from uuid import UUID

import pytest

from app.application.analysis_runs.topology_result_models import DependencyStatus
from app.application.analysis_runs.topology_scheduler import (
    TopologyScheduleState,
    TopologyScheduler,
    WaterbodyWorkflowLimiter,
    build_topology_execution_view,
)
from tests.application.analysis_runs._topology_scheduler_support import (
    ConcurrencyProbe,
    RUN_ID,
    RecordingOutcomePort,
    RecordingRunner,
    outcome,
    topology,
)


def test_duplicate_completion_decrements_downstream_once() -> None:
    async def exercise() -> None:
        graph = topology(("100", "200"))
        state = TopologyScheduleState(
            run_id=RUN_ID,
            view=build_topology_execution_view(graph),
        )
        source = await state.claim_ready()
        assert source is not None and source.node.comid == "100"
        terminal = outcome("100")

        settled = await asyncio.gather(
            state.settle(terminal),
            state.settle(terminal),
        )

        assert sorted(settled) == [False, True]
        downstream = await state.claim_ready()
        assert downstream is not None and downstream.node.comid == "200"
        assert await state.claim_ready() is None

    asyncio.run(exercise())


def test_out_of_order_completion_never_starts_merge_early() -> None:
    async def exercise() -> None:
        graph = topology(("100", "300"), ("200", "300"))
        view = build_topology_execution_view(graph)
        state = TopologyScheduleState(run_id=RUN_ID, view=view)
        first = await state.claim_ready()
        second = await state.claim_ready()
        assert first is not None and second is not None
        assert (first.node.comid, second.node.comid) == ("100", "200")

        assert await state.settle(outcome("200")) is True
        assert await state.claim_ready() is None
        assert await state.settle(outcome("100")) is True
        merge = await state.claim_ready()
        assert merge is not None and merge.node.comid == "300"
        assert tuple(item.target_comid for item in merge.dependency_outcomes) == (
            "100",
            "200",
        )

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("limit", "source_count", "expected_peak"),
    (
        (1, 4, 1),
        (4, 8, 4),
        (32, 40, 32),
    ),
)
def test_limiter_bounds_parallel_independent_nodes(
    limit,
    source_count,
    expected_peak,
) -> None:
    async def exercise() -> None:
        release = asyncio.Event()
        graph = topology(
            *((str(comid), "1000") for comid in range(1, source_count + 1))
        )
        runner = RecordingRunner(
            expected_overlap=expected_peak,
            release=release,
        )
        scheduler = TopologyScheduler(
            run_id=RUN_ID,
            topology=graph,
            runner=runner,
            outcome_port=RecordingOutcomePort(),
            limiter=WaterbodyWorkflowLimiter(limit),
        )
        scheduled = asyncio.create_task(scheduler.run())
        await asyncio.wait_for(runner.overlap_reached.wait(), timeout=1)
        assert runner.peak_in_flight == expected_peak
        release.set()
        result = await scheduled
        assert len(result.outcomes) == source_count + 1
        assert runner.peak_in_flight == expected_peak

    asyncio.run(exercise())


def test_one_shared_limiter_bounds_two_runs_globally() -> None:
    async def exercise() -> None:
        release = asyncio.Event()
        limiter = WaterbodyWorkflowLimiter(3)
        probe = ConcurrencyProbe(3)
        run_ids = (
            RUN_ID,
            UUID("10000000-0000-0000-0000-000000000002"),
        )
        runners = [
            RecordingRunner(release=release, concurrency_probe=probe),
            RecordingRunner(release=release, concurrency_probe=probe),
        ]
        schedulers = [
            TopologyScheduler(
                run_id=run_id,
                topology=topology(
                    (str(index * 10 + 1), str(index * 10 + 9)),
                    (str(index * 10 + 2), str(index * 10 + 9)),
                ),
                runner=runner,
                outcome_port=RecordingOutcomePort(),
                limiter=limiter,
            )
            for index, (run_id, runner) in enumerate(
                zip(run_ids, runners, strict=True),
                start=1,
            )
        ]
        tasks = [asyncio.create_task(item.run()) for item in schedulers]
        await asyncio.wait_for(probe.reached.wait(), timeout=1)
        assert probe.current == 3
        release.set()
        await asyncio.gather(*tasks)
        assert probe.peak == 3
        assert probe.current == 0

    asyncio.run(exercise())


def test_exception_and_out_of_order_branches_converge_once() -> None:
    graph = topology(("100", "300"), ("200", "300"))
    runner = RecordingRunner(
        failures={"200"},
        statuses={"100": DependencyStatus.USABLE},
    )

    async def exercise() -> None:
        result = await TopologyScheduler(
            run_id=RUN_ID,
            topology=graph,
            runner=runner,
            outcome_port=RecordingOutcomePort(),
            limiter=WaterbodyWorkflowLimiter(2),
        ).run()
        assert tuple(item.target_comid for item in result.outcomes) == (
            "100",
            "200",
            "300",
        )
        assert sum(
            request.node.comid == "300" for request in runner.requests
        ) == 1

    asyncio.run(exercise())

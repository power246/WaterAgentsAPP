from __future__ import annotations

import asyncio
import heapq
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from typing import AsyncIterator, Protocol, Self
from uuid import UUID

from pydantic import Field, ValidationError, field_validator, model_validator

from app.application.analysis_runs.topology_result_models import DependencyOutcome
from app.domain import (
    DomainModel,
    HydrologyGraph,
    HydrologyNode,
    HydrologyParticipation,
    NonEmptyString,
    ensure_unique_tuple,
)


class TopologyExecutionInvalidError(RuntimeError):
    code = "topology_execution_invalid"


class NodeExecutionRequest(DomainModel):
    run_id: UUID
    node: HydrologyNode
    graph_position: int = Field(strict=True, ge=0)
    layer_index: int = Field(strict=True, ge=0)
    direct_upstream_comids: tuple[NonEmptyString, ...]
    dependency_outcomes: tuple[DependencyOutcome, ...]

    @field_validator("direct_upstream_comids")
    @classmethod
    def validate_direct_upstreams(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        ensure_unique_tuple(value, field_name="scheduled direct upstream COMIDs")
        if value != tuple(sorted(value)):
            raise ValueError("scheduled direct upstream COMIDs must be sorted")
        return value

    @model_validator(mode="after")
    def validate_dependencies(self) -> Self:
        if self.node.participation is not HydrologyParticipation.ACTIVE:
            raise ValueError("scheduled node must be active")
        if tuple(item.target_comid for item in self.dependency_outcomes) != (
            self.direct_upstream_comids
        ):
            raise ValueError("scheduled dependency outcomes must be exact and sorted")
        if any(item.run_id != self.run_id for item in self.dependency_outcomes):
            raise ValueError("scheduled dependency outcomes must remain in the run")
        return self


class NodeRunner(Protocol):
    async def run(self, request: NodeExecutionRequest) -> DependencyOutcome: ...

    async def settle_failure(
        self,
        request: NodeExecutionRequest,
        error: Exception,
    ) -> DependencyOutcome: ...


class DependencyOutcomePort(Protocol):
    async def publish(self, outcome: DependencyOutcome) -> None: ...


@dataclass(frozen=True, slots=True)
class TopologyExecutionNode:
    node: HydrologyNode
    graph_position: int
    layer_index: int
    direct_upstream_comids: tuple[str, ...]
    direct_downstream_comids: tuple[str, ...]

    @property
    def priority(self) -> tuple[int, int, str]:
        return self.layer_index, self.graph_position, self.node.comid


@dataclass(frozen=True, slots=True)
class TopologyExecutionView:
    target_comid: str
    nodes: tuple[TopologyExecutionNode, ...]


@dataclass(frozen=True, slots=True)
class TopologyScheduleResult:
    run_id: UUID
    outcomes: tuple[DependencyOutcome, ...]


class WaterbodyWorkflowLimiter:
    def __init__(self, max_concurrency: int) -> None:
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or not 1 <= max_concurrency <= 32
        ):
            raise ValueError(
                "waterbody workflow concurrency must be an integer from 1 to 32"
            )
        self.max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        async with self._semaphore:
            yield


class _NodeState(Enum):
    WAITING = "waiting"
    READY = "ready"
    CLAIMED = "claimed"
    TERMINAL = "terminal"


class TopologyScheduleState:
    """Own ready, claim, and settle transitions for one immutable graph."""

    def __init__(self, *, run_id: UUID, view: TopologyExecutionView) -> None:
        self._run_id = run_id
        self._view = view
        self._nodes = {item.node.comid: item for item in view.nodes}
        self._remaining = {
            item.node.comid: len(item.direct_upstream_comids) for item in view.nodes
        }
        self._states = {
            item.node.comid: _NodeState.WAITING for item in view.nodes
        }
        self._outcomes: dict[str, DependencyOutcome] = {}
        self._ready: list[tuple[tuple[int, int, str], str]] = []
        self._lock = asyncio.Lock()
        for item in view.nodes:
            if not item.direct_upstream_comids:
                self._mark_ready(item)

    async def claim_ready(self) -> NodeExecutionRequest | None:
        async with self._lock:
            while self._ready:
                _, comid = heapq.heappop(self._ready)
                if self._states[comid] is not _NodeState.READY:
                    continue
                self._states[comid] = _NodeState.CLAIMED
                item = self._nodes[comid]
                outcomes = tuple(
                    self._outcomes[upstream]
                    for upstream in item.direct_upstream_comids
                )
                return NodeExecutionRequest(
                    run_id=self._run_id,
                    node=item.node,
                    graph_position=item.graph_position,
                    layer_index=item.layer_index,
                    direct_upstream_comids=item.direct_upstream_comids,
                    dependency_outcomes=outcomes,
                )
            return None

    async def settle(self, outcome: DependencyOutcome) -> bool:
        async with self._lock:
            if outcome.run_id != self._run_id:
                raise TopologyExecutionInvalidError(
                    "node outcome left the scheduled run"
                )
            comid = outcome.target_comid
            if comid not in self._nodes:
                raise TopologyExecutionInvalidError(
                    "node outcome is outside active topology"
                )
            state = self._states[comid]
            if state is _NodeState.TERMINAL:
                if self._outcomes[comid] != outcome:
                    raise TopologyExecutionInvalidError(
                        "duplicate node completion changed its outcome"
                    )
                return False
            if state is not _NodeState.CLAIMED:
                raise TopologyExecutionInvalidError(
                    "only a claimed node may become terminal"
                )
            self._states[comid] = _NodeState.TERMINAL
            self._outcomes[comid] = outcome
            for downstream in self._nodes[comid].direct_downstream_comids:
                remaining = self._remaining[downstream] - 1
                if remaining < 0:
                    raise TopologyExecutionInvalidError(
                        "dependency counter was decremented more than once"
                    )
                self._remaining[downstream] = remaining
                if remaining == 0:
                    self._mark_ready(self._nodes[downstream])
            return True

    async def is_complete(self) -> bool:
        async with self._lock:
            return len(self._outcomes) == len(self._nodes)

    async def stable_outcomes(self) -> tuple[DependencyOutcome, ...]:
        async with self._lock:
            if len(self._outcomes) != len(self._nodes):
                raise TopologyExecutionInvalidError("topology execution is incomplete")
            return tuple(
                self._outcomes[item.node.comid] for item in self._view.nodes
            )

    def _mark_ready(self, item: TopologyExecutionNode) -> None:
        comid = item.node.comid
        if self._states[comid] is not _NodeState.WAITING:
            raise TopologyExecutionInvalidError("node became ready more than once")
        self._states[comid] = _NodeState.READY
        heapq.heappush(self._ready, (item.priority, comid))


class TopologyScheduler:
    def __init__(
        self,
        *,
        run_id: UUID,
        topology: HydrologyGraph,
        runner: NodeRunner,
        outcome_port: DependencyOutcomePort,
        limiter: WaterbodyWorkflowLimiter,
    ) -> None:
        self._run_id = run_id
        self._view = build_topology_execution_view(topology)
        self._runner = runner
        self._outcome_port = outcome_port
        self._limiter = limiter

    async def run(self) -> TopologyScheduleResult:
        state = TopologyScheduleState(run_id=self._run_id, view=self._view)
        running: dict[
            asyncio.Task[tuple[NodeExecutionRequest, DependencyOutcome]],
            NodeExecutionRequest,
        ] = {}
        try:
            while not await state.is_complete():
                while request := await state.claim_ready():
                    task = asyncio.create_task(self._execute(request))
                    running[task] = request
                if not running:
                    raise TopologyExecutionInvalidError(
                        "topology execution has no ready or running node"
                    )
                completed, _ = await asyncio.wait(
                    tuple(running),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                ordered = sorted(
                    completed,
                    key=lambda task: _request_priority(running[task]),
                )
                for task in ordered:
                    running.pop(task)
                    _, outcome = task.result()
                    await self._outcome_port.publish(outcome)
                    await state.settle(outcome)
        finally:
            for task in running:
                task.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
        return TopologyScheduleResult(
            run_id=self._run_id,
            outcomes=await state.stable_outcomes(),
        )

    async def _execute(
        self,
        request: NodeExecutionRequest,
    ) -> tuple[NodeExecutionRequest, DependencyOutcome]:
        async with self._limiter.slot():
            try:
                outcome = await self._runner.run(request)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                outcome = await self._runner.settle_failure(request, error)
        if outcome.run_id != request.run_id:
            raise TopologyExecutionInvalidError("runner outcome left the scheduled run")
        if outcome.target_comid != request.node.comid:
            raise TopologyExecutionInvalidError(
                "runner outcome changed the scheduled COMID"
            )
        return request, outcome


def build_topology_execution_view(
    topology: HydrologyGraph,
) -> TopologyExecutionView:
    try:
        graph = HydrologyGraph.model_validate(topology.model_dump(mode="python"))
    except (TypeError, ValueError, ValidationError) as error:
        raise TopologyExecutionInvalidError(
            "saved topology failed execution validation"
        ) from error

    active = {
        node.comid: node
        for node in graph.nodes
        if node.participation is HydrologyParticipation.ACTIVE
    }
    order = tuple(comid for layer in graph.execution_layers for comid in layer.root)
    positions = {comid: index for index, comid in enumerate(order)}
    layers = {
        comid: index
        for index, layer in enumerate(graph.execution_layers)
        for comid in layer.root
    }
    upstreams = {comid: [] for comid in active}
    downstreams = {comid: [] for comid in active}
    for edge in graph.edges:
        if not edge.participating:
            continue
        upstreams[edge.downstream_comid].append(edge.upstream_comid)
        downstreams[edge.upstream_comid].append(edge.downstream_comid)
    nodes = tuple(
        TopologyExecutionNode(
            node=active[comid],
            graph_position=positions[comid],
            layer_index=layers[comid],
            direct_upstream_comids=tuple(sorted(upstreams[comid])),
            direct_downstream_comids=tuple(
                sorted(downstreams[comid], key=positions.__getitem__)
            ),
        )
        for comid in order
    )
    return TopologyExecutionView(target_comid=graph.target_comid, nodes=nodes)


def _request_priority(request: NodeExecutionRequest) -> tuple[int, int, str]:
    return request.layer_index, request.graph_position, request.node.comid

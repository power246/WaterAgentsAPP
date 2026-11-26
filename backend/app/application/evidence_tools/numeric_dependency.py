from __future__ import annotations

import asyncio
import heapq
import logging
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from app.application.analysis_runs.workflow import WorkflowFailed, WorkflowStopped
from app.application.evidence_tools.errors import EvidenceScopeViolationError
from app.application.evidence_tools.measurement_provenance import (
    derive_measured_anchors,
)
from app.application.evidence_tools.models import EvidenceToolReader, EvidenceToolWriter
from app.application.evidence_tools.numeric_estimation import (
    NumericEstimateRequest,
    NumericEstimationError,
    NumericEstimator,
)
from app.application.evidence_tools.numeric_methods import (
    NumericMethod,
    NumericMethodInput,
    NumericMethodRegistry,
)
from app.application.evidence_tools.scope import (
    EvidenceSessionScope,
    EvidenceWindow,
    with_intermediate_parameters,
)
from app.application.evidence_tools.temporal_candidate import (
    TemporalCandidate,
    build_temporal_fragment,
    select_temporal_candidates,
)
from app.domain import (
    AggregatePayload,
    CollectionFactPayload,
    CollectionStatus,
    ConversionPayload,
    CoverageStatus,
    DataQualityFlag,
    DirectRecordPayload,
    EvidenceApplicability,
    EvidenceCoverage,
    EvidenceDerivation,
    EvidenceFragment,
    EvidenceFragmentReference,
    EvidenceLocation,
    EvidenceQualityClass,
    EvidenceResultEligibility,
    InferenceMode,
    ItemReference,
    NumericDependencyPlan,
    NumericDerivedPayload,
    NumericPlanInput,
    NumericPlanTask,
    ParameterCapabilityRegistry,
    ParameterKey,
    Provenance,
    TemporalEstimatePayload,
    TimeRange,
    UpstreamResultPayload,
)
from app.domain.common import normalize_canonical_decimal

logger = logging.getLogger(__name__)


class NumericDependencyPlanError(ValueError):
    pass


class NumericDependencyCycleError(NumericDependencyPlanError):
    pass


class NumericInputsUnavailable(NumericDependencyPlanError):
    pass


@dataclass(frozen=True, slots=True)
class NumericExecutionOutcome:
    plan_ref: ItemReference | None
    fragment_refs: tuple[EvidenceFragmentReference, ...]
    blocked_task_ids: tuple[str, ...]
    failed_task_ids: tuple[str, ...]
    failure_codes: tuple[tuple[str, str], ...] = ()


def prepare_numeric_scope(
    scope: EvidenceSessionScope,
    registry: NumericMethodRegistry,
    *,
    include_production_temporal: bool = False,
) -> EvidenceSessionScope:
    """Fix the parameter closure before task validation or data collection."""
    requested = {*scope.requested_parameters, *scope.supporting_parameters}
    intermediate: set[ParameterKey] = set()
    windows = {window.window_id: window for window in scope.time_windows}
    visiting: set[tuple[ParameterKey, str]] = set()
    visited: set[tuple[ParameterKey, str]] = set()

    def visit(parameter: ParameterKey, window_id: str) -> None:
        cell = (parameter, window_id)
        if cell in visited:
            return
        if cell in visiting:
            return
        window = windows[window_id]
        if window.window_kind is None:
            return
        visiting.add(cell)
        methods = tuple(
            method
            for method in registry.for_output(parameter)
            if not _is_temporal_method(method) or include_production_temporal
            if window.window_kind.value in method.allowed_modes
        )
        for method in methods:
            for input_ in method.inputs:
                if input_.parameter not in requested:
                    intermediate.add(input_.parameter)
                if input_.window_relation == "same_window":
                    visit(input_.parameter, window_id)
                elif not _is_temporal_method(method):
                    # The actual input window depends on saved evidence. Close over
                    # every eligible window for this one selected method.
                    for candidate_id in _candidate_window_ids(
                        windows, window_id, input_.window_relation
                    ):
                        if (input_.parameter, candidate_id) not in visiting:
                            visit(input_.parameter, candidate_id)
        visiting.remove(cell)
        visited.add(cell)

    for window in scope.time_windows:
        if window.window_kind is None:
            continue
        for parameter in (*scope.requested_parameters, *scope.supporting_parameters):
            visit(parameter, window.window_id)
    return with_intermediate_parameters(
        scope,
        tuple(sorted(intermediate, key=lambda item: (item.code_system, item.code))),
    )


class NumericDependencyPlanner:
    def __init__(
        self,
        *,
        registry: NumericMethodRegistry,
        reader: EvidenceToolReader,
        max_tasks: int = 256,
        max_depth: int = 16,
        include_production_temporal: bool = False,
        capability_registry: ParameterCapabilityRegistry | None = None,
    ) -> None:
        if max_tasks < 1 or max_depth < 1:
            raise ValueError("numeric planning limits must be positive")
        self._registry = registry
        self._reader = reader
        self._max_tasks = max_tasks
        self._max_depth = max_depth
        self._include_production_temporal = include_production_temporal
        self._capability_registry = capability_registry

    async def plan(
        self,
        scope: EvidenceSessionScope,
        references: tuple[EvidenceFragmentReference, ...],
        *,
        allow_estimation: bool = False,
        excluded_task_ids: tuple[str, ...] = (),
    ) -> NumericDependencyPlan:
        fragments = await self._reader.read_fragments(scope, references)
        windows = {item.window_id: item for item in scope.time_windows}
        saved: dict[
            tuple[ParameterKey, str],
            list[tuple[EvidenceFragmentReference, EvidenceFragment]],
        ] = {}
        for reference, fragment in zip(references, fragments, strict=True):
            if not _usable_numeric(fragment, scope.agent_comid):
                continue
            for window in scope.time_windows:
                if fragment.time_range == window.time_range:
                    saved.setdefault(
                        (fragment.parameter_refs[0], window.window_id), []
                    ).append((reference, fragment))
        for candidates in saved.values():
            candidates.sort(
                key=lambda item: (_seed_priority(item[1]), item[0].evidence_id)
            )

        tasks: dict[str, NumericPlanTask] = {}
        temporal_tasks: dict[str, NumericPlanTask] = {}
        temporal_by_cell: dict[tuple[ParameterKey, str], list[str]] = {}
        temporal_priority: dict[str, tuple[int, float, str, str, str]] = {}
        if self._capability_registry is not None:
            temporal_candidates = select_temporal_candidates(
                scope, references, fragments, self._capability_registry
            )
            authorized = {
                *scope.requested_parameters,
                *scope.supporting_parameters,
                *scope.intermediate_parameters,
            }
            for candidate in temporal_candidates:
                if candidate.parameter not in authorized:
                    continue
                task = _temporal_task(scope, self._registry, candidate)
                if task is None:
                    continue
                temporal_tasks[task.task_id] = task
                midpoint = candidate.window.time_range.start + (
                    candidate.window.time_range.end
                    - candidate.window.time_range.start
                ) / 2
                temporal_priority[task.task_id] = (
                    0 if candidate.method == "linear_interpolation" else 1,
                    max(
                        abs((anchor.midpoint - midpoint).total_seconds())
                        for anchor in candidate.anchors
                    ),
                    candidate.station_id,
                    candidate.series_identity,
                    candidate.source,
                )
                temporal_by_cell.setdefault(
                    (candidate.parameter, candidate.window_id), []
                ).append(task.task_id)
            for task_ids in temporal_by_cell.values():
                task_ids.sort(key=lambda task_id: (*temporal_priority[task_id], task_id))
        selected: dict[
            tuple[ParameterKey, str, str | None, str | None, bool | None],
            str | EvidenceFragmentReference | None,
        ] = {}
        visiting: set[tuple[ParameterKey, str]] = set()

        def expand(
            parameter: ParameterKey,
            window_id: str,
            *,
            depth: int,
            required: NumericMethodInput | None = None,
        ) -> str | EvidenceFragmentReference | None:
            cell = (parameter, window_id)
            if cell in visiting:
                raise NumericDependencyCycleError("numeric task dependency cycle")
            selection = (
                parameter,
                window_id,
                required.unit if required is not None else None,
                required.required_payload_type if required is not None else None,
                required.allow_estimated if required is not None else None,
            )
            if selection in selected:
                return selected[selection]
            if depth > self._max_depth:
                raise NumericDependencyPlanError("numeric task depth limit exceeded")
            window = windows[window_id]
            candidates = saved.get(cell, ())
            for reference, fragment in candidates:
                if _seed_matches_input(fragment, required):
                    selected[selection] = reference
                    return reference
            for task_id in temporal_by_cell.get(cell, ()):
                if task_id in excluded_task_ids:
                    continue
                temporal_task = temporal_tasks[task_id]
                temporal_method = self._registry.get(
                    temporal_task.method_id, temporal_task.method_version
                )
                if temporal_method is not None and _temporal_matches_input(
                    temporal_method, required
                ):
                    tasks[task_id] = temporal_task
                    if len(tasks) > self._max_tasks:
                        raise NumericDependencyPlanError(
                            "numeric task count limit exceeded"
                        )
                    selected[selection] = task_id
                    return task_id
            visiting.add(cell)
            try:
                cycle_error: NumericDependencyCycleError | None = None
                methods = tuple(
                    method
                    for method in self._registry.for_output(parameter)
                    if (
                        method.kind == "explicit_calculation"
                        and
                        window.window_kind is not None
                        and window.window_kind.value in method.allowed_modes
                        and (
                            not _is_temporal_method(method)
                            or self._include_production_temporal
                        )
                        and _derived_matches_input(method, required)
                    )
                )
                for method in methods:
                    old_tasks = set(tasks)
                    old_selected = set(selected)
                    bindings: list[NumericPlanInput] = []
                    station_id = None
                    series_identity = None
                    try:
                        for input_spec in method.inputs:
                            binding, station_id, series_identity = bind_input(
                                method,
                                input_spec,
                                window_id,
                                depth,
                                station_id,
                                series_identity,
                            )
                            if binding is None:
                                break
                            bindings.append(binding)
                    except NumericDependencyCycleError as error:
                        cycle_error = error
                    if len(bindings) != len(method.inputs):
                        for task_id in set(tasks) - old_tasks:
                            tasks.pop(task_id)
                        for added_cell in set(selected) - old_selected:
                            selected.pop(added_cell)
                        continue
                    task_id = _task_id(
                        scope, method, window_id, station_id, series_identity
                    )
                    if task_id in excluded_task_ids:
                        for added_task_id in set(tasks) - old_tasks:
                            tasks.pop(added_task_id)
                        for added_cell in set(selected) - old_selected:
                            selected.pop(added_cell)
                        continue
                    task = NumericPlanTask(
                        task_id=task_id,
                        target_comid=scope.agent_comid,
                        output_parameter=parameter,
                        output_unit=method.output_unit,
                        window_id=window_id,
                        mode=window.window_kind.value,
                        method_id=method.method_id,
                        method_version=method.version,
                        station_id=station_id,
                        series_identity=series_identity,
                        inputs=tuple(bindings),
                    )
                    tasks[task_id] = task
                    if len(tasks) > self._max_tasks:
                        raise NumericDependencyPlanError(
                            "numeric task count limit exceeded"
                        )
                    selected[selection] = task_id
                    return task_id
                if allow_estimation:
                    method = next((
                        candidate for candidate in self._registry.for_output(parameter)
                        if candidate.kind == "model_estimate"
                        and window.window_kind is not None
                        and window.window_kind.value in candidate.allowed_modes
                        and _derived_matches_input(candidate, required)
                        and _task_id(scope, candidate, window_id, None, None) not in excluded_task_ids
                    ), None)
                    if method is not None:
                        bindings = []
                        discharge = ParameterKey(code_system="usgs", code="00060")
                        supports = tuple(dict.fromkeys((
                            *((discharge,) if parameter != discharge else ()),
                            *(input_spec.parameter
                              for explicit in methods
                              for input_spec in explicit.inputs
                              if input_spec.parameter != parameter),
                            *(input_parameter
                              for rule in scope.evidence_rule_plan.effective_rules
                              if rule.output_parameter == parameter
                              for input_parameter in rule.input_parameters
                              if input_parameter != parameter),
                        )))
                        authorized = {*scope.requested_parameters,
                                      *scope.supporting_parameters,
                                      *scope.intermediate_parameters}
                        for support in supports:
                            if support not in authorized or (support, window_id) in visiting:
                                continue
                            predecessor = expand(support, window_id, depth=depth + 1)
                            if predecessor is None:
                                continue
                            predecessor_unit = (
                                _numeric_fragment_unit(next(
                                    fragment for reference, fragment in saved[(support, window_id)]
                                    if reference == predecessor
                                ))
                                if isinstance(predecessor, EvidenceFragmentReference)
                                else tasks[predecessor].output_unit
                            )
                            bindings.append(NumericPlanInput(
                                parameter=support, unit=predecessor_unit,
                                window_id=window_id, required=False,
                                evidence_ref=predecessor if isinstance(predecessor, EvidenceFragmentReference) else None,
                                dependency_task_id=predecessor if isinstance(predecessor, str) else None,
                            ))
                        task_id = _task_id(scope, method, window_id, None, None)
                        tasks[task_id] = NumericPlanTask(
                            task_id=task_id, target_comid=scope.agent_comid,
                            output_parameter=parameter, output_unit=method.output_unit,
                            window_id=window_id, mode=window.window_kind.value,
                            method_id=method.method_id, method_version=method.version,
                            method_kind="model_estimate", inputs=tuple(bindings),
                        )
                        if len(tasks) > self._max_tasks:
                            raise NumericDependencyPlanError("numeric task count limit exceeded")
                        selected[selection] = task_id
                        return task_id
                if cycle_error is not None:
                    raise cycle_error
            finally:
                visiting.remove(cell)
            selected[selection] = None
            return None

        def bind_input(
            method: NumericMethod,
            input_spec: NumericMethodInput,
            window_id: str,
            depth: int,
            station_id: str | None,
            series_identity: str | None,
        ) -> tuple[NumericPlanInput | None, str | None, str | None]:
            if input_spec.window_relation == "same_window":
                predecessor = expand(
                    input_spec.parameter,
                    window_id,
                    depth=depth + 1,
                    required=input_spec,
                )
                predecessor_window_id = window_id
            elif _is_temporal_method(method):
                # Production temporal anchors are original observed aggregates.
                anchor = _select_anchor(
                    scope,
                    saved,
                    method,
                    window_id,
                    input_spec.window_relation,
                    station_id=station_id,
                    series_identity=series_identity,
                )
                if anchor is None:
                    return None, station_id, series_identity
                predecessor_window_id, predecessor, anchor_fragment = anchor
                station_id = anchor_fragment.location.station_id
                series_identity = anchor_fragment.series_identity
            else:
                predecessor = None
                predecessor_window_id = window_id
                for candidate_id in _candidate_window_ids(
                    windows, window_id, input_spec.window_relation
                ):
                    predecessor = expand(
                        input_spec.parameter,
                        candidate_id,
                        depth=depth + 1,
                        required=input_spec,
                    )
                    if predecessor is not None:
                        predecessor_window_id = candidate_id
                        break
            if predecessor is None:
                return None, station_id, series_identity
            return (
                NumericPlanInput(
                    parameter=input_spec.parameter,
                    unit=input_spec.unit,
                    window_id=predecessor_window_id,
                    evidence_ref=(
                        predecessor
                        if isinstance(predecessor, EvidenceFragmentReference)
                        else None
                    ),
                    dependency_task_id=(
                        predecessor if isinstance(predecessor, str) else None
                    ),
                ),
                station_id,
                series_identity,
            )

        for window in scope.time_windows:
            for parameter in (
                *scope.requested_parameters,
                *scope.supporting_parameters,
            ):
                expand(parameter, window.window_id, depth=0)
        return NumericDependencyPlan.create(
            run_id=scope.run_id,
            session_id=scope.session_id,
            target_comid=scope.agent_comid,
            method_registry_digest=self._registry.manifest_digest,
            source_fragment_refs=references,
            tasks=tuple(sorted(tasks.values(), key=lambda item: item.task_id)),
        )


class NumericDependencyExecutor:
    def __init__(
        self,
        *,
        registry: NumericMethodRegistry,
        reader: EvidenceToolReader,
        writer: EvidenceToolWriter,
        limiter: asyncio.Semaphore,
        max_tasks: int = 256,
        max_concurrency: int = 4,
        capability_registry: ParameterCapabilityRegistry | None = None,
        total_deadline_seconds: float = 180,
    ) -> None:
        if max_concurrency < 1 or max_concurrency > 32:
            raise ValueError("numeric concurrency must be between 1 and 32")
        if total_deadline_seconds <= 0:
            raise ValueError("numeric total deadline must be positive")
        self._registry = registry
        self._reader = reader
        self._writer = writer
        self._limiter = limiter
        self._max_tasks = max_tasks
        self._max_concurrency = max_concurrency
        self._capability_registry = capability_registry
        self._total_deadline_seconds = total_deadline_seconds

    async def run(
        self,
        scope: EvidenceSessionScope,
        plan: NumericDependencyPlan,
        *,
        estimator: NumericEstimator | None = None,
    ) -> NumericExecutionOutcome:
        async with asyncio.timeout(self._total_deadline_seconds):
            return await self._run_within_deadline(scope, plan, estimator=estimator)

    async def _run_within_deadline(
        self,
        scope: EvidenceSessionScope,
        plan: NumericDependencyPlan,
        *,
        estimator: NumericEstimator | None = None,
    ) -> NumericExecutionOutcome:
        if (
            plan.run_id != scope.run_id
            or plan.session_id != scope.session_id
            or plan.target_comid != scope.agent_comid
            or plan.method_registry_digest != self._registry.manifest_digest
            or len(plan.tasks) > self._max_tasks
        ):
            raise EvidenceScopeViolationError()
        if not plan.tasks:
            return NumericExecutionOutcome(None, (), (), ())
        scope.cancel_token.checkpoint()
        plan_ref = await self._writer.write_numeric_plan(scope, plan)
        if await self._reader.read_numeric_plan(scope, plan_ref) != plan:
            raise EvidenceScopeViolationError()
        temporal_by_task: dict[str, TemporalCandidate] = {}
        if any(task.method_id.startswith("temporal_gap_fill_") for task in plan.tasks):
            if self._capability_registry is None:
                raise EvidenceScopeViolationError()
            fragments = await self._reader.read_fragments(
                scope, plan.source_fragment_refs
            )
            for candidate in select_temporal_candidates(
                scope,
                plan.source_fragment_refs,
                fragments,
                self._capability_registry,
            ):
                task = _temporal_task(scope, self._registry, candidate)
                if task is not None:
                    temporal_by_task[task.task_id] = candidate
            if any(
                temporal_by_task.get(task.task_id) is None
                or _temporal_task(scope, self._registry, temporal_by_task[task.task_id])
                != task
                for task in plan.tasks
                if task.method_id.startswith("temporal_gap_fill_")
            ):
                raise EvidenceScopeViolationError()
        tasks = {task.task_id: task for task in plan.tasks}
        children: dict[str, list[str]] = {task_id: [] for task_id in tasks}
        remaining = {}
        for task in plan.tasks:
            predecessors = {
                item.dependency_task_id
                for item in task.inputs
                if item.dependency_task_id is not None
            }
            remaining[task.task_id] = len(predecessors)
            for predecessor in predecessors:
                children[predecessor].append(task.task_id)
        ready = [task_id for task_id, count in remaining.items() if count == 0]
        heapq.heapify(ready)
        running: dict[asyncio.Task[EvidenceFragmentReference], str] = {}
        stopped = asyncio.create_task(scope.cancel_token.wait())
        published: dict[str, EvidenceFragmentReference] = {}
        blocked: set[str] = set()
        failed: set[str] = set()
        settled: set[str] = set()
        failure_codes: dict[str, str] = {}
        try:
            while ready or running:
                scope.cancel_token.checkpoint()
                while ready and len(running) < self._max_concurrency:
                    task_id = heapq.heappop(ready)
                    if any(
                        item.required and item.dependency_task_id in blocked | failed
                        for item in tasks[task_id].inputs
                    ):
                        blocked.add(task_id)
                        settled.add(task_id)
                        _release_children(task_id, children, remaining, ready)
                        continue
                    future = asyncio.create_task(
                        self._execute(
                            scope,
                            plan,
                            plan_ref,
                            tasks[task_id],
                            published,
                            temporal_by_task,
                            estimator,
                        )
                    )
                    running[future] = task_id
                if not running:
                    continue
                done, _ = await asyncio.wait(
                    (*running, stopped), return_when=asyncio.FIRST_COMPLETED
                )
                if stopped in done:
                    scope.cancel_token.checkpoint()
                for future in sorted(done, key=lambda item: running[item]):
                    task_id = running.pop(future)
                    try:
                        published[task_id] = future.result()
                    except asyncio.CancelledError:
                        raise
                    except (WorkflowFailed, WorkflowStopped):
                        raise
                    except NumericInputsUnavailable:
                        blocked.add(task_id)
                    except Exception as error:
                        logger.exception(
                            "Numeric dependency task failed.",
                            extra={
                                "event_name": "numeric_dependency_task_failed",
                                "run_id": scope.run_id,
                                "session_id": scope.session_id,
                                "task_id": task_id,
                            },
                        )
                        failed.add(task_id)
                        failure_codes[task_id] = (
                            error.code if isinstance(error, NumericEstimationError)
                            else "numeric_dependency_timeout" if isinstance(error, TimeoutError)
                            else "numeric_dependency_failed"
                        )
                    settled.add(task_id)
                    _release_children(task_id, children, remaining, ready)
        finally:
            for future in running:
                future.cancel()
            if running:
                await asyncio.gather(*running, return_exceptions=True)
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)
        if len(settled) != len(tasks):
            raise NumericDependencyPlanError("numeric schedule did not settle")
        return NumericExecutionOutcome(
            plan_ref=plan_ref,
            fragment_refs=tuple(
                published[task.task_id]
                for task in plan.tasks
                if task.task_id in published
            ),
            blocked_task_ids=tuple(sorted(blocked)),
            failed_task_ids=tuple(sorted(failed)),
            failure_codes=tuple(sorted(failure_codes.items())),
        )

    async def _execute(
        self,
        scope: EvidenceSessionScope,
        plan: NumericDependencyPlan,
        plan_ref: ItemReference,
        task: NumericPlanTask,
        published: dict[str, EvidenceFragmentReference],
        temporal_by_task: dict[str, TemporalCandidate],
        estimator: NumericEstimator | None = None,
    ) -> EvidenceFragmentReference:
        async with self._limiter:
            scope.cancel_token.checkpoint()
            if task.method_kind == "model_estimate":
                if estimator is None:
                    raise EvidenceScopeViolationError()
                return await self._execute_estimate(
                    scope, plan, plan_ref, task, published, estimator
                )
            if task.task_id in temporal_by_task:
                return await self._execute_temporal(
                    scope, plan, plan_ref, task, temporal_by_task[task.task_id]
                )
            method = self._registry.get(task.method_id, task.method_version)
            if (
                method is None
                or method.output_parameter != task.output_parameter
                or method.output_unit != task.output_unit
                or len(method.inputs) != len(task.inputs)
                or task.mode not in method.allowed_modes
            ):
                raise EvidenceScopeViolationError()
            input_refs = tuple(
                input_.evidence_ref or published[input_.dependency_task_id]
                for input_ in task.inputs
            )
            fragments = await self._reader.read_fragments(scope, input_refs)
            if any(not _usable_numeric(fragment, scope.agent_comid)
                   or (isinstance(fragment.payload, NumericDerivedPayload)
                       and fragment.payload.value is None)
                   for fragment in fragments):
                raise NumericInputsUnavailable("numeric method input has no point value")
            windows = {item.window_id: item for item in scope.time_windows}
            target = windows[task.window_id].time_range
            values = []
            input_ranges = []
            for input_, specification, fragment in zip(
                task.inputs, method.inputs, fragments, strict=True
            ):
                value, unit = numeric_fragment_value(fragment)
                if (
                    input_.parameter != specification.parameter
                    or input_.unit != specification.unit
                    or fragment.parameter_refs != (input_.parameter,)
                    or unit != input_.unit
                    or fragment.time_range != windows[input_.window_id].time_range
                    or fragment.target_comid != scope.agent_comid
                    or fragment.location.comid != scope.agent_comid
                    or not _usable_numeric(fragment, scope.agent_comid)
                    or not _window_relation_matches(
                        specification.window_relation,
                        fragment.time_range,
                        target,
                    )
                    or (
                        specification.required_payload_type is not None
                        and fragment.payload.payload_type
                        != specification.required_payload_type
                    )
                    or (
                        not specification.allow_estimated
                        and (
                            isinstance(
                                fragment.payload,
                                (TemporalEstimatePayload, NumericDerivedPayload),
                            )
                            or DataQualityFlag.ESTIMATED in fragment.quality_flags
                        )
                    )
                ):
                    raise EvidenceScopeViolationError()
                values.append(value)
                input_ranges.append(fragment.time_range)
            assert method.calculate is not None
            result = method.calculate(tuple(values), tuple(input_ranges), target)
            result_value = normalize_canonical_decimal(result)
            confidence_cap = numeric_confidence_cap(method, fragments, target)
            identity = f"{plan.plan_digest}:{task.task_id}:" + ":".join(
                reference.evidence_id for reference in input_refs
            )
            operation_id = str(uuid5(NAMESPACE_URL, f"m5:numeric:{identity}"))
            seconds = int((target.end - target.start).total_seconds())
            first = fragments[0]
            location = (
                first.location
                if task.station_id is not None
                and first.location.station_id == task.station_id
                else EvidenceLocation(
                    point=scope.target_node.point,
                    comid=scope.agent_comid,
                    station_id=None,
                    distance_to_target_km=0,
                )
            )
            fragment = EvidenceFragment(
                evidence_id=f"evidence:sha256:{sha256(identity.encode()).hexdigest()}",
                source="numeric_method",
                collection_status=CollectionStatus.AVAILABLE,
                target_comid=scope.agent_comid,
                location=location,
                time_range=target,
                parameter_refs=(task.output_parameter,),
                data_type="numeric_derived",
                applicability=EvidenceApplicability.ALLOWED_INDIRECT,
                relation_id=method.method_id,
                series_identity=task.series_identity,
                quality_class=EvidenceQualityClass.SUSPICIOUS,
                quality_flags=tuple(
                    sorted(
                        {
                            DataQualityFlag.ESTIMATED,
                            *(
                                flag
                                for item in fragments
                                for flag in item.quality_flags
                            ),
                        },
                        key=lambda item: item.value,
                    )
                ),
                coverage=EvidenceCoverage(
                    status=CoverageStatus.INSUFFICIENT,
                    observed_count=0,
                    covered_duration_seconds=0,
                    window_duration_seconds=seconds,
                ),
                payload=NumericDerivedPayload(
                    value=result_value,
                    unit=method.output_unit,
                    method_id=method.method_id,
                    method_version=method.version,
                    task_id=task.task_id,
                    plan_digest=plan.plan_digest,
                    plan_item_ref=plan_ref,
                    method_registry_digest=self._registry.manifest_digest,
                    input_evidence_refs=input_refs,
                    input_window_ids=tuple(item.window_id for item in task.inputs),
                    confidence_cap=confidence_cap,
                ),
                derivation=EvidenceDerivation(
                    algorithm=method.method_id,
                    algorithm_version=method.version,
                    policy_id=method.method_id,
                    registry_digest=self._registry.manifest_digest,
                ),
                provenance=Provenance(
                    operation_id=operation_id,
                    registry_digest=self._registry.manifest_digest,
                ),
                result_eligibility=EvidenceResultEligibility.INFERRED_ONLY,
            )
            scope.cancel_token.checkpoint()
            reference = await self._writer.write_fragment(
                scope, operation_id=operation_id, fragment=fragment
            )
            if await self._reader.read_fragment(scope, reference) != fragment:
                raise EvidenceScopeViolationError()
            return reference

    async def _execute_estimate(
        self, scope, plan, plan_ref, task, published, estimator
    ) -> EvidenceFragmentReference:
        method = self._registry.get(task.method_id, task.method_version)
        if (method is None or method.kind != "model_estimate"
                or method.output_parameter != task.output_parameter
                or method.output_unit != task.output_unit
                or task.mode not in method.allowed_modes):
            raise EvidenceScopeViolationError()
        input_refs = tuple(dict.fromkeys(
            item.evidence_ref or published[item.dependency_task_id]
            for item in task.inputs
            if item.evidence_ref is not None or item.dependency_task_id in published
        ))
        input_fragments = await self._reader.read_fragments(scope, input_refs)
        available_inputs = tuple((reference, fragment) for reference, fragment in zip(
            input_refs, input_fragments, strict=True
        ) if fragment.collection_status is CollectionStatus.AVAILABLE)
        input_refs = tuple(reference for reference, _ in available_inputs)
        input_fragments = tuple(fragment for _, fragment in available_inputs)
        context_fragments = await self._reader.read_fragments(scope, plan.source_fragment_refs)
        target = next(window.time_range for window in scope.time_windows
                      if window.window_id == task.window_id)
        candidates = list(zip(plan.source_fragment_refs, context_fragments, strict=True))
        candidates.sort(key=lambda item: _estimate_context_priority(
            item[1], task.output_parameter, target
        ))
        aggregates = tuple(fragment for _, fragment in candidates
                           if isinstance(fragment.payload, AggregatePayload))
        eligible = tuple((reference, fragment) for reference, fragment in candidates
                         if fragment.quality_class is not EvidenceQualityClass.INVALID
                         and not (isinstance(fragment.payload, DirectRecordPayload)
                                  and any(fragment.payload.record_ref in aggregate.payload.input_record_refs
                                          for aggregate in aggregates)))
        selected = list(eligible[:48])
        closure = {fragment.evidence_id: fragment for fragment in context_fragments}
        closure.update((fragment.evidence_id, fragment) for fragment in input_fragments)
        pending = list(closure.values())
        while pending:
            fragment = pending.pop()
            dependencies = (
                (fragment.payload.input_evidence_ref,)
                if isinstance(fragment.payload, ConversionPayload)
                else getattr(fragment.payload, "input_evidence_refs", ())
            )
            missing = tuple(ref for ref in dependencies if ref.evidence_id not in closure)
            if missing:
                loaded = await self._reader.read_fragments(scope, missing)
                closure.update((item.evidence_id, item) for item in loaded)
                pending.extend(loaded)
            if len(closure) > 20000:
                raise EvidenceScopeViolationError()
        full_anchor_ids = tuple(dict.fromkeys((
            *(ref.evidence_id for ref in input_refs),
            *(ref.evidence_id for ref in plan.source_fragment_refs),
        )))
        full_anchors = derive_measured_anchors(
            parameter=task.output_parameter, target_comid=scope.agent_comid,
            evidence_ids=full_anchor_ids, fragments=closure,
        )
        selected_ids = tuple(dict.fromkeys((
            *(ref.evidence_id for ref in input_refs),
            *(ref.evidence_id for ref, _ in selected),
        )))
        selected_anchors = derive_measured_anchors(
            parameter=task.output_parameter, target_comid=scope.agent_comid,
            evidence_ids=selected_ids, fragments=closure,
        )
        anchor_candidate = None
        if full_anchors:
            nearest = min(full_anchors, key=lambda anchor: (
                anchor.distance_km is None,
                anchor.distance_km if anchor.distance_km is not None else float("inf"),
                len(anchor.path_comids),
                anchor.model_dump_json(),
            ))
            if nearest not in selected_anchors:
                for candidate in eligible:
                    if nearest in derive_measured_anchors(
                        parameter=task.output_parameter,
                        target_comid=scope.agent_comid,
                        evidence_ids=(candidate[0].evidence_id,),
                        fragments=closure,
                    ):
                        anchor_candidate = candidate
                        if candidate not in selected:
                            if len(selected) == 48:
                                selected[-1] = candidate
                            else:
                                selected.append(candidate)
                        break
        related_candidate = next((candidate for candidate in eligible
                                  if task.output_parameter not in candidate[1].parameter_refs), None)
        if related_candidate is not None and related_candidate not in selected:
            if len(selected) == 48:
                replace_index = next(index for index in range(47, -1, -1)
                                     if selected[index] != anchor_candidate)
                selected[replace_index] = related_candidate
            else:
                selected.append(related_candidate)
        selected = tuple(selected)
        all_refs = tuple(dict.fromkeys((*input_refs, *(reference for reference, _ in selected))))
        all_fragments = await self._reader.read_fragments(scope, all_refs)
        anchors = derive_measured_anchors(
            parameter=task.output_parameter, target_comid=scope.agent_comid,
            evidence_ids=tuple(ref.evidence_id for ref in all_refs), fragments=closure,
        )
        result = await estimator(NumericEstimateRequest(
            scope=scope, task=task, plan_ref=plan_ref,
            input_refs=input_refs, input_fragments=input_fragments,
            context_refs=tuple(reference for reference, _ in selected),
            context_fragments=tuple(fragment for _, fragment in selected),
            measured_anchors=anchors,
        ))
        scope.cancel_token.checkpoint()
        identity = f"{plan.plan_digest}:{task.task_id}:" + ":".join(ref.evidence_id for ref in all_refs)
        operation_id = str(uuid5(NAMESPACE_URL, f"m5:numeric:{identity}"))
        seconds = int((target.end - target.start).total_seconds())
        common = {
            "evidence_id": f"evidence:sha256:{sha256(identity.encode()).hexdigest()}",
            "target_comid": scope.agent_comid,
            "location": EvidenceLocation(
                point=scope.target_node.point, comid=scope.agent_comid,
                station_id=None, distance_to_target_km=0,
            ),
            "time_range": target,
            "parameter_refs": (task.output_parameter,),
            "coverage": EvidenceCoverage(
                status=CoverageStatus.INSUFFICIENT, observed_count=0,
                covered_duration_seconds=0, window_duration_seconds=seconds,
            ),
            "provenance": Provenance(
                operation_id=operation_id,
                registry_digest=self._registry.manifest_digest,
            ),
        }
        if result.value is None and result.value_lower is None and result.value_upper is None:
            fragment = EvidenceFragment(
                **common, source="numeric_method", collection_status=CollectionStatus.EMPTY,
                data_type="collection_fact", applicability=EvidenceApplicability.CONTEXTUAL_ONLY,
                series_identity=f"numeric:{task.task_id}", quality_class=None,
                payload=CollectionFactPayload(status=CollectionStatus.EMPTY,
                    reason=f"model_estimate_insufficient_basis: {result.rationale}",
                    affected_window_ids=(task.window_id,)),
                result_eligibility=EvidenceResultEligibility.EXPLANATORY_ONLY,
            )
        else:
            if (task.output_parameter.code_system == "usgs"
                    and task.output_parameter.code in {"00400", "00403"}
                    and result.value_lower is not None and result.value_upper is not None
                    and Decimal(result.value_lower) <= 0 and Decimal(result.value_upper) >= 14):
                raise ValueError("a full pH scale is not an informative prediction")
            basis = ("same_parameter_measurement" if anchors else "related_evidence"
                     if any(fragment.collection_status is CollectionStatus.AVAILABLE
                            and not isinstance(fragment.payload, CollectionFactPayload)
                            for fragment in all_fragments) else "general_knowledge")
            inherited_flags = {
                flag
                for item in all_fragments
                if item.collection_status is CollectionStatus.AVAILABLE
                and item.quality_class in {
                    EvidenceQualityClass.VALID, EvidenceQualityClass.SUSPICIOUS,
                }
                and item.result_eligibility
                is not EvidenceResultEligibility.EXPLANATORY_ONLY
                for flag in item.quality_flags
            }
            fragment = EvidenceFragment(
                **common, source="numeric_method", collection_status=CollectionStatus.AVAILABLE,
                data_type="numeric_derived", applicability=EvidenceApplicability.ALLOWED_INDIRECT,
                relation_id=method.method_id, quality_class=EvidenceQualityClass.SUSPICIOUS,
                quality_flags=tuple(sorted(
                    {DataQualityFlag.ESTIMATED, *inherited_flags},
                    key=lambda flag: flag.value,
                )),
                payload=NumericDerivedPayload(
                    value=result.value, value_lower=result.value_lower, value_upper=result.value_upper,
                    method_kind="model_estimate", inference_basis=basis,
                    measured_anchors=anchors, rationale=result.rationale,
                    model_provider=result.model_provider, model_name=result.model_name,
                    model_operation_id=result.operation_id, prompt_digest=result.prompt_digest,
                    unit=method.output_unit, method_id=method.method_id, method_version=method.version,
                    task_id=task.task_id, plan_digest=plan.plan_digest, plan_item_ref=plan_ref,
                    method_registry_digest=self._registry.manifest_digest,
                    input_evidence_refs=all_refs,
                    input_window_ids=tuple(next((window.window_id for window in scope.time_windows
                                                if window.time_range == fragment.time_range), "context")
                                           for fragment in all_fragments),
                    confidence_cap=result.confidence,
                ),
                derivation=EvidenceDerivation(algorithm=method.method_id, algorithm_version=method.version,
                    policy_id=method.method_id, registry_digest=self._registry.manifest_digest),
                result_eligibility=EvidenceResultEligibility.INFERRED_ONLY,
            )
        reference = await self._writer.write_fragment(scope, operation_id=operation_id, fragment=fragment)
        if await self._reader.read_fragment(scope, reference) != fragment:
            raise EvidenceScopeViolationError()
        return reference

    async def _execute_temporal(
        self,
        scope: EvidenceSessionScope,
        plan: NumericDependencyPlan,
        plan_ref: ItemReference,
        task: NumericPlanTask,
        candidate: TemporalCandidate,
    ) -> EvidenceFragmentReference:
        if self._capability_registry is None:
            raise EvidenceScopeViolationError()
        _, original = build_temporal_fragment(
            scope,
            candidate,
            registry_digest=self._capability_registry.manifest_digest,
        )
        identity = f"{plan.plan_digest}:{task.task_id}:{original.evidence_id}"
        operation_id = str(uuid5(NAMESPACE_URL, f"m5:planned-temporal:{identity}"))
        payload = original.payload.model_copy(
            update={
                "plan_item_ref": plan_ref,
                "task_id": task.task_id,
                "plan_digest": plan.plan_digest,
                "method_registry_digest": self._registry.manifest_digest,
            }
        )
        fragment = EvidenceFragment.model_validate(
            original.model_copy(
                update={
                    "evidence_id": (
                        f"evidence:sha256:{sha256(identity.encode()).hexdigest()}"
                    ),
                    "payload": payload,
                    "provenance": original.provenance.model_copy(
                        update={"operation_id": operation_id}
                    ),
                }
            ).model_dump(mode="python")
        )
        scope.cancel_token.checkpoint()
        reference = await self._writer.write_fragment(
            scope, operation_id=operation_id, fragment=fragment
        )
        if await self._reader.read_fragment(scope, reference) != fragment:
            raise EvidenceScopeViolationError()
        return reference


def _release_children(
    task_id: str,
    children: dict[str, list[str]],
    remaining: dict[str, int],
    ready: list[str],
) -> None:
    for child in children[task_id]:
        remaining[child] -= 1
        if remaining[child] == 0:
            heapq.heappush(ready, child)


def _task_id(scope, method, window_id, station_id, series_identity) -> str:
    identity = "|".join(
        (
            scope.run_id,
            scope.session_id,
            scope.agent_comid,
            method.output_parameter.code_system,
            method.output_parameter.code,
            window_id,
            method.method_id,
            str(method.version),
            station_id or "",
            series_identity or "",
        )
    )
    return f"numeric:sha256:{sha256(identity.encode()).hexdigest()}"


def _seed_priority(fragment: EvidenceFragment) -> int:
    if isinstance(fragment.payload, AggregatePayload):
        return 0
    if isinstance(fragment.payload, TemporalEstimatePayload):
        return 1
    if isinstance(fragment.payload, NumericDerivedPayload):
        return 2
    return 3


def _seed_matches_input(
    fragment: EvidenceFragment, required: NumericMethodInput | None
) -> bool:
    if required is None:
        return True
    if isinstance(fragment.payload, NumericDerivedPayload) and fragment.payload.value is None:
        return False
    if required.required_payload_type is not None and (
        fragment.payload.payload_type != required.required_payload_type
    ):
        return False
    if not required.allow_estimated and (
        isinstance(fragment.payload, (TemporalEstimatePayload, NumericDerivedPayload))
        or DataQualityFlag.ESTIMATED in fragment.quality_flags
    ):
        return False
    return numeric_fragment_value(fragment)[1] == required.unit


def _derived_matches_input(
    method: NumericMethod, required: NumericMethodInput | None
) -> bool:
    return required is None or (
        required.allow_estimated
        and required.unit == method.output_unit
        and required.required_payload_type in {None, "numeric_derived"}
    )


def _temporal_matches_input(
    method: NumericMethod, required: NumericMethodInput | None
) -> bool:
    return required is None or (
        required.allow_estimated
        and required.unit == method.output_unit
        and required.required_payload_type in {None, "temporal_estimate"}
    )


def _is_temporal_method(method: NumericMethod) -> bool:
    return method.production and method.method_id.startswith("temporal_gap_fill_")


def _temporal_task(
    scope: EvidenceSessionScope,
    registry: NumericMethodRegistry,
    candidate: TemporalCandidate,
) -> NumericPlanTask | None:
    marker = (
        "temporal_gap_fill_linear_v1_"
        if candidate.method == "linear_interpolation"
        else "temporal_gap_fill_hold_v1_"
    )
    method = next(
        (
            item
            for item in registry.for_output(candidate.parameter)
            if item.production
            and item.method_id.startswith(marker)
            and item.output_unit == candidate.unit
        ),
        None,
    )
    if method is None or len(method.inputs) != len(candidate.anchors):
        return None
    identity = "|".join(
        (
            _task_id(
                scope,
                method,
                candidate.window_id,
                candidate.station_id,
                candidate.series_identity,
            ),
            candidate.source,
            *(anchor.reference.evidence_id for anchor in candidate.anchors),
        )
    )
    return NumericPlanTask(
        task_id=f"numeric:sha256:{sha256(identity.encode()).hexdigest()}",
        target_comid=scope.agent_comid,
        output_parameter=candidate.parameter,
        output_unit=candidate.unit,
        window_id=candidate.window_id,
        mode=candidate.window.window_kind.value,
        method_id=method.method_id,
        method_version=method.version,
        station_id=candidate.station_id,
        series_identity=candidate.series_identity,
        inputs=tuple(
            NumericPlanInput(
                parameter=candidate.parameter,
                unit=candidate.unit,
                window_id=anchor.window_id,
                evidence_ref=anchor.reference,
            )
            for anchor in candidate.anchors
        ),
    )


def _candidate_window_ids(
    windows: dict[str, EvidenceWindow],
    target_id: str,
    relation: str,
) -> tuple[str, ...]:
    target = windows[target_id].time_range
    middle = target.start + (target.end - target.start) / 2
    candidates = (
        (
            abs(
                (
                    window.time_range.start
                    + (window.time_range.end - window.time_range.start) / 2
                    - middle
                ).total_seconds()
            ),
            window_id,
        )
        for window_id, window in windows.items()
        if window_id != target_id
        and _window_relation_matches(relation, window.time_range, target)
    )
    return tuple(window_id for _, window_id in sorted(candidates))


def _usable_numeric(fragment: EvidenceFragment, target_comid: str) -> bool:
    return (
        fragment.target_comid == target_comid
        and fragment.location.comid == target_comid
        and fragment.collection_status is CollectionStatus.AVAILABLE
        and fragment.quality_class
        in {
            EvidenceQualityClass.VALID,
            EvidenceQualityClass.SUSPICIOUS,
        }
        and fragment.result_eligibility
        is not EvidenceResultEligibility.EXPLANATORY_ONLY
        and len(fragment.parameter_refs) == 1
        and isinstance(
            fragment.payload,
            (
                DirectRecordPayload,
                AggregatePayload,
                ConversionPayload,
                TemporalEstimatePayload,
                NumericDerivedPayload,
            ),
        )
    )


def numeric_fragment_value(fragment: EvidenceFragment) -> tuple[Decimal, str]:
    payload = fragment.payload
    if isinstance(payload, DirectRecordPayload):
        return Decimal(payload.displayed_value), payload.displayed_unit
    if isinstance(
        payload,
        (
            AggregatePayload,
            ConversionPayload,
            TemporalEstimatePayload,
            NumericDerivedPayload,
        ),
    ):
        return Decimal(payload.value), payload.unit
    raise EvidenceScopeViolationError()


def _numeric_fragment_unit(fragment: EvidenceFragment) -> str:
    return (fragment.payload.displayed_unit if isinstance(fragment.payload, DirectRecordPayload)
            else fragment.payload.unit)


def _estimate_context_priority(fragment, parameter, target):
    midpoint = target.start + (target.end - target.start) / 2
    source_midpoint = fragment.time_range.start + (fragment.time_range.end - fragment.time_range.start) / 2
    return (
        0 if parameter in fragment.parameter_refs else 1,
        0 if fragment.collection_status is CollectionStatus.AVAILABLE else 1,
        abs((source_midpoint - midpoint).total_seconds()),
        0 if isinstance(fragment.payload, (AggregatePayload, UpstreamResultPayload)) else 1,
        fragment.evidence_id,
    )


def numeric_confidence_cap(
    method: NumericMethod,
    inputs: tuple[EvidenceFragment, ...],
    target: TimeRange,
) -> float:
    cap = method.confidence_cap
    for fragment in inputs:
        if isinstance(fragment.payload, NumericDerivedPayload):
            cap = min(cap, fragment.payload.confidence_cap)
        elif isinstance(fragment.payload, TemporalEstimatePayload):
            payload = fragment.payload
            window_seconds = (
                fragment.time_range.end - fragment.time_range.start
            ).total_seconds()
            base = 0.75 if payload.method == "linear_interpolation" else 0.55
            temporal_cap = round(
                base
                * min(
                    1.0,
                    window_seconds / max(payload.anchor_distance_seconds),
                ),
                6,
            )
            cap = min(cap, temporal_cap)
    if method.production and method.method_id.startswith("temporal_gap_fill_"):
        middle = target.start + (target.end - target.start) / 2
        duration = (target.end - target.start).total_seconds()
        distance = max(
            abs(
                (
                    fragment.time_range.start
                    + (fragment.time_range.end - fragment.time_range.start) / 2
                    - middle
                ).total_seconds()
            )
            for fragment in inputs
        )
        cap = min(cap, 0.6 / (1 + distance / duration))
    return float(cap)


def _window_relation_matches(
    relation: str,
    source: TimeRange,
    target: TimeRange,
) -> bool:
    if relation == "same_window":
        return source == target
    if relation == "nearest_before":
        return source.end <= target.start
    if relation == "nearest_after":
        return source.start >= target.end
    if relation == "nearest_anchor":
        return source.end <= target.start or source.start >= target.end
    return False


def _select_anchor(
    scope, saved, method, window_id, relation, *, station_id, series_identity
):
    windows = {item.window_id: item for item in scope.time_windows}
    target = windows[window_id].time_range
    middle = target.start + (target.end - target.start) / 2
    candidates = []
    for (parameter, candidate_window_id), pairs in saved.items():
        if parameter != method.output_parameter or candidate_window_id == window_id:
            continue
        candidate_window = windows[candidate_window_id]
        if candidate_window.window_kind is not InferenceMode.GAP_FILLING:
            continue
        for reference, fragment in pairs:
            if (
                not isinstance(fragment.payload, AggregatePayload)
                or fragment.source != "deterministic_aggregation"
                or fragment.location.station_id is None
                or fragment.series_identity is None
                or fragment.payload.unit != method.output_unit
                or (
                    station_id is not None
                    and fragment.location.station_id != station_id
                )
                or (
                    series_identity is not None
                    and fragment.series_identity != series_identity
                )
            ):
                continue
            candidate_middle = (
                fragment.time_range.start
                + (fragment.time_range.end - fragment.time_range.start) / 2
            )
            delta = (candidate_middle - middle).total_seconds()
            if relation == "nearest_before" and delta >= 0:
                continue
            if relation == "nearest_after" and delta <= 0:
                continue
            candidates.append(
                (
                    abs(delta),
                    candidate_middle,
                    fragment.location.station_id,
                    fragment.series_identity,
                    candidate_window_id,
                    reference,
                    fragment,
                )
            )
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[2], item[3], item[4]))
    _, _, _, _, candidate_window_id, reference, fragment = candidates[0]
    return candidate_window_id, reference, fragment

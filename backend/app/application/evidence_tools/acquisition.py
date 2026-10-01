from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256

from app.application.evidence_tools.bundle_builder import EvidenceBundleBuilder
from app.application.evidence_tools.errors import EvidenceScopeViolationError
from app.application.evidence_tools.executor import EvidenceToolExecutor
from app.application.evidence_tools.models import (
    EvidenceClock,
    EvidencePolicyClass,
    EvidenceToolDescriptor,
    EvidenceToolExecution,
    EvidenceToolReader,
    EvidenceToolWriter,
)
from app.application.evidence_tools.registry import EvidenceToolRegistry
from app.application.evidence_tools.scope import EvidenceSessionScope
from app.application.evidence_tools.relation_materializer import (
    EvidenceRelationMaterializer,
)
from app.application.evidence_tools.evidence_sources import (
    weather_capable_parameters,
)
from app.domain import (
    CollectionFactPayload,
    CollectionStatus,
    CoverageStatus,
    EvidenceApplicability,
    EvidenceCell,
    EvidenceCellRole,
    EvidenceCellStatus,
    EvidenceCoverage,
    EvidenceFragment,
    EvidenceQualityClass,
    EvidenceResultEligibility,
    EvidenceToolExecutionStatus,
    EvidenceToolRequest,
    EvidenceToolOptions,
    EvidenceLocation,
    EvidenceFragmentReference,
    ItemReference,
    ParameterKey,
    ParameterCapabilityRegistry,
    Provenance,
    InferenceMode,
)


_DISCHARGE = ParameterKey(code_system="usgs", code="00060")


class _UtcClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class EvidenceAcquisitionCellGate:
    parameter: ParameterKey
    window_id: str
    target_status: EvidenceCellStatus
    discharge_status: EvidenceCellStatus
    status: EvidenceCellStatus
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EvidenceAcquisitionHandoff:
    bundle_ref: ItemReference
    cells: tuple[EvidenceCell, ...]
    read_port: EvidenceToolReader
    tool_roster: tuple[EvidenceToolDescriptor, ...]
    executor: EvidenceToolExecutor
    cell_gates: tuple[EvidenceAcquisitionCellGate, ...]
    stop_before_model: bool
    stop_facts: tuple[str, ...]
    block_facts: tuple[str, ...]


class EvidenceAcquisitionService:
    """Build the evidence input for one already-existing waterbody session."""

    def __init__(
        self,
        *,
        registry: EvidenceToolRegistry,
        executor: EvidenceToolExecutor,
        reader: EvidenceToolReader,
        writer: EvidenceToolWriter,
        capability_registry: ParameterCapabilityRegistry,
        clock: EvidenceClock | None = None,
    ) -> None:
        self._registry = registry
        self._executor = executor
        self._reader = reader
        self._writer = writer
        self._clock = clock or _UtcClock()
        self._bundle_builder = EvidenceBundleBuilder(reader=reader, writer=writer)
        self._relation_materializer = EvidenceRelationMaterializer(
            registry=capability_registry,
            reader=reader,
            writer=writer,
        )

    async def acquire(
        self,
        scope: EvidenceSessionScope,
        *,
        optional_requests: tuple[EvidenceToolRequest, ...] = (),
    ) -> EvidenceAcquisitionHandoff:
        self._validate_optional_requests(scope, optional_requests)
        fragment_refs: list[EvidenceFragmentReference] = []
        result_refs: list[ItemReference] = []
        block_facts: list[str] = []

        if scope.station_refs:
            required = await self._execute(
                scope,
                self._request(scope, "collect_water_observations"),
                fragment_refs=fragment_refs,
                result_refs=result_refs,
                block_facts=block_facts,
                materialize_technical_facts=True,
            )
            completed = (
                required.result.execution_status
                is EvidenceToolExecutionStatus.COMPLETED
            )
            has_direct = await self._has_usable_direct(
                scope,
                required.result.fragment_refs,
            )
            has_rules = bool(scope.evidence_rule_plan.effective_rules)
            stop_before_model = completed and not has_direct and not has_rules
            run_optional = completed and not stop_before_model
        else:
            upstream = await self._execute(
                scope,
                self._request(scope, "get_upstream_results"),
                fragment_refs=fragment_refs,
                result_refs=result_refs,
                block_facts=block_facts,
                materialize_technical_facts=True,
            )
            stop_before_model = False
            run_optional = (
                upstream.result.execution_status
                is not EvidenceToolExecutionStatus.CANCELLED
            )

        if run_optional:
            for request in (
                *self._required_rule_requests(scope),
                *optional_requests,
            ):
                execution = await self._execute(
                    scope,
                    request,
                    fragment_refs=fragment_refs,
                    result_refs=result_refs,
                    block_facts=block_facts,
                    materialize_technical_facts=False,
                )
                if (
                    execution.result.execution_status
                    is EvidenceToolExecutionStatus.CANCELLED
                ):
                    break

        fragment_refs.extend(
            await self._relation_materializer.materialize(
                scope,
                _unique_fragment_refs(fragment_refs),
                operation_id=f"m5:materialize-relations:{scope.session_id}",
            )
        )
        if (
            scope.station_refs
            and not stop_before_model
            and scope.evidence_rule_plan.effective_rules
        ):
            stop_before_model = not await self._has_usable_target_evidence(
                scope,
                _unique_fragment_refs(fragment_refs),
            )

        bundle, bundle_ref = await self._bundle_builder.build_and_write(
            scope,
            fragment_refs=_unique_fragment_refs(fragment_refs),
            tool_result_refs=_unique_item_refs(result_refs),
            created_at=self._clock.now(),
        )
        return EvidenceAcquisitionHandoff(
            bundle_ref=bundle_ref,
            cells=bundle.cells,
            read_port=self._reader,
            tool_roster=self._registry.roster(scope),
            executor=self._executor,
            cell_gates=_cell_gates(bundle.cells),
            stop_before_model=stop_before_model,
            stop_facts=(
                ("station_backed_no_usable_direct_evidence",)
                if stop_before_model
                else ()
            ),
            block_facts=tuple(dict.fromkeys(block_facts)),
        )

    async def _execute(
        self,
        scope: EvidenceSessionScope,
        request: EvidenceToolRequest,
        *,
        fragment_refs: list[EvidenceFragmentReference],
        result_refs: list[ItemReference],
        block_facts: list[str],
        materialize_technical_facts: bool,
    ) -> EvidenceToolExecution:
        execution = await self._executor.execute_tracked(scope, request)
        result = execution.result
        result_refs.append(execution.result_ref)
        fragment_refs.extend(result.fragment_refs)
        fragment_refs.extend(result.collection_fact_refs)
        if result.execution_status is not EvidenceToolExecutionStatus.COMPLETED:
            reason = (
                result.safe_error.code
                if result.safe_error is not None
                else "evidence_operation_failed"
            )
            block_facts.append(reason)
            if materialize_technical_facts:
                fragment_refs.extend(
                    await self._write_technical_facts(
                        scope,
                        operation_id=result.operation_id,
                        reason=reason,
                        existing_refs=(
                            *result.fragment_refs,
                            *result.collection_fact_refs,
                        ),
                    )
                )
        return execution

    async def _write_technical_facts(
        self,
        scope: EvidenceSessionScope,
        *,
        operation_id: str,
        reason: str,
        existing_refs: tuple[EvidenceFragmentReference, ...],
    ) -> tuple[EvidenceFragmentReference, ...]:
        covered = set()
        for reference in existing_refs:
            fragment = await self._reader.read_fragment(scope, reference)
            for parameter in fragment.parameter_refs:
                for window in scope.time_windows:
                    if fragment.time_range.overlaps(window.time_range):
                        covered.add((parameter, window.window_id))
        references = []
        for window in scope.time_windows:
            seconds = int(
                (window.time_range.end - window.time_range.start).total_seconds()
            )
            for parameter in (*scope.requested_parameters, *scope.supporting_parameters):
                if (parameter, window.window_id) in covered:
                    continue
                identity = (
                    f"{operation_id}:{parameter.code_system}:{parameter.code}:"
                    f"{window.window_id}:{reason}"
                )
                fragment = EvidenceFragment(
                    evidence_id=f"evidence:sha256:{sha256(identity.encode()).hexdigest()}",
                    source="m5_acquisition",
                    collection_status=CollectionStatus.UNAVAILABLE,
                    target_comid=scope.agent_comid,
                    location=EvidenceLocation(
                        point=scope.target_node.point,
                        comid=scope.agent_comid,
                        station_id=None,
                        distance_to_target_km=0,
                    ),
                    time_range=window.time_range,
                    parameter_refs=(parameter,),
                    data_type="collection_fact",
                    applicability=EvidenceApplicability.CONTEXTUAL_ONLY,
                    series_identity=f"acquisition:{parameter.code_system}:{parameter.code}",
                    quality_class=None,
                    coverage=EvidenceCoverage(
                        status=CoverageStatus.UNKNOWN,
                        observed_count=0,
                        covered_duration_seconds=0,
                        window_duration_seconds=seconds,
                    ),
                    payload=CollectionFactPayload(
                        status=CollectionStatus.UNAVAILABLE,
                        reason=reason,
                        affected_window_ids=(window.window_id,),
                    ),
                    provenance=Provenance(operation_id=operation_id),
                    result_eligibility=EvidenceResultEligibility.EXPLANATORY_ONLY,
                )
                references.append(
                    await self._writer.write_fragment(
                        scope,
                        operation_id=operation_id,
                        fragment=fragment,
                    )
                )
        return tuple(references)

    async def _has_usable_direct(
        self,
        scope: EvidenceSessionScope,
        references: tuple[EvidenceFragmentReference, ...],
    ) -> bool:
        for reference in references:
            fragment = await self._reader.read_fragment(scope, reference)
            if (
                fragment.applicability is EvidenceApplicability.DIRECT_MEASUREMENT
                and fragment.quality_class
                in {EvidenceQualityClass.VALID, EvidenceQualityClass.SUSPICIOUS}
            ):
                return True
        return False

    async def _has_usable_target_evidence(
        self,
        scope: EvidenceSessionScope,
        references: tuple[EvidenceFragmentReference, ...],
    ) -> bool:
        target_parameters = set(scope.requested_parameters)
        for reference in references:
            fragment = await self._reader.read_fragment(scope, reference)
            if (
                target_parameters.intersection(fragment.parameter_refs)
                and fragment.applicability
                in {
                    EvidenceApplicability.DIRECT_MEASUREMENT,
                    EvidenceApplicability.ALLOWED_INDIRECT,
                }
                and fragment.quality_class
                in {EvidenceQualityClass.VALID, EvidenceQualityClass.SUSPICIOUS}
            ):
                return True
        return False

    @staticmethod
    def _required_rule_requests(
        scope: EvidenceSessionScope,
    ) -> tuple[EvidenceToolRequest, ...]:
        parameters = weather_capable_parameters(
            tuple(
                dict.fromkeys(
                    parameter
                    for rule in scope.evidence_rule_plan.effective_rules
                    if rule.required_collection_attempt
                    for parameter in rule.input_parameters
                )
            )
        )
        if not parameters:
            return ()
        return (
            EvidenceToolRequest(
                tool_call_id=f"m5:required-rule-weather:{scope.session_id}",
                session_id=scope.session_id,
                tool_name="collect_weather_evidence",
                purpose="Collect weather inputs required by the inherited evidence rules.",
                window_ids=tuple(item.window_id for item in scope.time_windows),
                parameter_keys=parameters,
                options=EvidenceToolOptions(
                    include_forecast=scope.mode is not InferenceMode.GAP_FILLING,
                ),
            ),
        )

    def _request(
        self,
        scope: EvidenceSessionScope,
        tool_name: str,
    ) -> EvidenceToolRequest:
        return EvidenceToolRequest(
            tool_call_id=f"m5:{tool_name}:{scope.session_id}",
            session_id=scope.session_id,
            tool_name=tool_name,
            purpose=(
                "Collect the current waterbody's required local observations."
                if tool_name == "collect_water_observations"
                else "Read converged direct-upstream results for this waterbody."
            ),
            window_ids=tuple(item.window_id for item in scope.time_windows),
            parameter_keys=tuple(
                dict.fromkeys(
                    (*scope.requested_parameters, *scope.supporting_parameters)
                    + tuple(
                        key
                        for rule in scope.evidence_rule_plan.effective_rules
                        for key in rule.input_parameters
                    )
                )
            ),
        )

    def _validate_optional_requests(
        self,
        scope: EvidenceSessionScope,
        requests: tuple[EvidenceToolRequest, ...],
    ) -> None:
        call_ids = tuple(request.tool_call_id for request in requests)
        if len(call_ids) != len(set(call_ids)):
            raise EvidenceScopeViolationError()
        for request in requests:
            descriptor = self._registry.get(request.tool_name)
            if (
                request.session_id != scope.session_id
                or descriptor is None
                or descriptor.policy_class is not EvidencePolicyClass.OPTIONAL
            ):
                raise EvidenceScopeViolationError()


def _cell_gates(
    cells: tuple[EvidenceCell, ...],
) -> tuple[EvidenceAcquisitionCellGate, ...]:
    by_identity = {
        (cell.parameter, cell.window_id, cell.role): cell for cell in cells
    }
    discharge_by_window = {
        cell.window_id: cell
        for cell in cells
        if cell.role is EvidenceCellRole.REQUIRED_SUPPORT
        and cell.parameter == _DISCHARGE
    }
    target_cells = tuple(
        cell for cell in cells if cell.role is EvidenceCellRole.TARGET
    )
    if len(discharge_by_window) != len({cell.window_id for cell in target_cells}):
        raise EvidenceScopeViolationError()

    gates = []
    for target in target_cells:
        discharge = discharge_by_window[target.window_id]
        statuses = {target.status, discharge.status}
        status = (
            EvidenceCellStatus.BLOCKED
            if EvidenceCellStatus.BLOCKED in statuses
            else EvidenceCellStatus.SUPPORTED
            if statuses == {EvidenceCellStatus.SUPPORTED}
            else EvidenceCellStatus.UNSUPPORTED
        )
        reasons = []
        if target.status is not EvidenceCellStatus.SUPPORTED:
            reasons.extend(f"target:{reason}" for reason in target.reasons)
        if discharge.status is not EvidenceCellStatus.SUPPORTED:
            reasons.extend(
                f"required_discharge:{reason}" for reason in discharge.reasons
            )
        gates.append(
            EvidenceAcquisitionCellGate(
                parameter=target.parameter,
                window_id=target.window_id,
                target_status=target.status,
                discharge_status=discharge.status,
                status=status,
                reasons=tuple(dict.fromkeys(reasons)),
            )
        )
    if len(by_identity) != len(cells):
        raise EvidenceScopeViolationError()
    return tuple(gates)


def _unique_fragment_refs(
    references: list[EvidenceFragmentReference],
) -> tuple[EvidenceFragmentReference, ...]:
    return tuple(dict.fromkeys(references))


def _unique_item_refs(references: list[ItemReference]) -> tuple[ItemReference, ...]:
    return tuple(dict.fromkeys(references))

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from app.application.evidence_tools.bundle_builder import (
    EvidenceBundleBuilder,
    ValidatedBundleInputs,
)
from app.application.evidence_tools.errors import EvidenceScopeViolationError
from app.application.evidence_tools.executor import EvidenceToolExecutor
from app.application.evidence_tools.models import (
    EvidenceClock,
    EvidenceBudgetUsage,
    EvidencePolicyClass,
    EvidenceProducerFact,
    EvidenceToolDescriptor,
    EvidenceToolExecution,
    EvidenceToolReader,
    EvidenceToolWriter,
)
from app.application.evidence_tools.registry import EvidenceToolRegistry
from app.application.evidence_tools.scope import (
    EvidenceSessionScope,
    with_evidence_rule_plan,
)
from app.application.evidence_tools.relation_materializer import (
    EvidenceRelationMaterializer,
)
from app.application.evidence_tools.evidence_sources import (
    weather_capable_parameters,
)
from app.application.query_understanding import (
    EvidenceRule,
    EvidenceRuleOrigin,
    EvidenceRulePlan,
    append_evidence_rules,
)
from app.domain import (
    CollectionFactPayload,
    CollectionStatus,
    CoverageStatus,
    EvidenceApplicability,
    EvidenceBundle,
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
    InferenceMode,
    ItemReference,
    ParameterKey,
    ParameterCapabilityRegistry,
    Provenance,
    UpstreamResultPayload,
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
    run_id: str
    session_id: str
    bundle_ref: ItemReference
    bundle: EvidenceBundle
    validated_inputs: ValidatedBundleInputs
    bundle_binding_digest: str
    rule_plan: EvidenceRulePlan
    rule_plan_digest: str
    capability_registry_digest: str
    cells: tuple[EvidenceCell, ...]
    collection_fact_refs: tuple[EvidenceFragmentReference, ...]
    fragment_allowlist: tuple[EvidenceFragmentReference, ...]
    source_record_allowlist: tuple[str, ...]
    tool_result_refs: tuple[ItemReference, ...]
    budget_usage: EvidenceBudgetUsage
    producer_facts: tuple[EvidenceProducerFact, ...]
    read_port: EvidenceToolReader
    tool_roster: tuple[EvidenceToolDescriptor, ...]
    executor: EvidenceToolExecutor
    cell_gates: tuple[EvidenceAcquisitionCellGate, ...]
    usable_upstream_comids: tuple[str, ...]
    stop_before_model: bool
    stop_facts: tuple[str, ...]
    block_facts: tuple[str, ...]

    def has_valid_bundle_binding(self) -> bool:
        return self.bundle_binding_digest == _bundle_binding_digest(
            self.bundle_ref,
            self.bundle,
        )


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
        self._capability_registry = capability_registry
        self._capability_registry_digest = capability_registry.manifest_digest
        self._bundle_builder = EvidenceBundleBuilder(reader=reader, writer=writer)
        self._relation_materializer = EvidenceRelationMaterializer(
            registry=capability_registry,
            reader=reader,
            writer=writer,
        )

    async def start(
        self,
        scope: EvidenceSessionScope,
    ) -> EvidenceAcquisitionHandoff:
        """Run the deterministic M5 preflight and persist its first bundle."""

        fragment_refs: list[EvidenceFragmentReference] = []
        result_refs: list[ItemReference] = []
        block_facts: list[str] = []

        cancelled = False
        if scope.station_refs:
            local = await self._execute(
                scope,
                self._request(scope, "collect_water_observations"),
                fragment_refs=fragment_refs,
                result_refs=result_refs,
                block_facts=block_facts,
                materialize_technical_facts=True,
            )
            cancelled = (
                local.result.execution_status
                is EvidenceToolExecutionStatus.CANCELLED
            )

        if scope.direct_upstream_comids and not cancelled:
            upstream = await self._execute(
                scope,
                self._request(scope, "get_upstream_results"),
                fragment_refs=fragment_refs,
                result_refs=result_refs,
                block_facts=block_facts,
                materialize_technical_facts=True,
            )
            cancelled = (
                upstream.result.execution_status
                is EvidenceToolExecutionStatus.CANCELLED
            )

        local_target = await self._has_usable_direct(
            scope,
            tuple(fragment_refs),
            parameters=scope.requested_parameters,
        )
        local_discharge = await self._has_usable_direct(
            scope,
            tuple(fragment_refs),
            parameters=scope.supporting_parameters,
        )
        usable_upstream = await self._has_usable_upstream(
            scope,
            tuple(fragment_refs),
        )
        has_starting_evidence = (
            local_target
            or (usable_upstream and (not scope.station_refs or local_discharge))
        )
        stop_before_model = (
            not cancelled and not block_facts and not has_starting_evidence
        )
        run_optional = not cancelled and has_starting_evidence

        if run_optional:
            for request in self._required_rule_requests(scope):
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

        return await self._build_handoff(
            scope,
            fragment_refs=tuple(fragment_refs),
            result_refs=tuple(result_refs),
            block_facts=tuple(block_facts),
            stop_before_model=stop_before_model,
            stop_facts=(
                (
                    "upstream_dependencies_insufficient"
                    if not scope.station_refs
                    else "no_usable_local_or_upstream_evidence"
                ),
            )
            if stop_before_model
            else (),
        )

    async def execute_optional(
        self,
        scope: EvidenceSessionScope,
        requests: tuple[EvidenceToolRequest, ...],
        prior: EvidenceAcquisitionHandoff,
        *,
        max_calls: int = 4,
    ) -> EvidenceAcquisitionHandoff:
        """Execute one bounded Agent-selected tool turn in provider order."""

        current_scope = self._scope_for_handoff(scope, prior)
        self._validate_optional_requests(
            current_scope,
            requests,
            prior=prior,
            max_calls=max_calls,
        )
        fragment_refs = list(prior.fragment_allowlist)
        result_refs = list(prior.tool_result_refs)
        block_facts = list(prior.block_facts)
        for request in requests:
            self._validate_optional_request(current_scope, request, prior)
            execution = await self._execute(
                current_scope,
                request,
                fragment_refs=fragment_refs,
                result_refs=result_refs,
                block_facts=block_facts,
                materialize_technical_facts=False,
                agent_selected=True,
            )
            if (
                execution.result.execution_status
                is EvidenceToolExecutionStatus.CANCELLED
            ):
                break
        return await self._build_handoff(
            current_scope,
            fragment_refs=tuple(fragment_refs),
            result_refs=tuple(result_refs),
            block_facts=tuple(block_facts),
            stop_before_model=prior.stop_before_model,
            stop_facts=prior.stop_facts,
        )

    async def append_rules(
        self,
        scope: EvidenceSessionScope,
        rules: tuple[EvidenceRule, ...],
        prior: EvidenceAcquisitionHandoff,
    ) -> EvidenceAcquisitionHandoff:
        """Append validated Agent rules and persist a rebuilt immutable bundle."""

        current_scope = self._scope_for_handoff(scope, prior)
        if not rules:
            raise EvidenceScopeViolationError()
        allowed_outputs = set(
            (*scope.requested_parameters, *scope.supporting_parameters)
        )
        for rule in rules:
            if (
                rule.origin is not EvidenceRuleOrigin.WATERBODY_AGENT
                or rule.origin_session_id != scope.session_id
                or rule.required_collection_attempt
                or rule.output_parameter not in allowed_outputs
                or self._capability_registry.get(rule.output_parameter) is None
                or any(
                    self._capability_registry.get(parameter) is None
                    for parameter in rule.input_parameters
                )
            ):
                raise EvidenceScopeViolationError()
        try:
            plan = append_evidence_rules(current_scope.evidence_rule_plan, rules)
        except ValueError as error:
            raise EvidenceScopeViolationError() from error
        next_scope = with_evidence_rule_plan(current_scope, plan)
        return await self._build_handoff(
            next_scope,
            fragment_refs=prior.fragment_allowlist,
            result_refs=prior.tool_result_refs,
            block_facts=prior.block_facts,
            stop_before_model=prior.stop_before_model,
            stop_facts=prior.stop_facts,
        )

    def seal(
        self,
        scope: EvidenceSessionScope,
        handoff: EvidenceAcquisitionHandoff,
    ) -> EvidenceAcquisitionHandoff:
        """Validate and return the only handoff eligible for reasoning sealing."""

        self._scope_for_handoff(scope, handoff)
        return handoff

    def planning_roster(
        self,
        scope: EvidenceSessionScope,
        handoff: EvidenceAcquisitionHandoff,
    ) -> tuple[EvidenceToolDescriptor, ...]:
        current_scope = self._scope_for_handoff(scope, handoff)
        usage = self._executor.budget_usage(current_scope)
        visible: list[EvidenceToolDescriptor] = []
        for descriptor in self._registry.roster(current_scope):
            agent_selectable_producer = (
                descriptor.policy_class is EvidencePolicyClass.PRODUCER_SLOT
                and descriptor.name == "search_transcript_evidence"
            )
            if (
                descriptor.policy_class
                not in {
                    EvidencePolicyClass.OPTIONAL,
                    EvidencePolicyClass.DETERMINISTIC,
                }
                and not agent_selectable_producer
            ):
                continue
            if (
                descriptor.policy_class is EvidencePolicyClass.OPTIONAL
                and usage.collection_used >= usage.collection_limit
            ) or (
                descriptor.policy_class is EvidencePolicyClass.DETERMINISTIC
                and usage.transform_used >= usage.transform_limit
            ):
                continue
            if descriptor.name == "collect_facility_evidence" and scope.geometry_ref is None:
                continue
            if descriptor.name == "convert_evidence_unit" and not handoff.fragment_allowlist:
                continue
            if descriptor.name in {
                "aggregate_evidence",
                "inspect_evidence_records",
            } and not handoff.source_record_allowlist:
                continue
            visible.append(descriptor)
        return tuple(visible)

    async def _build_handoff(
        self,
        scope: EvidenceSessionScope,
        *,
        fragment_refs: tuple[EvidenceFragmentReference, ...],
        result_refs: tuple[ItemReference, ...],
        block_facts: tuple[str, ...],
        stop_before_model: bool,
        stop_facts: tuple[str, ...],
    ) -> EvidenceAcquisitionHandoff:
        mutable_fragments = list(fragment_refs)

        mutable_fragments.extend(
            await self._relation_materializer.materialize(
                scope,
                _unique_fragment_refs(mutable_fragments),
                operation_id=str(uuid5(
                    NAMESPACE_URL,
                    f"m5:materialize-relations:{scope.run_id}:{scope.session_id}",
                )),
            )
        )
        mutable_fragments.extend(
            await self._write_missing_cell_facts(
                scope,
                _unique_fragment_refs(mutable_fragments),
            )
        )

        built = await self._bundle_builder.build_and_write(
            scope,
            fragment_refs=_unique_fragment_refs(mutable_fragments),
            tool_result_refs=_unique_item_refs(list(result_refs)),
            created_at=self._clock.now(),
        )
        bundle = built.bundle
        bundle_ref = built.reference
        return EvidenceAcquisitionHandoff(
            run_id=scope.run_id,
            session_id=scope.session_id,
            bundle_ref=bundle_ref,
            bundle=bundle,
            validated_inputs=built.validated_inputs,
            bundle_binding_digest=_bundle_binding_digest(bundle_ref, bundle),
            rule_plan=scope.evidence_rule_plan,
            rule_plan_digest=scope.evidence_rule_plan.plan_digest,
            capability_registry_digest=self._capability_registry_digest,
            cells=bundle.cells,
            collection_fact_refs=tuple(
                reference
                for reference in bundle.fragment_refs
                if reference.evidence_id
                in {
                    evidence_id
                    for cell in bundle.cells
                    for evidence_id in cell.collection_fact_refs
                }
            ),
            fragment_allowlist=bundle.fragment_refs,
            source_record_allowlist=bundle.source_record_item_refs,
            tool_result_refs=bundle.tool_result_refs,
            budget_usage=self._executor.budget_usage(scope),
            producer_facts=tuple(
                EvidenceProducerFact(
                    name=descriptor.name,
                    status=descriptor.producer_status,
                    authorized_for_scope=self._registry.is_scope_authorized(
                        scope,
                        descriptor,
                    ),
                )
                for descriptor in self._registry.descriptors
                if descriptor.policy_class is EvidencePolicyClass.PRODUCER_SLOT
            ),
            read_port=self._reader,
            tool_roster=self._registry.roster(scope),
            executor=self._executor,
            cell_gates=_cell_gates(bundle.cells),
            usable_upstream_comids=await self._usable_upstream_comids(
                scope,
                bundle.fragment_refs,
            ),
            stop_before_model=stop_before_model,
            stop_facts=tuple(dict.fromkeys(stop_facts)),
            block_facts=tuple(dict.fromkeys(block_facts)),
        )

    def _scope_for_handoff(
        self,
        scope: EvidenceSessionScope,
        handoff: EvidenceAcquisitionHandoff,
    ) -> EvidenceSessionScope:
        baseline = scope.evidence_rule_plan
        current = handoff.rule_plan
        current_rules = {rule.rule_id: rule for rule in current.effective_rules}
        if (
            handoff.run_id != scope.run_id
            or handoff.session_id != scope.session_id
            or current.plan_digest != handoff.rule_plan_digest
            or current.baseline_rules != baseline.baseline_rules
            or any(
                current_rules.get(rule.rule_id) != rule
                for rule in baseline.appended_rules
            )
            or handoff.capability_registry_digest
            != self._capability_registry_digest
            or not handoff.has_valid_bundle_binding()
            or handoff.bundle.agent_comid != scope.agent_comid
            or handoff.bundle.context_ref.item_id
            != scope.analysis_context_ref.item_id
            or handoff.bundle.topology_ref.item_id != scope.topology_ref.item_id
            or handoff.bundle.requested_parameters != scope.requested_parameters
            or handoff.bundle.supporting_parameters != scope.supporting_parameters
            or handoff.bundle.window_refs
            != tuple(window.window_id for window in scope.time_windows)
        ):
            raise EvidenceScopeViolationError()
        return with_evidence_rule_plan(scope, current)

    async def _write_missing_cell_facts(
        self,
        scope: EvidenceSessionScope,
        existing_refs: tuple[EvidenceFragmentReference, ...],
    ) -> tuple[EvidenceFragmentReference, ...]:
        fragments = tuple(
            [
                await self._reader.read_fragment(scope, reference)
                for reference in existing_refs
            ]
        )
        references = []
        for window in scope.time_windows:
            seconds = int(
                (window.time_range.end - window.time_range.start).total_seconds()
            )
            for parameter in (*scope.requested_parameters, *scope.supporting_parameters):
                has_fact = any(
                    parameter in fragment.parameter_refs
                    and (
                        window.window_id in fragment.payload.affected_window_ids
                        if isinstance(fragment.payload, CollectionFactPayload)
                        else fragment.time_range.overlaps(window.time_range)
                    )
                    for fragment in fragments
                )
                if has_fact:
                    continue
                identity = (
                    f"{scope.run_id}:{scope.session_id}:{parameter.code_system}:"
                    f"{parameter.code}:{window.window_id}:no_applicable_evidence"
                )
                operation_id = str(uuid5(NAMESPACE_URL, f"m5:seal-fact:{identity}"))
                fragment = EvidenceFragment(
                    evidence_id=f"evidence:sha256:{sha256(identity.encode()).hexdigest()}",
                    source="m5_acquisition",
                    collection_status=CollectionStatus.EMPTY,
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
                    series_identity=(
                        f"seal:{parameter.code_system}:{parameter.code}"
                    ),
                    quality_class=None,
                    coverage=EvidenceCoverage(
                        status=CoverageStatus.INSUFFICIENT,
                        observed_count=0,
                        covered_duration_seconds=0,
                        window_duration_seconds=seconds,
                    ),
                    payload=CollectionFactPayload(
                        status=CollectionStatus.EMPTY,
                        reason="no_applicable_evidence",
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

    async def _execute(
        self,
        scope: EvidenceSessionScope,
        request: EvidenceToolRequest,
        *,
        fragment_refs: list[EvidenceFragmentReference],
        result_refs: list[ItemReference],
        block_facts: list[str],
        materialize_technical_facts: bool,
        agent_selected: bool = False,
    ) -> EvidenceToolExecution:
        execution = await (
            self._executor.execute_agent_tracked(scope, request)
            if agent_selected
            else self._executor.execute_tracked(scope, request)
        )
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
        *,
        parameters: tuple[ParameterKey, ...],
    ) -> bool:
        for reference in references:
            fragment = await self._reader.read_fragment(scope, reference)
            if (
                fragment.applicability is EvidenceApplicability.DIRECT_MEASUREMENT
                and set(fragment.parameter_refs) & set(parameters)
                and fragment.quality_class
                in {EvidenceQualityClass.VALID, EvidenceQualityClass.SUSPICIOUS}
            ):
                return True
        return False

    async def _has_usable_upstream(
        self,
        scope: EvidenceSessionScope,
        references: tuple[EvidenceFragmentReference, ...],
    ) -> bool:
        return bool(await self._usable_upstream_comids(scope, references))

    async def _usable_upstream_comids(
        self,
        scope: EvidenceSessionScope,
        references: tuple[EvidenceFragmentReference, ...],
    ) -> tuple[str, ...]:
        comids = set()
        for reference in references:
            fragment = await self._reader.read_fragment(scope, reference)
            if (
                isinstance(fragment.payload, UpstreamResultPayload)
                and fragment.quality_class
                in {EvidenceQualityClass.VALID, EvidenceQualityClass.SUSPICIOUS}
            ):
                comid = fragment.location.comid
                if comid not in scope.direct_upstream_comids:
                    raise EvidenceScopeViolationError()
                comids.add(comid)
        return tuple(sorted(comids))

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
        *,
        prior: EvidenceAcquisitionHandoff,
        max_calls: int,
    ) -> None:
        if type(max_calls) is not int or max_calls <= 0 or len(requests) > max_calls:
            raise EvidenceScopeViolationError()
        call_ids = tuple(request.tool_call_id for request in requests)
        if len(call_ids) != len(set(call_ids)):
            raise EvidenceScopeViolationError()

    def _validate_optional_request(
        self,
        scope: EvidenceSessionScope,
        request: EvidenceToolRequest,
        prior: EvidenceAcquisitionHandoff,
    ) -> None:
        legal = {item.name for item in self.planning_roster(scope, prior)}
        descriptor = self._registry.get(request.tool_name)
        if (
            request.session_id != scope.session_id
            or descriptor is None
            or (
                descriptor.policy_class
                not in {
                    EvidencePolicyClass.OPTIONAL,
                    EvidencePolicyClass.DETERMINISTIC,
                }
                and not (
                    descriptor.policy_class is EvidencePolicyClass.PRODUCER_SLOT
                    and descriptor.name == "search_transcript_evidence"
                )
            )
            or request.tool_name not in legal
        ):
            raise EvidenceScopeViolationError()


def _bundle_binding_digest(
    reference: ItemReference,
    bundle: EvidenceBundle,
) -> str:
    encoded = json.dumps(
        {
            "bundle_id": bundle.bundle_id,
            "item_id": reference.item_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"sha256:{sha256(encoded).hexdigest()}"


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

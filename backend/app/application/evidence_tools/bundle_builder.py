import json
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_EVEN, Decimal, localcontext
from hashlib import sha256

from app.application.evidence_policy import (
    temporal_relation_matches,
)
from app.application.evidence_tools.errors import EvidenceScopeViolationError
from app.application.evidence_tools.measurement_provenance import (
    derive_measured_anchors,
)
from app.application.evidence_tools.models import EvidenceToolReader, EvidenceToolWriter
from app.application.evidence_tools.numeric_methods import NumericMethodRegistry
from app.application.evidence_tools.scope import EvidenceSessionScope
from app.domain import (
    AggregatePayload,
    CollectionFactPayload,
    CollectionStatus,
    CollectionSummary,
    ConversionPayload,
    CoverageStatus,
    DataQualityFlag,
    DirectRecordPayload,
    DischargeObservationRecord,
    EvidenceAggregationMethod,
    EvidenceApplicability,
    EvidenceBundle,
    EvidenceCell,
    EvidenceCellRole,
    EvidenceCellStatus,
    EvidenceFragment,
    EvidenceFragmentReference,
    EvidenceQualityClass,
    EvidenceResultEligibility,
    InferenceMode,
    ItemReference,
    NumericDerivedPayload,
    ParameterProvider,
    QualitySummary,
    RelationSupportPayload,
    SourceRecord,
    TemporalEstimatePayload,
    TimeRange,
    UpstreamResultPayload,
    WaterObservationRecord,
)
from app.domain.common import normalize_canonical_decimal


@dataclass(frozen=True, slots=True)
class ValidatedBundleInputs:
    fragments: tuple[tuple[EvidenceFragmentReference, EvidenceFragment], ...]
    source_records: tuple[tuple[str, tuple[SourceRecord, ...]], ...]


@dataclass(frozen=True, slots=True)
class BuiltEvidenceBundle:
    bundle: EvidenceBundle
    validated_inputs: ValidatedBundleInputs


@dataclass(frozen=True, slots=True)
class WrittenEvidenceBundle:
    bundle: EvidenceBundle
    reference: ItemReference
    validated_inputs: ValidatedBundleInputs


class EvidenceBundleBuilder:
    def __init__(
        self,
        *,
        reader: EvidenceToolReader,
        writer: EvidenceToolWriter,
        numeric_registry: NumericMethodRegistry | None = None,
    ):
        self._reader = reader
        self._writer = writer
        self._numeric_registry = numeric_registry

    async def build(
        self,
        scope: EvidenceSessionScope,
        *,
        fragment_refs: tuple[EvidenceFragmentReference, ...],
        tool_result_refs: tuple[ItemReference, ...],
        created_at,
    ) -> BuiltEvidenceBundle:
        _ensure_unique_refs(fragment_refs, tool_result_refs)
        fragments = await self._reader.read_fragments(scope, fragment_refs)
        by_evidence_id = {
            fragment.evidence_id: (reference, fragment)
            for reference, fragment in zip(fragment_refs, fragments, strict=True)
        }
        if len(by_evidence_id) != len(fragments) or any(
            reference.evidence_id != fragment.evidence_id
            for reference, fragment in zip(fragment_refs, fragments, strict=True)
        ):
            raise EvidenceScopeViolationError()
        self._validate_fragments(scope, by_evidence_id)
        plans = {}
        await self._validate_numeric_derivations(scope, by_evidence_id, plans)
        await self._validate_planned_temporal_estimates(scope, by_evidence_id, plans)

        source_item_ids = tuple(
            sorted(
                {
                    reference.item_id
                    for fragment in fragments
                    for reference in fragment.source_record_refs
                }
            )
        )
        records_by_item = await self._validate_source_records(
            scope, fragments, source_item_ids
        )
        _validate_temporal_anchor_records(by_evidence_id, records_by_item)
        by_evidence_id = _compact_covered_observations(
            scope, by_evidence_id, records_by_item
        )
        fragments = tuple(fragment for _, fragment in by_evidence_id.values())
        fragment_item_ids = {
            reference.item_id for reference, _ in by_evidence_id.values()
        }
        nested_item_ids = {
            item_id
            for fragment in fragments
            for item_id in fragment.referenced_item_ids()
            if item_id not in set(source_item_ids) | fragment_item_ids
        }
        closure_refs = tuple(ItemReference(item_id=item_id) for item_id in sorted(nested_item_ids))
        ordered_fragment_refs = tuple(
            sorted(
                (reference for reference, _ in by_evidence_id.values()),
                key=lambda item: (item.evidence_id, item.item_id),
            )
        )
        ordered_tool_refs = tuple(sorted(tool_result_refs, key=lambda item: item.item_id))
        cells = self._build_cells(scope, by_evidence_id)
        used_ids = {item for cell in cells for item in cell.fragment_ids()}
        if used_ids != set(by_evidence_id):
            raise EvidenceScopeViolationError()

        bundle = EvidenceBundle(
            bundle_id=_bundle_id(
                scope,
                ordered_fragment_refs,
                ordered_tool_refs,
            ),
            agent_comid=scope.agent_comid,
            context_ref=ItemReference(item_id=scope.analysis_context_ref.item_id),
            topology_ref=ItemReference(item_id=scope.topology_ref.item_id),
            time_range=TimeRange(
                start=scope.time_windows[0].time_range.start,
                end=scope.time_windows[-1].time_range.end,
            ),
            analysis_timezone=scope.analysis_timezone,
            time_resolution=scope.time_resolution,
            time_resolution_source=scope.time_resolution_source,
            requested_parameters=scope.requested_parameters,
            supporting_parameters=scope.supporting_parameters,
            intermediate_parameters=scope.intermediate_parameters,
            window_refs=tuple(item.window_id for item in scope.time_windows),
            cells=cells,
            fragment_refs=ordered_fragment_refs,
            source_record_item_refs=source_item_ids,
            closure_refs=closure_refs,
            tool_result_refs=ordered_tool_refs,
            quality_summary=_quality_summary(fragments),
            collection_summary=_collection_summary(fragments),
            created_at=created_at,
        )
        await self._validate_reference_scope(scope, bundle, fragments)
        return BuiltEvidenceBundle(
            bundle=bundle,
            validated_inputs=ValidatedBundleInputs(
                fragments=tuple(
                    (reference, by_evidence_id[reference.evidence_id][1])
                    for reference in ordered_fragment_refs
                ),
                source_records=tuple(sorted(records_by_item.items())),
            ),
        )

    async def build_and_write(self, scope: EvidenceSessionScope, **kwargs) -> WrittenEvidenceBundle:
        built = await self.build(scope, **kwargs)
        reference = await self._writer.write_bundle(scope, built.bundle)
        if not reference.item_id:
            raise EvidenceScopeViolationError()
        return WrittenEvidenceBundle(
            bundle=built.bundle,
            reference=reference,
            validated_inputs=built.validated_inputs,
        )

    def _validate_fragments(self, scope, by_evidence_id):
        allowed_parameters = set(
            (
                *scope.requested_parameters,
                *scope.supporting_parameters,
                *scope.intermediate_parameters,
                *(
                    key
                    for rule in scope.evidence_rule_plan.effective_rules
                    for key in (rule.output_parameter, *rule.input_parameters)
                ),
            )
        )
        rules_by_id = {
            rule.rule_id: rule
            for rule in scope.evidence_rule_plan.effective_rules
        }
        graph: dict[str, tuple[str, ...]] = {}
        for evidence_id, (_reference, fragment) in by_evidence_id.items():
            if (
                fragment.target_comid != scope.agent_comid
                or not set(fragment.parameter_refs) <= allowed_parameters
            ):
                raise EvidenceScopeViolationError()
            dependencies = ()
            if (
                isinstance(fragment.payload, DirectRecordPayload)
                and fragment.result_eligibility
                is EvidenceResultEligibility.OBSERVED_OR_INFERRED
                and not (
                    fragment.location.comid == scope.agent_comid
                    or any(
                        station.station_id == fragment.location.station_id
                        for station in scope.station_refs
                    )
                )
            ):
                raise EvidenceScopeViolationError()
            if isinstance(fragment.payload, ConversionPayload):
                dependency = fragment.payload.input_evidence_ref
                stored = by_evidence_id.get(dependency.evidence_id)
                if stored is None or stored[0] != dependency:
                    raise EvidenceScopeViolationError()
                dependencies = (dependency.evidence_id,)
                source = stored[1]
                if (
                    fragment.result_eligibility
                    is EvidenceResultEligibility.OBSERVED_OR_INFERRED
                    and source.result_eligibility
                    is not EvidenceResultEligibility.OBSERVED_OR_INFERRED
                ):
                    raise EvidenceScopeViolationError()
                if (
                    fragment.result_eligibility
                    is EvidenceResultEligibility.OBSERVED_OR_INFERRED
                    and (
                        fragment.applicability
                        is not EvidenceApplicability.DIRECT_MEASUREMENT
                        or fragment.derivation is None
                        or not fragment.derivation.policy_id.startswith("unit:")
                    )
                ):
                    raise EvidenceScopeViolationError()
            elif isinstance(fragment.payload, RelationSupportPayload):
                rule = rules_by_id.get(fragment.payload.rule_id)
                if (
                    rule is None
                    or fragment.parameter_refs != (rule.output_parameter,)
                ):
                    raise EvidenceScopeViolationError()
                dependency_ids = []
                if len(fragment.payload.input_evidence_refs) != len(
                    rule.input_parameters
                ):
                    raise EvidenceScopeViolationError()
                for expected_parameter, dependency in zip(
                    rule.input_parameters,
                    fragment.payload.input_evidence_refs,
                    strict=True,
                ):
                    stored = by_evidence_id.get(dependency.evidence_id)
                    if (
                        stored is None
                        or stored[0] != dependency
                        or expected_parameter not in stored[1].parameter_refs
                    ):
                        raise EvidenceScopeViolationError()
                    dependency_ids.append(dependency.evidence_id)
                dependencies = tuple(dependency_ids)
            elif isinstance(fragment.payload, TemporalEstimatePayload):
                dependencies = _validate_temporal_estimate(
                    scope, fragment, by_evidence_id
                )
            elif isinstance(fragment.payload, NumericDerivedPayload):
                dependencies = tuple(
                    dependency.evidence_id
                    for dependency in fragment.payload.input_evidence_refs
                )
                if any(
                    by_evidence_id.get(dependency.evidence_id, (None,))[0]
                    != dependency
                    for dependency in fragment.payload.input_evidence_refs
                ):
                    raise EvidenceScopeViolationError()
            graph[evidence_id] = dependencies
        _reject_cycles(graph)

    async def _load_validated_numeric_plan(
        self, scope, reference, by_evidence_id, plans
    ):
        plan = plans.get(reference.item_id)
        if plan is not None:
            return plan
        plan = await self._reader.read_numeric_plan(scope, reference)
        missing_refs = []
        for source_ref in plan.source_fragment_refs:
            stored = by_evidence_id.get(source_ref.evidence_id)
            if stored is None:
                missing_refs.append(source_ref)
            elif stored[0] != source_ref:
                raise EvidenceScopeViolationError()
        if missing_refs:
            item_ids = tuple(source_ref.item_id for source_ref in missing_refs)
            resolved = await self._reader.resolve_reference_scopes(scope, item_ids)
            fragments = await self._reader.read_fragments(scope, tuple(missing_refs))
            if (
                len(set(item_ids)) != len(item_ids)
                or len(resolved) != len(missing_refs)
                or len(fragments) != len(missing_refs)
                or {
                    item.item_id for item in resolved
                } != set(item_ids)
                or any(
                    item.run_id != scope.run_id
                    or item.session_id != scope.session_id
                    for item in resolved
                )
            ):
                raise EvidenceScopeViolationError()
            for source_ref, fragment in zip(missing_refs, fragments, strict=True):
                if (
                    fragment.evidence_id != source_ref.evidence_id
                    or fragment.target_comid != scope.agent_comid
                    or not isinstance(fragment.payload, DirectRecordPayload)
                    or not any(
                        isinstance(covered.payload, AggregatePayload)
                        and covered.payload.method is EvidenceAggregationMethod.MEAN
                        and covered.source == "deterministic_aggregation"
                        and fragment.payload.record_ref
                        in covered.payload.input_record_refs
                        and covered.parameter_refs == fragment.parameter_refs
                        and covered.time_range == fragment.time_range
                        and covered.location.station_id == fragment.location.station_id
                        and covered.series_identity == fragment.series_identity
                        for _, covered in by_evidence_id.values()
                    )
                ):
                    raise EvidenceScopeViolationError()
        plans[reference.item_id] = plan
        return plan

    async def _validate_numeric_derivations(
        self, scope, by_evidence_id, plans
    ) -> None:
        derived = tuple(
            fragment
            for _, fragment in by_evidence_id.values()
            if isinstance(fragment.payload, NumericDerivedPayload)
        )
        if not derived:
            return
        registry = self._numeric_registry
        if registry is None:
            raise EvidenceScopeViolationError()
        windows = {window.window_id: window for window in scope.time_windows}
        completed_tasks: set[tuple[str, str]] = set()
        for fragment in derived:
            payload = fragment.payload
            plan = await self._load_validated_numeric_plan(
                scope, payload.plan_item_ref, by_evidence_id, plans
            )
            method = registry.get(payload.method_id, payload.method_version)
            task = next(
                (item for item in plan.tasks if item.task_id == payload.task_id),
                None,
            )
            target_window = windows.get(task.window_id) if task is not None else None
            identity = (payload.plan_item_ref.item_id, payload.task_id)
            if (
                plan.run_id != scope.run_id
                or plan.session_id != scope.session_id
                or plan.target_comid != scope.agent_comid
                or plan.plan_digest != payload.plan_digest
                or plan.method_registry_digest != registry.manifest_digest
                or payload.method_registry_digest != registry.manifest_digest
                or method is None
                or task is None
                or target_window is None
                or identity in completed_tasks
                or fragment.parameter_refs != (task.output_parameter,)
                or task.output_parameter != method.output_parameter
                or task.output_unit != payload.unit
                or payload.unit != method.output_unit
                or (task.method_id, task.method_version)
                != (method.method_id, method.version)
                or task.mode != target_window.window_kind.value
                or task.mode not in method.allowed_modes
                or fragment.time_range != target_window.time_range
                or fragment.target_comid != scope.agent_comid
                or fragment.location.comid != scope.agent_comid
                or fragment.location.station_id != task.station_id
                or fragment.series_identity != task.series_identity
                or task.method_kind != method.kind
                or payload.method_kind != method.kind
                or (method.kind == "explicit_calculation" and (
                    len(task.inputs) != len(method.inputs)
                    or len(task.inputs) != len(payload.input_evidence_refs)
                ))
                or payload.confidence_cap > method.confidence_cap
                or fragment.coverage.observed_count != 0
                or fragment.coverage.status is not CoverageStatus.INSUFFICIENT
            ):
                raise EvidenceScopeViolationError()
            completed_tasks.add(identity)
            if method.kind == "model_estimate":
                _validate_model_derivation(scope, fragment, task, plan, by_evidence_id)
                continue
            values = []
            input_windows = []
            for planned, specification, reference, window_id in zip(
                task.inputs,
                method.inputs,
                payload.input_evidence_refs,
                payload.input_window_ids,
                strict=True,
            ):
                stored = by_evidence_id.get(reference.evidence_id)
                input_window = windows.get(window_id)
                if stored is None or stored[0] != reference or input_window is None:
                    raise EvidenceScopeViolationError()
                source = stored[1]
                source_payload = source.payload
                source_number = _numeric_payload_value_unit(source_payload)
                expected_dependency = planned.dependency_task_id
                if (
                    planned.window_id != window_id
                    or planned.parameter != specification.parameter
                    or planned.unit != specification.unit
                    or source.parameter_refs != (planned.parameter,)
                    or source.target_comid != scope.agent_comid
                    or source.location.comid != scope.agent_comid
                    or source.time_range != input_window.time_range
                    or source.quality_class
                    not in {EvidenceQualityClass.VALID, EvidenceQualityClass.SUSPICIOUS}
                    or source.result_eligibility
                    is EvidenceResultEligibility.EXPLANATORY_ONLY
                    or source_number is None
                    or source_number[1] != planned.unit
                    or (
                        planned.evidence_ref is not None
                        and planned.evidence_ref != reference
                    )
                    or (expected_dependency is None and planned.evidence_ref is None)
                    or (
                        expected_dependency is not None
                        and not _planned_predecessor_matches(
                            source_payload, expected_dependency, payload
                        )
                    )
                    or (
                        specification.required_payload_type is not None
                        and source_payload.payload_type
                        != specification.required_payload_type
                    )
                    or (
                        not specification.allow_estimated
                        and (
                            isinstance(
                                source_payload,
                                (TemporalEstimatePayload, NumericDerivedPayload),
                            )
                            or DataQualityFlag.ESTIMATED in source.quality_flags
                        )
                    )
                    or not _numeric_window_relation_matches(
                        specification.window_relation,
                        input_window.time_range,
                        target_window.time_range,
                    )
                    or (
                        method.production
                        and (
                            source.location.station_id != task.station_id
                            or source.series_identity != task.series_identity
                        )
                    )
                    or not set(source.quality_flags).issubset(fragment.quality_flags)
                    or (
                        source.quality_class is EvidenceQualityClass.SUSPICIOUS
                        and fragment.quality_class
                        is not EvidenceQualityClass.SUSPICIOUS
                    )
                    or (
                        isinstance(source_payload, NumericDerivedPayload)
                        and payload.confidence_cap > source_payload.confidence_cap
                    )
                    or (
                        isinstance(source_payload, TemporalEstimatePayload)
                        and payload.confidence_cap
                        > _temporal_input_confidence_cap(
                            source_payload, input_window.time_range
                        )
                    )
                ):
                    raise EvidenceScopeViolationError()
                values.append(Decimal(source_number[0]))
                input_windows.append(input_window.time_range)
            try:
                assert method.calculate is not None
                expected_value = normalize_canonical_decimal(method.calculate(
                    tuple(values), tuple(input_windows), target_window.time_range
                ))
            except (ArithmeticError, TypeError, ValueError) as error:
                raise EvidenceScopeViolationError() from error
            if payload.value != expected_value:
                raise EvidenceScopeViolationError()

    async def _validate_planned_temporal_estimates(
        self, scope, by_evidence_id, plans
    ) -> None:
        planned = tuple(
            fragment
            for _, fragment in by_evidence_id.values()
            if isinstance(fragment.payload, TemporalEstimatePayload)
            and fragment.payload.plan_item_ref is not None
        )
        if not planned:
            return
        registry = self._numeric_registry
        if registry is None:
            raise EvidenceScopeViolationError()
        windows = {window.window_id: window for window in scope.time_windows}
        completed: set[tuple[str, str]] = set()
        for fragment in planned:
            payload = fragment.payload
            plan_ref = payload.plan_item_ref
            if plan_ref is None or payload.task_id is None:
                raise EvidenceScopeViolationError()
            plan = await self._load_validated_numeric_plan(
                scope, plan_ref, by_evidence_id, plans
            )
            if not plan.source_fragment_refs:
                raise EvidenceScopeViolationError()
            task = next(
                (item for item in plan.tasks if item.task_id == payload.task_id), None
            )
            if task is None:
                raise EvidenceScopeViolationError()
            window = windows.get(task.window_id)
            method = registry.get(task.method_id, task.method_version)
            parameter = fragment.parameter_refs[0]
            kind = "linear" if payload.method == "linear_interpolation" else "hold"
            expected_method_id = (
                f"temporal_gap_fill_{kind}_v1_"
                f"{parameter.code_system}_{parameter.code}"
            )
            identity = (plan_ref.item_id, payload.task_id)
            if (
                plan.run_id != scope.run_id
                or plan.session_id != scope.session_id
                or plan.target_comid != scope.agent_comid
                or plan.plan_digest != payload.plan_digest
                or plan.method_registry_digest != registry.manifest_digest
                or payload.method_registry_digest != registry.manifest_digest
                or method is None
                or not method.production
                or method.method_id != expected_method_id
                or task.method_id != method.method_id
                or task.method_version != method.version
                or task.output_parameter != parameter
                or task.output_unit != payload.unit
                or window is None
                or window.time_range != fragment.time_range
                or task.mode != window.window_kind.value
                or task.mode not in method.allowed_modes
                or task.target_comid != scope.agent_comid
                or task.station_id != fragment.location.station_id
                or task.series_identity != fragment.series_identity
                or len(task.inputs) != len(method.inputs)
                or len(task.inputs) != len(payload.input_evidence_refs)
                or identity in completed
            ):
                raise EvidenceScopeViolationError()
            completed.add(identity)
            for planned_input, specification, reference, window_id in zip(
                task.inputs,
                method.inputs,
                payload.input_evidence_refs,
                payload.anchor_window_ids,
                strict=True,
            ):
                source_window = windows.get(window_id)
                if (
                    source_window is None
                    or planned_input.evidence_ref != reference
                    or planned_input.dependency_task_id is not None
                    or planned_input.window_id != window_id
                    or planned_input.parameter != specification.parameter
                    or planned_input.unit != specification.unit
                    or specification.required_payload_type != "aggregate"
                    or specification.allow_estimated
                    or not _numeric_window_relation_matches(
                        specification.window_relation,
                        source_window.time_range,
                        window.time_range,
                    )
                ):
                    raise EvidenceScopeViolationError()

    async def _validate_source_records(self, scope, fragments, source_item_ids):
        records_by_item = {
            item_id: await self._reader.read_source_records(scope, item_id)
            for item_id in source_item_ids
        }
        record_ids_by_item = {}
        for item_id, records in records_by_item.items():
            record_ids = {record.record_id for record in records}
            if len(record_ids) != len(records):
                raise EvidenceScopeViolationError()
            record_ids_by_item[item_id] = record_ids
        for fragment in fragments:
            for reference in fragment.source_record_refs:
                if reference.record_id not in record_ids_by_item[reference.item_id]:
                    raise EvidenceScopeViolationError()
        return records_by_item

    def _build_cells(self, scope, by_evidence_id):
        cells = []
        for window in scope.time_windows:
            for role, parameters in (
                (EvidenceCellRole.TARGET, scope.requested_parameters),
                (EvidenceCellRole.REQUIRED_SUPPORT, scope.supporting_parameters),
                (EvidenceCellRole.METHOD_INPUT, scope.intermediate_parameters),
            ):
                for parameter in parameters:
                    matching = tuple(
                        fragment
                        for _, fragment in by_evidence_id.values()
                        if parameter in fragment.parameter_refs
                        and _fragment_applies(fragment, window.window_id, window.time_range)
                    )
                    applicable_rules = tuple(
                        rule
                        for rule in scope.evidence_rule_plan.effective_rules
                        if rule.output_parameter == parameter
                    )
                    auxiliary = tuple(
                        fragment
                        for _, fragment in by_evidence_id.values()
                        if any(
                            set(rule.input_parameters).intersection(
                                fragment.parameter_refs
                            )
                            and temporal_relation_matches(
                                rule.temporal_relation,
                                fragment.time_range,
                                window.time_range,
                                analysis_timezone=scope.analysis_timezone,
                            )
                            for rule in applicable_rules
                        )
                        and parameter not in fragment.parameter_refs
                    )
                    direct = tuple(
                        sorted(
                            fragment.evidence_id
                            for fragment in matching
                            if not isinstance(fragment.payload, CollectionFactPayload)
                            and fragment.quality_class
                            in {
                                EvidenceQualityClass.VALID,
                                EvidenceQualityClass.SUSPICIOUS,
                            }
                            and fragment.applicability
                            is EvidenceApplicability.DIRECT_MEASUREMENT
                        )
                    )
                    indirect = tuple(
                        sorted(
                            fragment.evidence_id
                            for fragment in matching
                            if not isinstance(fragment.payload, CollectionFactPayload)
                            and fragment.quality_class
                            in {
                                EvidenceQualityClass.VALID,
                                EvidenceQualityClass.SUSPICIOUS,
                            }
                            and fragment.applicability
                            is EvidenceApplicability.ALLOWED_INDIRECT
                        )
                    )
                    facts = tuple(
                        sorted(
                            fragment.evidence_id
                            for fragment in matching
                            if isinstance(fragment.payload, CollectionFactPayload)
                        )
                    )
                    classified = set((*direct, *indirect, *facts))
                    contextual = tuple(
                        sorted(
                            {
                                fragment.evidence_id
                                for fragment in (*matching, *auxiliary)
                                if fragment.evidence_id not in classified
                            }
                        )
                    )
                    blockers = tuple(
                        sorted(
                            {
                                fragment.payload.reason
                                for fragment in matching
                                if isinstance(fragment.payload, CollectionFactPayload)
                                and fragment.payload.status
                                in {CollectionStatus.UNAVAILABLE, CollectionStatus.INVALID}
                                and not (
                                    fragment.payload.status is CollectionStatus.INVALID
                                    and fragment.payload.reason == "unit_unresolved"
                                    and (direct or indirect)
                                )
                            }
                        )
                    )
                    estimated = any(
                        isinstance(fragment.payload, NumericDerivedPayload)
                        and fragment.payload.method_kind == "model_estimate"
                        and fragment.evidence_id in indirect
                        for fragment in matching
                    )
                    if blockers and not estimated:
                        status = EvidenceCellStatus.BLOCKED
                        reasons = blockers
                    elif direct or indirect:
                        status = EvidenceCellStatus.SUPPORTED
                        reasons = (*blockers, *tuple(
                            reason
                            for reason, present in (
                                (
                                    "suspicious_evidence_present",
                                    any(
                                        fragment.quality_class
                                        is EvidenceQualityClass.SUSPICIOUS
                                        for fragment in matching
                                        if fragment.evidence_id
                                        in set((*direct, *indirect))
                                    ),
                                ),
                                (
                                    "partial_coverage",
                                    any(
                                        fragment.coverage.status.value == "partial"
                                        for fragment in matching
                                        if fragment.evidence_id
                                        in set((*direct, *indirect))
                                    ),
                                ),
                            )
                            if present
                        ))
                    else:
                        status = EvidenceCellStatus.UNSUPPORTED
                        reasons = ("no_usable_evidence",)
                    cells.append(
                        EvidenceCell(
                            parameter=parameter,
                            window_id=window.window_id,
                            role=role,
                            direct_fragment_refs=direct,
                            allowed_indirect_fragment_refs=indirect,
                            contextual_fragment_refs=contextual,
                            collection_fact_refs=facts,
                            status=status,
                            reasons=reasons,
                        )
                    )
        return tuple(cells)

    async def _validate_reference_scope(self, scope, bundle, fragments):
        known = (
            scope.analysis_context_ref,
            scope.topology_ref,
        )
        remaining = tuple(
            item_id
            for item_id in bundle.referenced_item_ids()
            if item_id not in {item.item_id for item in known}
        )
        resolved = await self._reader.resolve_reference_scopes(scope, remaining)
        if {item.item_id for item in resolved} != set(remaining):
            raise EvidenceScopeViolationError()
        try:
            upstream_pairs = tuple(
                (item_id, fragment.payload.upstream_session_id)
                for fragment in fragments
                if isinstance(fragment.payload, UpstreamResultPayload)
                for item_id in (
                    fragment.payload.result_ref.item_id,
                    *(item.item_id for item in fragment.payload.closure_refs),
                )
            )
            upstream_scopes: dict[str, str] = {}
            for item_id, upstream_session_id in upstream_pairs:
                previous = upstream_scopes.setdefault(item_id, upstream_session_id)
                if previous != upstream_session_id:
                    raise EvidenceScopeViolationError()
            resolved_by_item = {item.item_id: item for item in (*known, *resolved)}
            if any(
                resolved_by_item[item_id].session_id != upstream_session_id
                for item_id, upstream_session_id in upstream_scopes.items()
            ):
                raise EvidenceScopeViolationError()
            bundle.validate_reference_scope(
                run_id=scope.run_id,
                session_id=scope.session_id,
                resolved_scopes=(*known, *resolved),
                same_run_session_item_ids=tuple(sorted(upstream_scopes)),
            )
        except ValueError as error:
            raise EvidenceScopeViolationError() from error


def _numeric_payload_value_unit(payload) -> tuple[str, str] | None:
    if isinstance(payload, DirectRecordPayload):
        return payload.displayed_value, payload.displayed_unit
    if isinstance(
        payload,
        (
            AggregatePayload,
            ConversionPayload,
            TemporalEstimatePayload,
            NumericDerivedPayload,
        ),
    ):
        return (payload.value, payload.unit) if payload.value is not None else None
    return None


def _validate_model_derivation(scope, fragment, task, plan, by_evidence_id):
    payload = fragment.payload
    windows = {window.window_id: window for window in scope.time_windows}
    source_refs = set(plan.source_fragment_refs)
    included: dict[EvidenceFragmentReference, EvidenceFragment] = {}
    for reference, window_id in zip(
        payload.input_evidence_refs, payload.input_window_ids, strict=True
    ):
        stored = by_evidence_id.get(reference.evidence_id)
        if stored is None or stored[0] != reference:
            raise EvidenceScopeViolationError()
        source = stored[1]
        expected_window = next(
            (key for key, window in windows.items() if window.time_range == source.time_range),
            "context",
        )
        if (
            source.target_comid != scope.agent_comid
            or source.quality_class is EvidenceQualityClass.INVALID
            or window_id != expected_window
        ):
            raise EvidenceScopeViolationError()
        admitted = reference in source_refs
        for binding in task.inputs:
            matches = (
                reference == binding.evidence_ref
                if binding.evidence_ref is not None
                else _planned_predecessor_matches(
                    source.payload, binding.dependency_task_id, payload
                )
            )
            if not matches:
                continue
            if (
                source.collection_status is not CollectionStatus.AVAILABLE
                or source.parameter_refs != (binding.parameter,)
                or window_id != binding.window_id
                or _model_input_unit(source.payload) != binding.unit
            ):
                raise EvidenceScopeViolationError()
            admitted = True
        if not admitted:
            raise EvidenceScopeViolationError()
        included[reference] = source
    for binding in task.inputs:
        if binding.required and not any(
            reference == binding.evidence_ref
            if binding.evidence_ref is not None
            else _planned_predecessor_matches(
                source.payload, binding.dependency_task_id, payload
            )
            for reference, source in included.items()
        ):
            raise EvidenceScopeViolationError()
    anchors = derive_measured_anchors(
        parameter=task.output_parameter,
        target_comid=scope.agent_comid,
        evidence_ids=tuple(reference.evidence_id for reference in included),
        fragments={key: item[1] for key, item in by_evidence_id.items()},
    )
    basis = (
        "same_parameter_measurement" if anchors else "related_evidence"
        if any(
            source.collection_status is CollectionStatus.AVAILABLE
            and not isinstance(source.payload, CollectionFactPayload)
            for source in included.values()
        ) else "general_knowledge"
    )
    inherited_flags = {
        flag
        for source in included.values()
        if source.collection_status is CollectionStatus.AVAILABLE
        and source.quality_class in {
            EvidenceQualityClass.VALID, EvidenceQualityClass.SUSPICIOUS,
        }
        and source.result_eligibility
        is not EvidenceResultEligibility.EXPLANATORY_ONLY
        for flag in source.quality_flags
    }
    expected_flags = tuple(sorted(
        {DataQualityFlag.ESTIMATED, *inherited_flags},
        key=lambda flag: flag.value,
    ))
    if (
        payload.measured_anchors != anchors
        or payload.inference_basis != basis
        or (payload.value is not None and not anchors)
        or fragment.quality_class is not EvidenceQualityClass.SUSPICIOUS
        or fragment.quality_flags != expected_flags
        or (
            task.output_parameter.code_system == "usgs"
            and task.output_parameter.code in {"00400", "00403"}
            and Decimal(payload.value_lower) <= 0
            and Decimal(payload.value_upper) >= 14
        )
    ):
        raise EvidenceScopeViolationError()


def _model_input_unit(payload) -> str | None:
    if isinstance(payload, DirectRecordPayload):
        return payload.displayed_unit
    return getattr(payload, "unit", None)


def _numeric_window_relation_matches(relation, source, target) -> bool:
    if relation == "same_window":
        return source == target
    if relation == "nearest_before":
        return source.end <= target.start
    if relation == "nearest_after":
        return source.start >= target.end
    if relation == "nearest_anchor":
        return source.end <= target.start or source.start >= target.end
    return False


def _ensure_unique_refs(fragment_refs, tool_result_refs):
    fragment_keys = tuple((item.item_id, item.evidence_id) for item in fragment_refs)
    tool_ids = tuple(item.item_id for item in tool_result_refs)
    if (
        not fragment_refs
        or len(fragment_keys) != len(set(fragment_keys))
        or len(tool_ids) != len(set(tool_ids))
    ):
        raise EvidenceScopeViolationError()


def _temporal_input_confidence_cap(
    payload: TemporalEstimatePayload, window: TimeRange
) -> float:
    window_seconds = (window.end - window.start).total_seconds()
    farthest_anchor_seconds = max(payload.anchor_distance_seconds)
    base = 0.75 if payload.method == "linear_interpolation" else 0.55
    return round(base * min(1.0, window_seconds / farthest_anchor_seconds), 6)


def _planned_predecessor_matches(
    source: object,
    task_id: str,
    output: NumericDerivedPayload,
) -> bool:
    return isinstance(source, (NumericDerivedPayload, TemporalEstimatePayload)) and (
        source.task_id == task_id
        and source.plan_item_ref == output.plan_item_ref
        and source.plan_digest == output.plan_digest
        and source.method_registry_digest == output.method_registry_digest
    )


def _compact_covered_observations(scope, by_evidence_id, records_by_item):
    """Use one persisted mean only when it represents every raw record in its group."""
    required_ids = {
        dependency.evidence_id
        for _, fragment in by_evidence_id.values()
        for dependency in (
            (fragment.payload.input_evidence_ref,)
            if isinstance(fragment.payload, ConversionPayload)
            else fragment.payload.input_evidence_refs
            if isinstance(
                fragment.payload,
                (RelationSupportPayload, TemporalEstimatePayload, NumericDerivedPayload),
            )
            else ()
        )
    }
    observations = tuple(
        fragment
        for _, fragment in by_evidence_id.values()
        if isinstance(fragment.payload, DirectRecordPayload)
    )
    aggregates = tuple(
        fragment
        for _, fragment in by_evidence_id.values()
        if isinstance(fragment.payload, AggregatePayload)
    )
    if not observations or not aggregates:
        return by_evidence_id

    records = {
        (item_id, record.record_id): record
        for item_id, items in records_by_item.items()
        for record in items
    }
    compacted_ids = set()
    for aggregate in aggregates:
        payload = aggregate.payload
        if (
            aggregate.source != "deterministic_aggregation"
            or aggregate.applicability is not EvidenceApplicability.DIRECT_MEASUREMENT
            or aggregate.result_eligibility is not EvidenceResultEligibility.INFERRED_ONLY
            or aggregate.quality_class
            not in {EvidenceQualityClass.VALID, EvidenceQualityClass.SUSPICIOUS}
            or aggregate.coverage.status
            not in {CoverageStatus.COMPLETE, CoverageStatus.PARTIAL}
            or payload.method is not EvidenceAggregationMethod.MEAN
            or payload.invalid_input_count
            or len(aggregate.parameter_refs) != 1
            or aggregate.location.station_id is None
            or aggregate.series_identity is None
        ):
            continue
        window = next(
            (
                item.time_range
                for item in scope.time_windows
                if item.time_range == aggregate.time_range
            ),
            None,
        )
        if window is None:
            continue
        input_refs = set(payload.input_record_refs)
        raw = tuple(
            fragment
            for fragment in observations
            if fragment.time_range == window
            and fragment.parameter_refs == aggregate.parameter_refs
            and fragment.location.station_id == aggregate.location.station_id
            and fragment.series_identity == aggregate.series_identity
        )
        raw_refs = {fragment.payload.record_ref for fragment in raw}
        if (
            len(raw_refs) != len(raw)
            or raw_refs != input_refs
            or aggregate.coverage.observed_count != len(raw)
            or any(
                fragment.quality_class
                not in {EvidenceQualityClass.VALID, EvidenceQualityClass.SUSPICIOUS}
                or fragment.applicability
                is not EvidenceApplicability.DIRECT_MEASUREMENT
                or fragment.result_eligibility
                is not EvidenceResultEligibility.OBSERVED_OR_INFERRED
                or fragment.source
                not in {"usgs_waterdata", ParameterProvider.WDFN_TIME_SERIES.value}
                for fragment in raw
            )
            or not any(
                station.station_id == aggregate.location.station_id
                and station.provider is ParameterProvider.WDFN_TIME_SERIES
                for station in scope.station_refs
            )
        ):
            continue
        if (
            payload.valid_input_count
            != sum(item.quality_class is EvidenceQualityClass.VALID for item in raw)
            or payload.suspicious_input_count
            != sum(item.quality_class is EvidenceQualityClass.SUSPICIOUS for item in raw)
            or any(
                flag not in aggregate.quality_flags
                for item in raw
                for flag in item.quality_flags
            )
            or (
                payload.suspicious_input_count
                and aggregate.quality_class is not EvidenceQualityClass.SUSPICIOUS
            )
        ):
            continue
        if any(
            not isinstance(
                record := records[(reference.item_id, reference.record_id)],
                (WaterObservationRecord, DischargeObservationRecord),
            )
            or record.provider != fragment.source
            or record.channel != "continuous"
            or record.parameter != aggregate.parameter_refs[0]
            or record.location.station_id != aggregate.location.station_id
            or record.provenance.provider_series_id
            != fragment.provenance.provider_series_id
            or record.observed_time is None
            or not window.contains(record.observed_time)
            for fragment in raw
            for reference in (fragment.payload.record_ref,)
        ):
            continue
        compacted_ids.update(
            fragment.evidence_id
            for fragment in raw
            if fragment.evidence_id not in required_ids
        )
    return {
        evidence_id: value
        for evidence_id, value in by_evidence_id.items()
        if evidence_id not in compacted_ids
    }


def _window_midpoint(window):
    return window.time_range.start + (
        window.time_range.end - window.time_range.start
    ) / 2


def _decimal_seconds(delta):
    return (
        Decimal(delta.days) * Decimal(86400)
        + Decimal(delta.seconds)
        + Decimal(delta.microseconds) / Decimal(1000000)
    )


def _validate_temporal_anchor_records(by_evidence_id, records_by_item):
    records_by_ref = {
        (item_id, record.record_id): record
        for item_id, records in records_by_item.items()
        for record in records
    }
    for _, estimate in by_evidence_id.values():
        if not isinstance(estimate.payload, TemporalEstimatePayload):
            continue
        for reference in estimate.payload.input_evidence_refs:
            anchor = by_evidence_id[reference.evidence_id][1]
            method = anchor.payload.method
            allowed_sources = (
                {("usgs_waterdata", "continuous"),
                 (ParameterProvider.WDFN_TIME_SERIES.value, "continuous")}
                if method is EvidenceAggregationMethod.MEAN
                else {(ParameterProvider.USGS_SAMPLES.value, "discrete_samples")}
            )
            if (
                anchor.derivation is None
                or anchor.derivation.algorithm != f"decimal_{method.value}"
            ):
                raise EvidenceScopeViolationError()
            records = tuple(
                records_by_ref[(record_ref.item_id, record_ref.record_id)]
                for record_ref in anchor.payload.input_record_refs
            )
            if any(
                not isinstance(record, (WaterObservationRecord, DischargeObservationRecord))
                or (record.provider, record.channel) not in allowed_sources
                or record.parameter != anchor.parameter_refs[0]
                or record.location.station_id != anchor.location.station_id
                or record.observed_time is None
                or not anchor.time_range.contains(record.observed_time)
                for record in records
            ):
                raise EvidenceScopeViolationError()
            if len({record.provenance.provider_series_id for record in records}) != 1:
                raise EvidenceScopeViolationError()


def _validate_temporal_estimate(scope, estimate, by_evidence_id):
    payload = estimate.payload
    target_window = next(
        (window for window in scope.time_windows if window.time_range == estimate.time_range),
        None,
    )
    if (
        target_window is None
        or target_window.window_kind is not InferenceMode.GAP_FILLING
        or estimate.location.station_id is None
        or estimate.series_identity is None
        or estimate.derivation is None
        or estimate.derivation.algorithm != "temporal_gap_fill"
        or estimate.derivation.algorithm_version != 1
        or estimate.derivation.policy_id != "temporal_gap_fill_v1"
    ):
        raise EvidenceScopeViolationError()
    if any(
        fragment.time_range == estimate.time_range
        and fragment.parameter_refs == estimate.parameter_refs
        and fragment.applicability is EvidenceApplicability.DIRECT_MEASUREMENT
        and fragment.quality_class in {EvidenceQualityClass.VALID, EvidenceQualityClass.SUSPICIOUS}
        for _, fragment in by_evidence_id.values()
    ):
        raise EvidenceScopeViolationError()
    if any(
        isinstance(fragment.payload, CollectionFactPayload)
        and fragment.parameter_refs == estimate.parameter_refs
        and target_window.window_id in fragment.payload.affected_window_ids
        and fragment.collection_status in {CollectionStatus.UNAVAILABLE, CollectionStatus.INVALID}
        and not (
            fragment.collection_status is CollectionStatus.INVALID
            and fragment.payload.reason == "unit_unresolved"
        )
        for _, fragment in by_evidence_id.values()
    ):
        raise EvidenceScopeViolationError()

    input_methods = set()
    for reference in payload.input_evidence_refs:
        stored = by_evidence_id.get(reference.evidence_id)
        if (
            stored is None
            or stored[0] != reference
            or not isinstance(stored[1].payload, AggregatePayload)
        ):
            raise EvidenceScopeViolationError()
        input_methods.add(stored[1].payload.method)
    if len(input_methods) != 1:
        raise EvidenceScopeViolationError()
    anchor_method = next(iter(input_methods))

    candidates = []
    for reference, anchor in by_evidence_id.values():
        if (
            not isinstance(anchor.payload, AggregatePayload)
            or anchor.source != "deterministic_aggregation"
            or anchor.payload.method is not anchor_method
            or anchor_method not in {
                EvidenceAggregationMethod.MEAN,
                EvidenceAggregationMethod.LATEST,
            }
            or anchor.applicability is not EvidenceApplicability.DIRECT_MEASUREMENT
            or anchor.result_eligibility is not EvidenceResultEligibility.INFERRED_ONLY
            or anchor.quality_class not in {EvidenceQualityClass.VALID, EvidenceQualityClass.SUSPICIOUS}
            or anchor.coverage.status not in {CoverageStatus.COMPLETE, CoverageStatus.PARTIAL}
            or anchor.target_comid != estimate.target_comid
            or anchor.location.comid != estimate.location.comid
            or anchor.location.station_id != estimate.location.station_id
            or anchor.parameter_refs != estimate.parameter_refs
            or anchor.series_identity != estimate.series_identity
            or anchor.payload.unit != payload.unit
        ):
            continue
        window = next(
            (item for item in scope.time_windows if item.time_range == anchor.time_range),
            None,
        )
        if window is not None and window.window_kind is InferenceMode.GAP_FILLING:
            candidates.append((window, reference, anchor))
    if not candidates or len({item[0].window_id for item in candidates}) != len(candidates):
        raise EvidenceScopeViolationError()

    target_midpoint = _window_midpoint(target_window)
    left = sorted(
        (item for item in candidates if _window_midpoint(item[0]) < target_midpoint),
        key=lambda item: _window_midpoint(item[0]),
    )
    right = sorted(
        (item for item in candidates if _window_midpoint(item[0]) > target_midpoint),
        key=lambda item: _window_midpoint(item[0]),
    )
    if any(_window_midpoint(item[0]) == target_midpoint for item in candidates):
        raise EvidenceScopeViolationError()
    if left and right:
        expected_method = "linear_interpolation"
        selected = (left[-1], right[0])
    else:
        expected_method = "nearest_hold"
        selected = (left[-1] if left else right[0],)
    expected_refs = tuple(item[1] for item in selected)
    expected_window_ids = tuple(item[0].window_id for item in selected)
    expected_distances = tuple(
        int(
            _decimal_seconds(abs(_window_midpoint(item[0]) - target_midpoint))
            .to_integral_value(rounding=ROUND_CEILING)
        )
        for item in selected
    )
    if (
        payload.method != expected_method
        or payload.input_evidence_refs != expected_refs
        or payload.anchor_window_ids != expected_window_ids
        or payload.anchor_distance_seconds != expected_distances
        or estimate.quality_class is not EvidenceQualityClass.SUSPICIOUS
        or any(
            not set(item[2].quality_flags) <= set(estimate.quality_flags)
            for item in selected
        )
    ):
        raise EvidenceScopeViolationError()

    actual = Decimal(payload.value)
    if expected_method == "nearest_hold":
        if actual != Decimal(selected[0][2].payload.value):
            raise EvidenceScopeViolationError()
    else:
        left_value = Decimal(selected[0][2].payload.value)
        right_value = Decimal(selected[1][2].payload.value)
        span = _decimal_seconds(_window_midpoint(selected[1][0]) - _window_midpoint(selected[0][0]))
        offset = _decimal_seconds(target_midpoint - _window_midpoint(selected[0][0]))
        with localcontext() as context:
            context.prec = 34
            expected = left_value + (right_value - left_value) * offset / span
            if actual != expected:
                source_scale = max(
                    -value.as_tuple().exponent for value in (left_value, right_value)
                )
                output_scale = -actual.as_tuple().exponent
                if output_scale < source_scale or actual != expected.quantize(
                    Decimal(1).scaleb(-output_scale), rounding=ROUND_HALF_EVEN
                ):
                    raise EvidenceScopeViolationError()
    return tuple(reference.evidence_id for reference in expected_refs)


def _reject_cycles(graph):
    visiting = set()
    visited = set()

    def visit(node):
        if node in visiting:
            raise EvidenceScopeViolationError()
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph[node]:
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)


def _fragment_applies(fragment, window_id, window):
    if isinstance(fragment.payload, CollectionFactPayload):
        return window_id in fragment.payload.affected_window_ids
    return fragment.time_range.overlaps(window)


def _bundle_id(scope, fragment_refs, tool_result_refs):
    payload = {
        "run_id": scope.run_id,
        "session_id": scope.session_id,
        "agent_comid": scope.agent_comid,
        "fragments": [item.model_dump(mode="json") for item in fragment_refs],
        "tool_results": [item.model_dump(mode="json") for item in tool_result_refs],
    }
    if scope.intermediate_parameters:
        payload["intermediate_parameters"] = [
            item.model_dump(mode="json") for item in scope.intermediate_parameters
        ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"bundle:sha256:{sha256(encoded).hexdigest()}"


def _quality_summary(fragments):
    return QualitySummary(
        valid=sum(item.quality_class is EvidenceQualityClass.VALID for item in fragments),
        suspicious=sum(
            item.quality_class is EvidenceQualityClass.SUSPICIOUS for item in fragments
        ),
        invalid=sum(
            item.quality_class is EvidenceQualityClass.INVALID for item in fragments
        ),
    )


def _collection_summary(fragments):
    facts = tuple(
        item for item in fragments if isinstance(item.payload, CollectionFactPayload)
    )
    return CollectionSummary(
        available=sum(item.collection_status is CollectionStatus.AVAILABLE for item in facts),
        empty=sum(item.collection_status is CollectionStatus.EMPTY for item in facts),
        unavailable=sum(
            item.collection_status is CollectionStatus.UNAVAILABLE for item in facts
        ),
        invalid=sum(item.collection_status is CollectionStatus.INVALID for item in facts),
    )

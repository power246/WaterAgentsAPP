import json
from dataclasses import dataclass
from hashlib import sha256

from app.application.evidence_policy import temporal_relation_matches
from app.application.evidence_tools.errors import EvidenceScopeViolationError
from app.application.evidence_tools.models import EvidenceToolReader, EvidenceToolWriter
from app.application.evidence_tools.scope import EvidenceSessionScope
from app.domain import (
    CollectionFactPayload,
    CollectionStatus,
    CollectionSummary,
    ConversionPayload,
    DirectRecordPayload,
    EvidenceApplicability,
    EvidenceBundle,
    EvidenceCell,
    EvidenceCellRole,
    EvidenceCellStatus,
    EvidenceFragment,
    EvidenceFragmentReference,
    EvidenceQualityClass,
    EvidenceResultEligibility,
    ItemReference,
    QualitySummary,
    RelationSupportPayload,
    ReferenceScope,
    SourceRecord,
    TimeRange,
    UpstreamResultPayload,
)


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
    def __init__(self, *, reader: EvidenceToolReader, writer: EvidenceToolWriter):
        self._reader = reader
        self._writer = writer

    async def build(
        self,
        scope: EvidenceSessionScope,
        *,
        fragment_refs: tuple[EvidenceFragmentReference, ...],
        tool_result_refs: tuple[ItemReference, ...],
        created_at,
    ) -> BuiltEvidenceBundle:
        _ensure_unique_refs(fragment_refs, tool_result_refs)
        fragments = tuple(
            [
                await self._reader.read_fragment(scope, reference)
                for reference in fragment_refs
            ]
        )
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
        fragment_item_ids = {reference.item_id for reference in fragment_refs}
        nested_item_ids = {
            item_id
            for fragment in fragments
            for item_id in fragment.referenced_item_ids()
            if item_id not in set(source_item_ids) | fragment_item_ids
        }
        closure_refs = tuple(ItemReference(item_id=item_id) for item_id in sorted(nested_item_ids))
        ordered_fragment_refs = tuple(
            sorted(fragment_refs, key=lambda item: (item.evidence_id, item.item_id))
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
        for evidence_id, (reference, fragment) in by_evidence_id.items():
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
            graph[evidence_id] = dependencies
        _reject_cycles(graph)

    async def _validate_source_records(self, scope, fragments, source_item_ids):
        records_by_item = {
            item_id: await self._reader.read_source_records(scope, item_id)
            for item_id in source_item_ids
        }
        for item_id, records in records_by_item.items():
            record_ids = tuple(record.record_id for record in records)
            if len(record_ids) != len(set(record_ids)):
                raise EvidenceScopeViolationError()
        for fragment in fragments:
            for reference in fragment.source_record_refs:
                if reference.record_id not in {
                    record.record_id for record in records_by_item[reference.item_id]
                }:
                    raise EvidenceScopeViolationError()
        return records_by_item

    def _build_cells(self, scope, by_evidence_id):
        cells = []
        for window in scope.time_windows:
            for role, parameters in (
                (EvidenceCellRole.TARGET, scope.requested_parameters),
                (EvidenceCellRole.REQUIRED_SUPPORT, scope.supporting_parameters),
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
                            }
                        )
                    )
                    if blockers:
                        status = EvidenceCellStatus.BLOCKED
                        reasons = blockers
                    elif direct or indirect:
                        status = EvidenceCellStatus.SUPPORTED
                        reasons = tuple(
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
                        )
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


def _ensure_unique_refs(fragment_refs, tool_result_refs):
    fragment_keys = tuple((item.item_id, item.evidence_id) for item in fragment_refs)
    tool_ids = tuple(item.item_id for item in tool_result_refs)
    if (
        not fragment_refs
        or len(fragment_keys) != len(set(fragment_keys))
        or len(tool_ids) != len(set(tool_ids))
    ):
        raise EvidenceScopeViolationError()


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

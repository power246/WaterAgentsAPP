from hashlib import sha256

from app.application.evidence_policy import (
    IndirectEvidenceContext,
    apply_rag_policy,
    resolve_applicability,
    temporal_relation_matches,
)
from app.application.evidence_tools.errors import EvidenceScopeViolationError
from app.application.evidence_tools.models import EvidenceToolReader, EvidenceToolWriter
from app.application.evidence_tools.scope import EvidenceSessionScope, EvidenceWindow
from app.application.query_understanding import (
    EvidenceSpatialRelation,
)
from app.domain import (
    CollectionFactPayload,
    CollectionStatus,
    CoverageStatus,
    DataQualityFlag,
    EvidenceApplicability,
    EvidenceCoverage,
    EvidenceDerivation,
    EvidenceFragment,
    EvidenceFragmentReference,
    EvidenceLocation,
    EvidenceQualityClass,
    EvidenceResultEligibility,
    ParameterCapabilityRegistry,
    Provenance,
    RelationSupportPayload,
    UpstreamResultPayload,
)


class EvidenceRelationMaterializer:
    def __init__(
        self,
        *,
        registry: ParameterCapabilityRegistry,
        reader: EvidenceToolReader,
        writer: EvidenceToolWriter,
    ) -> None:
        self._registry = registry
        self._reader = reader
        self._writer = writer

    async def materialize(
        self,
        scope: EvidenceSessionScope,
        references: tuple[EvidenceFragmentReference, ...],
        *,
        operation_id: str,
    ) -> tuple[EvidenceFragmentReference, ...]:
        stored = list(zip(
            references,
            await self._reader.read_fragments(scope, references),
            strict=True,
        ))
        created: list[EvidenceFragmentReference] = []
        existing_ids = {fragment.evidence_id for _, fragment in stored}
        for _ in range(max(1, len(scope.evidence_rule_plan.effective_rules))):
            added = False
            for rule in scope.evidence_rule_plan.effective_rules:
                capability = self._registry.get(rule.output_parameter)
                if capability is None:
                    raise EvidenceScopeViolationError()
                for window in scope.time_windows:
                    selected = self._select_inputs(scope, rule, window, tuple(stored))
                    if selected is None:
                        continue
                    input_refs = tuple(item[0] for item in selected)
                    evidence_id = _relation_evidence_id(
                        rule.rule_id,
                        window.window_id,
                        input_refs,
                    )
                    if evidence_id in existing_ids:
                        continue
                    context = IndirectEvidenceContext(
                        rule=rule,
                        input_parameters=tuple(
                            item[1].parameter_refs[0] for item in selected
                        ),
                        input_windows=tuple(item[1].time_range for item in selected),
                        analysis_timezone=scope.analysis_timezone,
                        same_location=all(
                            _is_same_location(scope, item[1]) for item in selected
                        ),
                        reference_complete=True,
                        upstream_result_complete=all(
                            isinstance(item[1].payload, UpstreamResultPayload)
                            for item in selected
                        ),
                    )
                    applicability = resolve_applicability(
                        self._registry,
                        capability,
                        scope.agent_comid,
                        window.time_range,
                        indirect_context=context,
                    )
                    if applicability is not EvidenceApplicability.ALLOWED_INDIRECT:
                        continue
                    fragment = self._build_fragment(
                        scope,
                        window,
                        rule,
                        selected,
                        evidence_id=evidence_id,
                        operation_id=operation_id,
                    )
                    reference = await self._writer.write_fragment(
                        scope,
                        operation_id=operation_id,
                        fragment=fragment,
                    )
                    stored.append((reference, fragment))
                    created.append(reference)
                    existing_ids.add(evidence_id)
                    added = True
            if not added:
                break
        return tuple(created)

    @staticmethod
    def _select_inputs(scope, rule, window, stored):
        selected = []
        for parameter in rule.input_parameters:
            candidates = tuple(
                (reference, fragment)
                for reference, fragment in stored
                if parameter in fragment.parameter_refs
                and fragment.collection_status is CollectionStatus.AVAILABLE
                and fragment.quality_class
                in {EvidenceQualityClass.VALID, EvidenceQualityClass.SUSPICIOUS}
                and fragment.result_eligibility
                is not EvidenceResultEligibility.EXPLANATORY_ONLY
                and not isinstance(fragment.payload, CollectionFactPayload)
                and fragment.relation_id != rule.rule_id
                and temporal_relation_matches(
                    rule.temporal_relation,
                    fragment.time_range,
                    window.time_range,
                    analysis_timezone=scope.analysis_timezone,
                )
                and (
                    isinstance(fragment.payload, UpstreamResultPayload)
                    == (
                        rule.spatial_relation
                        is EvidenceSpatialRelation.UPSTREAM_TO_DOWNSTREAM
                    )
                )
            )
            if not candidates:
                return None
            selected.append(min(candidates, key=_candidate_sort_key))
        return tuple(selected)

    def _build_fragment(
        self,
        scope,
        window,
        rule,
        selected,
        *,
        evidence_id,
        operation_id,
    ):
        fragments = tuple(item[1] for item in selected)
        quality = (
            EvidenceQualityClass.SUSPICIOUS
            if any(
                item.quality_class is EvidenceQualityClass.SUSPICIOUS
                for item in fragments
            )
            else EvidenceQualityClass.VALID
        )
        flags = tuple(
            sorted(
                {flag for item in fragments for flag in item.quality_flags},
                key=lambda item: item.value,
            )
        )
        coverage = _relation_coverage(window, fragments)
        fragment = EvidenceFragment(
            evidence_id=evidence_id,
            source="evidence_rule_materializer",
            collection_status=CollectionStatus.AVAILABLE,
            target_comid=scope.agent_comid,
            location=EvidenceLocation(
                point=scope.target_node.point,
                comid=scope.agent_comid,
                station_id=None,
                distance_to_target_km=0,
            ),
            time_range=window.time_range,
            parameter_refs=(rule.output_parameter,),
            data_type="relation_support",
            applicability=EvidenceApplicability.ALLOWED_INDIRECT,
            relation_id=rule.rule_id,
            series_identity=None,
            quality_class=quality,
            quality_flags=flags,
            coverage=coverage,
            payload=RelationSupportPayload(
                rule_id=rule.rule_id,
                plan_digest=scope.evidence_rule_plan.plan_digest,
                input_evidence_refs=tuple(item[0] for item in selected),
                rationale=rule.rationale,
            ),
            derivation=EvidenceDerivation(
                algorithm="evidence_relation_materialization",
                algorithm_version=1,
                policy_id=rule.rule_id,
                registry_digest=self._registry.manifest_digest,
            ),
            provenance=Provenance(
                operation_id=operation_id,
                registry_digest=self._registry.manifest_digest,
            ),
            result_eligibility=EvidenceResultEligibility.INFERRED_ONLY,
        )
        return apply_rag_policy(
            fragment,
            operation_complete=True,
            reference_complete=True,
            redacted=True,
        )


def _candidate_sort_key(item):
    reference, fragment = item
    rank = {
        EvidenceApplicability.DIRECT_MEASUREMENT: 0,
        EvidenceApplicability.CONTEXTUAL_ONLY: 1,
        EvidenceApplicability.ALLOWED_INDIRECT: 2,
    }[fragment.applicability]
    return rank, fragment.evidence_id, reference.item_id


def _is_same_location(scope, fragment):
    return (
        fragment.location.comid == scope.agent_comid
        or fragment.location.point == scope.target_node.point
        or any(
            station.station_id == fragment.location.station_id
            for station in scope.station_refs
        )
    )


def _relation_coverage(window, fragments):
    seconds = int(
        (window.time_range.end - window.time_range.start).total_seconds()
    )
    statuses = {item.coverage.status for item in fragments}
    if statuses == {CoverageStatus.COMPLETE}:
        status = CoverageStatus.COMPLETE
    elif CoverageStatus.UNKNOWN in statuses:
        status = CoverageStatus.UNKNOWN
    elif CoverageStatus.INSUFFICIENT in statuses:
        status = CoverageStatus.INSUFFICIENT
    else:
        status = CoverageStatus.PARTIAL
    return EvidenceCoverage(
        status=status,
        observed_count=len(fragments),
        covered_duration_seconds=min(
            seconds,
            *(item.coverage.covered_duration_seconds for item in fragments),
        ),
        window_duration_seconds=seconds,
    )


def _relation_evidence_id(rule_id, window_id, references):
    identity = "|".join(
        (
            rule_id,
            window_id,
            *(f"{item.item_id}:{item.evidence_id}" for item in references),
        )
    )
    return f"evidence:sha256:{sha256(identity.encode()).hexdigest()}"

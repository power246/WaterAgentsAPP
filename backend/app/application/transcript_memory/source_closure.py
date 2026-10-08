from dataclasses import dataclass
from typing import Annotated

from pydantic import Field, field_validator

from app.application.transcript_memory.eligibility import (
    FinalEligibilityContext,
    evaluate_final_fragment_eligibility,
    evaluate_final_result_eligibility,
)
from app.application.transcript_memory.source_models import RAGCaseSource
from app.application.transcript_memory.source_reader import TypedRAGSourceItem
from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.evidence.evidence_fragment import EvidenceFragment
from app.domain.hydrology.hydrology_participation import HydrologyParticipation
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.inference.inference_result import InferenceResult
from app.domain.memory_rag.historical_case_ref import HistoricalCaseRef
from app.domain.memory_rag.prepared_rag_case_projection import (
    PreparedRAGCaseProjectionV1,
)


class RAGSourceClosureError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class SourceClosureExpectation(DomainModel):
    active_generation_id: NonEmptyString
    case_ref: HistoricalCaseRef
    projection_digest: NonEmptyString
    authorized_comids: Annotated[tuple[NonEmptyString, ...], Field(min_length=1)]
    authorized_parameter_keys: Annotated[
        tuple[ParameterKey, ...],
        Field(min_length=1),
    ]
    authorized_source_window_ids: Annotated[
        tuple[NonEmptyString, ...],
        Field(min_length=1),
    ]

    @field_validator(
        "authorized_comids",
        "authorized_parameter_keys",
        "authorized_source_window_ids",
    )
    @classmethod
    def validate_unique_scope(cls, value: tuple[object, ...]) -> tuple[object, ...]:
        return ensure_unique_tuple(value, field_name="source closure scope")


@dataclass(frozen=True, slots=True)
class ValidatedRAGCaseSource:
    source: RAGCaseSource
    eligible_fragments: tuple[TypedRAGSourceItem[EvidenceFragment], ...]


def validate_source_closure(
    source: RAGCaseSource,
    *,
    projection: PreparedRAGCaseProjectionV1 | None = None,
    expectation: SourceClosureExpectation | None = None,
) -> ValidatedRAGCaseSource:
    root_context = FinalEligibilityContext(
        run_status=source.run_status,
        session=source.session,
        persistence_confirmed=source.persistence_confirmed,
        source=source.result,
        confirmed_references=source.confirmed_references,
    )
    root_decision = evaluate_final_result_eligibility(root_context)
    if not root_decision.eligible:
        raise RAGSourceClosureError(root_decision.reason.value)

    context_item = source.analysis_context
    topology_item = source.topology
    result_item = source.result
    result = result_item.body
    if (
        context_item.item.run_id != result.run_id
        or context_item.item.session_id is None
        or context_item.item.session_id == result.session_id
        or context_item.item.content_type != "query_understood"
        or context_item.item.content_schema_version != 1
        or topology_item.item.run_id != result.run_id
        or topology_item.item.session_id is not None
        or topology_item.item.content_type != "topology_ready"
        or topology_item.item.content_schema_version != 1
    ):
        raise RAGSourceClosureError("source_global_fact_ownership_mismatch")
    context = context_item.body
    topology = topology_item.body
    if context.target.comid != topology.target_comid:
        raise RAGSourceClosureError("source_context_topology_mismatch")
    result_node = next(
        (node for node in topology.nodes if node.comid == result.target_comid),
        None,
    )
    if result_node is None or result_node.participation is not HydrologyParticipation.ACTIVE:
        raise RAGSourceClosureError("source_result_outside_saved_graph")
    if (
        result.mode is not context.mode
        or result.time_range != context.time_range
        or result.analysis_timezone != context.analysis_timezone
        or result.time_resolution is not context.time_resolution
        or result.time_resolution_source is not context.time_resolution_source
        or result.requested_parameters != tuple(item.key for item in context.params_used)
    ):
        raise RAGSourceClosureError("source_result_context_mismatch")

    fragments_by_item = {
        fragment.item.item_id: fragment for fragment in source.fragments
    }
    if len(fragments_by_item) != len(source.fragments):
        raise RAGSourceClosureError("source_fragment_identity_duplicate")
    confirmed_by_item = {
        reference.item_id: reference for reference in source.confirmed_references
    }
    referenced_fragment_ids = {
        item_id
        for item_id in result_item.references
        if item_id in confirmed_by_item
        and confirmed_by_item[item_id].content_type == "evidence_fragment"
    }
    if not referenced_fragment_ids <= set(fragments_by_item):
        raise RAGSourceClosureError("source_fragment_closure_incomplete")

    eligible_fragments = tuple(
        fragment
        for fragment in source.fragments
        if evaluate_final_fragment_eligibility(
            FinalEligibilityContext(
                run_status=source.run_status,
                session=source.session,
                persistence_confirmed=source.persistence_confirmed,
                source=fragment,
                confirmed_references=source.confirmed_references,
            ),
            root=result_item,
        ).eligible
    )
    if expectation is not None:
        if projection is None:
            raise RAGSourceClosureError("source_projection_required")
        validate_projection_expectation(
            source,
            projection=projection,
            expectation=expectation,
        )
    return ValidatedRAGCaseSource(
        source=source,
        eligible_fragments=eligible_fragments,
    )


def validate_projection_expectation(
    source: RAGCaseSource,
    *,
    projection: PreparedRAGCaseProjectionV1,
    expectation: SourceClosureExpectation,
) -> None:
    if (
        expectation.active_generation_id != projection.case_ref.generation_id
        or expectation.case_ref != projection.case_ref
        or expectation.projection_digest != projection.projection_digest
        or projection.case_ref.analysis_context_item_id
        != str(source.analysis_context.item.item_id)
        or projection.case_ref.result_item_id != str(source.result.item.item_id)
    ):
        raise RAGSourceClosureError("source_active_projection_mismatch")
    if projection.case_ref.target_comid not in expectation.authorized_comids:
        raise RAGSourceClosureError("source_scope_comid_mismatch")
    if not set(projection.parameter_keys) & set(
        expectation.authorized_parameter_keys
    ):
        raise RAGSourceClosureError("source_scope_parameter_mismatch")
    if not set(projection.window_ids) & set(
        expectation.authorized_source_window_ids
    ):
        raise RAGSourceClosureError("source_scope_window_mismatch")

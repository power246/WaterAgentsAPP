from typing import Literal, Self

from pydantic import field_validator, model_validator

from app.domain.common import DomainModel, NonEmptyString, UtcDatetime, ensure_unique_tuple
from app.domain.evidence.collection_status import CollectionStatus
from app.domain.evidence.evidence_fragment_reference import EvidenceFragmentReference
from app.domain.evidence.evidence_tool_counts import EvidenceToolCounts
from app.domain.evidence.evidence_tool_execution_status import (
    EvidenceToolExecutionStatus,
)
from app.domain.evidence.item_reference import ItemReference
from app.domain.evidence.safe_evidence_error import SafeEvidenceError
from app.domain.evidence.source_record_reference import SourceRecordReference


class EvidenceToolResult(DomainModel):
    schema_version: Literal[1] = 1
    tool_call_id: NonEmptyString
    operation_id: NonEmptyString
    tool_name: NonEmptyString
    execution_status: EvidenceToolExecutionStatus
    collection_status: CollectionStatus | None
    manifest_refs: tuple[ItemReference, ...] = ()
    fragment_refs: tuple[EvidenceFragmentReference, ...] = ()
    collection_fact_refs: tuple[EvidenceFragmentReference, ...] = ()
    source_record_refs: tuple[SourceRecordReference, ...] = ()
    counts: EvidenceToolCounts
    deduplicated: bool = False
    deduplicated_from_operation_id: NonEmptyString | None = None
    deduplicated_from_result_ref: ItemReference | None = None
    safe_error: SafeEvidenceError | None = None
    completed_at: UtcDatetime

    @field_validator("fragment_refs", "collection_fact_refs")
    @classmethod
    def validate_unique_refs(
        cls,
        value: tuple[EvidenceFragmentReference, ...],
    ) -> tuple[EvidenceFragmentReference, ...]:
        evidence_ids = tuple(reference.evidence_id for reference in value)
        ensure_unique_tuple(evidence_ids, field_name="tool result evidence IDs")
        return value

    @field_validator("manifest_refs")
    @classmethod
    def validate_manifest_refs(
        cls,
        value: tuple[ItemReference, ...],
    ) -> tuple[ItemReference, ...]:
        return ensure_unique_tuple(value, field_name="tool result manifest refs")

    @field_validator("source_record_refs")
    @classmethod
    def validate_unique_record_refs(
        cls,
        value: tuple[SourceRecordReference, ...],
    ) -> tuple[SourceRecordReference, ...]:
        return ensure_unique_tuple(value, field_name="tool result source record refs")

    @model_validator(mode="after")
    def validate_status_matrix(self) -> Self:
        if self.collection_status is None and self.manifest_refs:
            raise ValueError("manifest_refs require collection_status")
        if self.collection_status is not None and not self.manifest_refs:
            raise ValueError("collection_status requires manifest_refs")

        if self.execution_status is EvidenceToolExecutionStatus.COMPLETED:
            if self.collection_status is CollectionStatus.UNAVAILABLE:
                raise ValueError("completed execution cannot be unavailable")
            if self.safe_error is not None:
                raise ValueError("completed execution must not contain safe_error")
        elif self.execution_status is EvidenceToolExecutionStatus.FAILED:
            if self.collection_status not in {
                None,
                CollectionStatus.UNAVAILABLE,
                CollectionStatus.INVALID,
            }:
                raise ValueError(
                    "failed execution requires null, unavailable, or invalid"
                )
            if self.safe_error is None:
                raise ValueError("failed execution requires safe_error")
        else:
            if self.collection_status not in {None, CollectionStatus.UNAVAILABLE}:
                raise ValueError("cancelled execution may only be null or unavailable")
            if self.safe_error is None:
                raise ValueError("cancelled execution requires safe_error")

        fragment_ids = {reference.evidence_id for reference in self.fragment_refs}
        fact_ids = {reference.evidence_id for reference in self.collection_fact_refs}
        if fragment_ids & fact_ids:
            raise ValueError("fragment and collection fact refs must be disjoint")
        if self.counts.fragments != len(fragment_ids | fact_ids):
            raise ValueError("fragment count does not match references")
        if self.deduplicated:
            if (
                self.deduplicated_from_operation_id is None
                or self.deduplicated_from_result_ref is None
            ):
                raise ValueError(
                    "deduplicated result requires source operation and result"
                )
            if self.deduplicated_from_operation_id == self.operation_id:
                raise ValueError("deduplicated source operation must be different")
        elif (
            self.deduplicated_from_operation_id is not None
            or self.deduplicated_from_result_ref is not None
        ):
            raise ValueError("non-deduplicated result must not name source result")
        return self

    def referenced_item_ids(self) -> tuple[str, ...]:
        item_ids: list[str] = []
        item_ids.extend(reference.item_id for reference in self.manifest_refs)
        item_ids.extend(reference.item_id for reference in self.fragment_refs)
        item_ids.extend(reference.item_id for reference in self.collection_fact_refs)
        item_ids.extend(reference.item_id for reference in self.source_record_refs)
        if self.deduplicated_from_result_ref is not None:
            item_ids.append(self.deduplicated_from_result_ref.item_id)
        return tuple(dict.fromkeys(item_ids))

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from app.application.evidence_policy import PolicyObservation
from app.application.evidence_tools.scope import EvidenceSessionScope
from app.domain import (
    CollectionStatus,
    EvidenceFragment,
    EvidenceFragmentReference,
    EvidenceToolRequest,
    EvidenceToolResult,
    InferenceMode,
    ItemReference,
    ProviderCollectionBatch,
    SourceRecord,
    SourceRecordReference,
)


class EvidencePolicyClass(str, Enum):
    CONDITIONAL_MANDATORY = "conditional_mandatory"
    OPTIONAL = "optional"
    PRODUCER_SLOT = "producer_slot"
    DETERMINISTIC = "deterministic"


class EvidenceProducerStatus(str, Enum):
    READY = "ready"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class EvidenceToolLimits:
    provider_timeout_seconds: float = 20
    total_deadline_seconds: float = 90
    collection_calls: int = 12
    transform_calls: int = 24
    pages_per_operation: int = 10
    records_per_operation: int = 10_000
    response_bytes_per_request: int = 8_388_608
    inspection_default_page_size: int = 50
    inspection_max_page_size: int = 200
    preview_max_records: int = 32
    preview_max_bytes: int = 16_384
    facility_max_centers: int = 32

    def __post_init__(self) -> None:
        positive = (
            self.provider_timeout_seconds,
            self.total_deadline_seconds,
            self.collection_calls,
            self.transform_calls,
            self.pages_per_operation,
            self.records_per_operation,
            self.response_bytes_per_request,
            self.inspection_default_page_size,
            self.inspection_max_page_size,
            self.preview_max_records,
            self.preview_max_bytes,
            self.facility_max_centers,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("evidence tool limits must be positive")
        if self.inspection_default_page_size > self.inspection_max_page_size:
            raise ValueError("default inspection size cannot exceed its maximum")


@dataclass(frozen=True, slots=True)
class EvidenceToolDescriptor:
    name: str
    purpose: str
    request_model: type[EvidenceToolRequest]
    result_model: type[EvidenceToolResult]
    policy_class: EvidencePolicyClass
    allowed_modes: tuple[InferenceMode, ...]
    scope_requirements: tuple[str, ...]
    max_calls_per_session: int
    max_records_per_call: int
    max_result_preview_records: int
    max_result_preview_bytes: int
    producer_status: EvidenceProducerStatus
    allowed_option_fields: tuple[str, ...] = ()
    allowed_aggregation_methods: tuple[str, ...] = ()
    allowed_target_unit_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceCollectionArtifacts:
    manifest_ref: ItemReference
    source_record_refs: tuple[SourceRecordReference, ...]
    fragment_refs: tuple[EvidenceFragmentReference, ...] = ()
    collection_fact_refs: tuple[EvidenceFragmentReference, ...] = ()


@dataclass(frozen=True, slots=True)
class StoredPolicyObservations:
    observations: tuple[PolicyObservation, ...]
    collection_complete: bool
    expected_cadence_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class EvidenceInspectionPage:
    source_item_id: str
    offset: int
    limit: int
    total_records: int
    records: tuple[SourceRecord, ...]
    record_refs: tuple[SourceRecordReference, ...]
    next_offset: int | None


class EvidenceToolWriter(Protocol):
    async def write_call(
        self,
        scope: EvidenceSessionScope,
        request: EvidenceToolRequest,
        *,
        operation_id: str,
    ) -> ItemReference: ...

    async def write_collection(
        self,
        scope: EvidenceSessionScope,
        *,
        operation_id: str,
        batches: tuple[ProviderCollectionBatch[SourceRecord], ...],
    ) -> EvidenceCollectionArtifacts: ...

    async def write_fragment(
        self,
        scope: EvidenceSessionScope,
        *,
        operation_id: str,
        fragment: EvidenceFragment,
    ) -> EvidenceFragmentReference: ...

    async def write_result(
        self,
        scope: EvidenceSessionScope,
        result: EvidenceToolResult,
    ) -> ItemReference: ...


class EvidenceToolReader(Protocol):
    async def read_fragment(
        self,
        scope: EvidenceSessionScope,
        reference: EvidenceFragmentReference,
    ) -> EvidenceFragment: ...

    async def read_policy_observations(
        self,
        scope: EvidenceSessionScope,
        references: tuple[SourceRecordReference, ...],
    ) -> StoredPolicyObservations: ...

    async def read_source_records(
        self,
        scope: EvidenceSessionScope,
        source_item_id: str,
    ) -> tuple[SourceRecord, ...]: ...


class EvidenceClock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class EvidenceOperationValue:
    collection_status: CollectionStatus | None = None
    manifest_ref: ItemReference | None = None
    fragment_refs: tuple[EvidenceFragmentReference, ...] = ()
    collection_fact_refs: tuple[EvidenceFragmentReference, ...] = ()
    source_record_refs: tuple[SourceRecordReference, ...] = ()
    requests: int = 0
    pages: int = 0
    records: int = 0
    cacheable: bool = True
    safe_error_code: str | None = None
    inspection_page: EvidenceInspectionPage | None = None

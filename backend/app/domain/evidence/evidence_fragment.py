from typing import Literal, Self

from pydantic import field_validator, model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.evidence.aggregate_payload import AggregatePayload
from app.domain.evidence.collection_fact_payload import CollectionFactPayload
from app.domain.evidence.collection_status import CollectionStatus
from app.domain.evidence.context_payload import ContextPayload
from app.domain.evidence.conversion_payload import ConversionPayload
from app.domain.evidence.data_quality import DataQualityFlag
from app.domain.evidence.direct_record_payload import DirectRecordPayload
from app.domain.evidence.evidence_applicability import EvidenceApplicability
from app.domain.evidence.evidence_coverage import EvidenceCoverage
from app.domain.evidence.evidence_derivation import EvidenceDerivation
from app.domain.evidence.evidence_location import EvidenceLocation
from app.domain.evidence.evidence_payload import EvidencePayload
from app.domain.evidence.evidence_quality_class import EvidenceQualityClass
from app.domain.evidence.evidence_result_eligibility import (
    EvidenceResultEligibility,
)
from app.domain.evidence.historical_inference_payload import (
    HistoricalInferencePayload,
)
from app.domain.evidence.provenance import Provenance
from app.domain.evidence.source_record_reference import SourceRecordReference
from app.domain.evidence.upstream_result_payload import UpstreamResultPayload
from app.domain.evidence.relation_support_payload import RelationSupportPayload
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.time.time_range import TimeRange


class EvidenceFragment(DomainModel):
    schema_version: Literal[1] = 1
    evidence_id: NonEmptyString
    source: NonEmptyString
    collection_status: CollectionStatus
    target_comid: NonEmptyString
    location: EvidenceLocation
    time_range: TimeRange
    parameter_refs: tuple[ParameterKey, ...]
    data_type: NonEmptyString
    applicability: EvidenceApplicability
    relation_id: NonEmptyString | None = None
    series_identity: NonEmptyString | None = None
    quality_class: EvidenceQualityClass | None
    quality_flags: tuple[DataQualityFlag, ...] = ()
    coverage: EvidenceCoverage
    payload: EvidencePayload
    source_record_refs: tuple[SourceRecordReference, ...] = ()
    derivation: EvidenceDerivation | None = None
    provenance: Provenance
    result_eligibility: EvidenceResultEligibility
    rag_eligible: bool = False

    @field_validator("parameter_refs")
    @classmethod
    def validate_parameter_refs(
        cls,
        value: tuple[ParameterKey, ...],
    ) -> tuple[ParameterKey, ...]:
        if not value:
            raise ValueError("parameter_refs must not be empty")
        return ensure_unique_tuple(value, field_name="parameter_refs")

    @field_validator("quality_flags", "source_record_refs")
    @classmethod
    def validate_unique_tuple(cls, value: tuple[object, ...]) -> tuple[object, ...]:
        return ensure_unique_tuple(value, field_name="evidence references or flags")

    @model_validator(mode="after")
    def validate_payload_contract(self) -> Self:
        if (
            self.applicability is EvidenceApplicability.ALLOWED_INDIRECT
            and self.relation_id is None
        ):
            raise ValueError("allowed_indirect evidence requires relation_id")
        if (
            self.applicability is EvidenceApplicability.DIRECT_MEASUREMENT
            and self.relation_id is not None
        ):
            raise ValueError("direct_measurement evidence must not declare relation_id")
        if (
            self.quality_class is EvidenceQualityClass.INVALID
            and self.rag_eligible
        ):
            raise ValueError("invalid evidence must not be RAG eligible")
        if (
            self.quality_class is EvidenceQualityClass.INVALID
            and self.result_eligibility
            is not EvidenceResultEligibility.EXPLANATORY_ONLY
        ):
            raise ValueError("invalid evidence must be explanatory only")

        source_refs = set(self.source_record_refs)
        if isinstance(self.payload, CollectionFactPayload):
            if self.collection_status is not self.payload.status:
                raise ValueError("collection fact status must match fragment status")
            if self.applicability is not EvidenceApplicability.CONTEXTUAL_ONLY:
                raise ValueError("collection fact must be contextual_only")
            if self.quality_class is not None:
                raise ValueError("collection fact must not declare quality_class")
            if self.rag_eligible:
                raise ValueError("collection fact must not be RAG eligible")
            if (
                self.result_eligibility
                is not EvidenceResultEligibility.EXPLANATORY_ONLY
            ):
                raise ValueError("collection fact must be explanatory only")
            if self.derivation is not None:
                raise ValueError("collection fact must not declare derivation")
            if self.series_identity is None:
                raise ValueError("collection fact requires series_identity")
            if source_refs != set(self.payload.record_refs):
                raise ValueError("collection fact record reference closure is incomplete")
            return self

        if self.collection_status is not CollectionStatus.AVAILABLE:
            raise ValueError("non-available fragments must use collection_fact payload")
        if self.quality_class is None:
            raise ValueError("available evidence requires quality_class")

        if isinstance(self.payload, DirectRecordPayload):
            if self.applicability is not EvidenceApplicability.DIRECT_MEASUREMENT:
                raise ValueError("direct record payload must be direct_measurement")
            if source_refs != {self.payload.record_ref}:
                raise ValueError("direct record reference closure is incomplete")
            if self.series_identity is None:
                raise ValueError("direct record requires series_identity")
            if self.derivation is not None:
                raise ValueError("direct record must not declare derivation")
            if (
                self.quality_class is not EvidenceQualityClass.INVALID
                and self.result_eligibility
                is not EvidenceResultEligibility.OBSERVED_OR_INFERRED
            ):
                raise ValueError("usable direct records must retain observed eligibility")
        elif isinstance(self.payload, AggregatePayload):
            if self.applicability is not EvidenceApplicability.DIRECT_MEASUREMENT:
                raise ValueError("aggregate payload must be direct_measurement")
            if source_refs != set(self.payload.input_record_refs):
                raise ValueError("aggregate record reference closure is incomplete")
            if self.series_identity is None or self.derivation is None:
                raise ValueError("aggregate requires series_identity and derivation")
            if self.result_eligibility is not EvidenceResultEligibility.INFERRED_ONLY:
                raise ValueError("aggregate evidence is inferred only")
        elif isinstance(self.payload, ConversionPayload):
            if self.derivation is None:
                raise ValueError("conversion requires derivation")
            if self.source_record_refs:
                raise ValueError("conversion references evidence, not source records")
            if self.result_eligibility is EvidenceResultEligibility.EXPLANATORY_ONLY:
                raise ValueError("usable conversion must remain a result candidate")
        elif isinstance(self.payload, ContextPayload):
            if self.applicability is not EvidenceApplicability.CONTEXTUAL_ONLY:
                raise ValueError("context payload must be contextual_only")
            if source_refs != set(self.payload.record_refs):
                raise ValueError("context record reference closure is incomplete")
            if self.derivation is not None:
                raise ValueError("context payload must not declare derivation")
            if (
                self.quality_class is not EvidenceQualityClass.INVALID
                and self.result_eligibility
                is not EvidenceResultEligibility.INFERRED_ONLY
            ):
                raise ValueError("usable contextual evidence is inferred only")
        elif isinstance(self.payload, UpstreamResultPayload):
            if self.applicability is not EvidenceApplicability.CONTEXTUAL_ONLY:
                raise ValueError("raw upstream result must be contextual_only")
            if self.source_record_refs or self.derivation is not None:
                raise ValueError("upstream result uses result closure references only")
            if (
                self.quality_class is not EvidenceQualityClass.INVALID
                and self.result_eligibility
                is not EvidenceResultEligibility.INFERRED_ONLY
            ):
                raise ValueError("upstream evidence is inferred only")
        elif isinstance(self.payload, RelationSupportPayload):
            if self.applicability is not EvidenceApplicability.ALLOWED_INDIRECT:
                raise ValueError("relation support must be allowed_indirect")
            if self.relation_id != self.payload.rule_id:
                raise ValueError("relation support rule identity must match fragment")
            if self.source_record_refs or self.derivation is None:
                raise ValueError("relation support requires evidence-only derivation")
            if self.result_eligibility is not EvidenceResultEligibility.INFERRED_ONLY:
                raise ValueError("relation support is inferred only")
        elif isinstance(self.payload, HistoricalInferencePayload):
            if self.applicability is not EvidenceApplicability.CONTEXTUAL_ONLY:
                raise ValueError("historical inference must be contextual_only")
            if self.source_record_refs or self.derivation is not None:
                raise ValueError("historical inference uses original item reference only")
            if (
                self.quality_class is not EvidenceQualityClass.INVALID
                and self.result_eligibility
                is not EvidenceResultEligibility.INFERRED_ONLY
            ):
                raise ValueError("historical evidence is inferred only")
        return self

    def referenced_item_ids(self) -> tuple[str, ...]:
        item_ids = [reference.item_id for reference in self.source_record_refs]
        if isinstance(self.payload, ConversionPayload):
            item_ids.append(self.payload.input_evidence_ref.item_id)
        elif isinstance(self.payload, UpstreamResultPayload):
            item_ids.append(self.payload.result_ref.item_id)
            item_ids.extend(reference.item_id for reference in self.payload.closure_refs)
        elif isinstance(self.payload, RelationSupportPayload):
            item_ids.extend(
                reference.item_id
                for reference in self.payload.input_evidence_refs
            )
        elif isinstance(self.payload, HistoricalInferencePayload):
            item_ids.append(self.payload.original_item_ref.item_id)
        return tuple(dict.fromkeys(item_ids))

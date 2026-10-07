import json
from hashlib import sha256
from typing import Literal, Self

from pydantic import field_validator, model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.evidence.data_quality import DataQualityFlag
from app.domain.evidence.evidence_coverage import EvidenceCoverage
from app.domain.evidence.evidence_result_eligibility import EvidenceResultEligibility
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.memory_rag.historical_case_ref import HistoricalCaseRef
from app.domain.memory_rag.rag_quality_tier import RAGQualityTier
from app.domain.time.time_range import TimeRange

_AUTO_DIGEST = "__auto_rag_fragment_projection_digest__"


class RAGFragmentProjectionV1(DomainModel):
    case_ref: HistoricalCaseRef
    projection_schema_version: Literal[1] = 1
    source_content_type: Literal["evidence_fragment"] = "evidence_fragment"
    source_content_schema_version: Literal[1] = 1
    source_fragment_item_id: NonEmptyString
    evidence_id: NonEmptyString
    parameter: ParameterKey
    window_id: NonEmptyString
    valid_time_range: TimeRange
    quality_tier: RAGQualityTier
    quality_flags: tuple[DataQualityFlag, ...] = ()
    coverage: EvidenceCoverage
    applicability: Literal[
        "direct_measurement",
        "allowed_indirect",
        "contextual_only",
    ]
    result_eligibility: EvidenceResultEligibility
    provenance_identity: NonEmptyString
    projection_digest: NonEmptyString = _AUTO_DIGEST

    @field_validator("quality_flags")
    @classmethod
    def validate_quality_flags(
        cls,
        value: tuple[DataQualityFlag, ...],
    ) -> tuple[DataQualityFlag, ...]:
        return ensure_unique_tuple(value, field_name="fragment quality flags")

    @model_validator(mode="after")
    def validate_projection_digest(self) -> Self:
        projection_values = self.model_dump(
            mode="json",
            exclude={"projection_digest"},
        )
        expected = f"sha256:{sha256(json.dumps(
            projection_values,
            allow_nan=False,
            ensure_ascii=False,
            separators=(',', ':'),
            sort_keys=True,
        ).encode('utf-8')).hexdigest()}"
        if self.projection_digest == _AUTO_DIGEST:
            object.__setattr__(self, "projection_digest", expected)
        elif self.projection_digest != expected:
            raise ValueError("fragment projection digest does not match canonical content")
        return self

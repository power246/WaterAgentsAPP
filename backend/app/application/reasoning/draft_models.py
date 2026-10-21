from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from app.application.guidelines.citation_resolver import GuidelineRefs
from app.application.reasoning.model_models import (
    ModelItemName,
    NonBlankText,
    WaterbodyJsonSchema,
)
from app.application.reasoning.prompt_policy import (
    canonical_json_digest,
    canonical_json_text,
)
from app.application.reasoning.working_context import require_safe_prompt_text
from app.domain import (
    AggregationMethod,
    AttributionBasis,
    AttributionDirection,
    AttributionMagnitude,
    CanonicalDecimal,
    DataQualityFlag,
    DomainFloat,
    DomainModel,
    EvidenceApplicability,
    IndeterminateReason,
    NonEmptyString,
    OutcomeStatus,
    ParameterKey,
    ResultSource,
    RiskLevel,
    TrendDirection,
    ensure_unique_tuple,
)
from app.domain.inference.value_range import validate_result_range


class DraftValidationIssue(DomainModel):
    code: ModelItemName
    path: NonEmptyString


class AttributionReferenceConstraint(DomainModel):
    evidence_ref: NonEmptyString
    current_waterbody_applicability: EvidenceApplicability
    dependency_evidence_refs: tuple[NonEmptyString, ...]


class CellSupportReferenceGroup(DomainModel):
    cell_id: NonEmptyString
    reference_options: Annotated[tuple[NonEmptyString, ...], Field(min_length=1)]


class ReasoningCellDraft(DomainModel):
    guideline_refs: GuidelineRefs = Field(default=None, exclude=True)
    cell_id: NonEmptyString
    outcome_status: OutcomeStatus
    representative_value: CanonicalDecimal | None
    value_lower: CanonicalDecimal | None = None
    value_upper: CanonicalDecimal | None = None
    target_unit_id: NonEmptyString | None
    source: ResultSource | None
    trend: TrendDirection | None
    risk_level: RiskLevel
    risk_rationale: NonBlankText
    confidence: Annotated[DomainFloat, Field(ge=0.0, le=1.0)]
    confidence_rationale: NonBlankText
    aggregation_method: AggregationMethod | None
    evidence_refs: tuple[NonEmptyString, ...]
    series_refs: tuple[NonEmptyString, ...]
    reason_codes: tuple[IndeterminateReason, ...]
    quality_flags: tuple[DataQualityFlag, ...]
    explanation: NonBlankText

    @field_validator("representative_value", "value_lower", "value_upper", mode="before")
    @classmethod
    def require_decimal_string(cls, value: object) -> object:
        if value is not None and not isinstance(value, str):
            raise ValueError("result values must be decimal strings")
        return value

    @field_validator(
        "risk_rationale",
        "confidence_rationale",
        "explanation",
    )
    @classmethod
    def validate_text(cls, value: str) -> str:
        return require_safe_prompt_text(value)

    @field_validator("evidence_refs", "series_refs")
    @classmethod
    def canonicalize_string_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        ensure_unique_tuple(value, field_name="reasoning draft references")
        return tuple(sorted(value))

    @field_validator("reason_codes", "quality_flags")
    @classmethod
    def canonicalize_enum_refs(cls, value: tuple[object, ...]) -> tuple[object, ...]:
        ensure_unique_tuple(value, field_name="reasoning draft facts")
        return tuple(sorted(value, key=lambda item: item.value))

    @model_validator(mode="after")
    def validate_outcome_shape(self) -> Self:
        if not self.evidence_refs:
            raise ValueError("supported reasoning draft requires evidence_refs")
        validate_result_range(
            self.outcome_status, self.representative_value,
            self.value_lower, self.value_upper,
        )
        if self.outcome_status is OutcomeStatus.QUANTIFIED:
            if (
                self.representative_value is None
                or self.target_unit_id is None
                or self.source is None
            ):
                raise ValueError("quantified draft requires value, unit, and source")
            if self.reason_codes:
                raise ValueError("quantified draft cannot contain reason_codes")
        elif self.outcome_status is OutcomeStatus.RANGE_ESTIMATE:
            if self.representative_value is not None or self.target_unit_id is None:
                raise ValueError("range estimate draft requires a unit and no point value")
            if self.source not in (ResultSource.INFERRED, ResultSource.PREDICTED):
                raise ValueError("range estimate draft requires inferred or predicted source")
            if self.aggregation_method is not None or self.reason_codes:
                raise ValueError("range estimate draft cannot contain aggregation or reason codes")
        elif self.outcome_status is OutcomeStatus.DIRECTIONAL:
            if self.representative_value is not None or self.target_unit_id is not None:
                raise ValueError("directional draft cannot contain value or unit")
            if self.source is None:
                raise ValueError("directional draft requires source")
            if self.aggregation_method is not None:
                raise ValueError("directional draft cannot contain aggregation_method")
            if (
                self.trend in {None, TrendDirection.UNCERTAIN}
                and self.risk_level is RiskLevel.INDETERMINATE
            ):
                raise ValueError("directional draft requires a definite trend or risk")
            if self.reason_codes:
                raise ValueError("directional draft cannot contain reason_codes")
        else:
            if (
                self.representative_value is not None
                or self.target_unit_id is not None
                or self.source is not None
                or self.aggregation_method is not None
            ):
                raise ValueError(
                    "indeterminate draft cannot contain value, unit, source, or method"
                )
            if self.risk_level is not RiskLevel.INDETERMINATE:
                raise ValueError("indeterminate draft requires indeterminate risk")
            if self.trend not in {None, TrendDirection.UNCERTAIN}:
                raise ValueError("indeterminate draft cannot claim a definite trend")
            if not self.reason_codes:
                raise ValueError("indeterminate draft requires reason_codes")
        return self


class ReasoningBatchDraft(DomainModel):
    schema_version: Literal[1] = 1
    decision_type: Literal["reasoning_batch"] = "reasoning_batch"
    batch_id: NonEmptyString
    scope_digest: NonEmptyString
    cells: Annotated[tuple[ReasoningCellDraft, ...], Field(min_length=1)]


class SynthesisSeriesDraft(DomainModel):
    parameter: ParameterKey
    confidence: Annotated[DomainFloat, Field(ge=0.0, le=1.0)]
    confidence_rationale: NonBlankText
    contributing_cell_refs: tuple[NonEmptyString, ...]
    evidence_refs: tuple[NonEmptyString, ...]

    @field_validator("confidence_rationale")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return require_safe_prompt_text(value)

    @field_validator("contributing_cell_refs", "evidence_refs")
    @classmethod
    def canonicalize_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        ensure_unique_tuple(value, field_name="synthesis series references")
        return tuple(sorted(value))


class SynthesisWindowDraft(DomainModel):
    guideline_refs: GuidelineRefs = Field(default=None, exclude=True)
    window_id: NonEmptyString
    risk_level: RiskLevel
    risk_rationale: NonBlankText
    confidence: Annotated[DomainFloat, Field(ge=0.0, le=1.0)]
    confidence_rationale: NonBlankText
    contributing_cell_refs: tuple[NonEmptyString, ...]
    evidence_refs: tuple[NonEmptyString, ...]

    @field_validator("risk_rationale", "confidence_rationale")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return require_safe_prompt_text(value)

    @field_validator("contributing_cell_refs", "evidence_refs")
    @classmethod
    def canonicalize_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        ensure_unique_tuple(value, field_name="synthesis window references")
        return tuple(sorted(value))


class SynthesisOverallDraft(DomainModel):
    guideline_refs: GuidelineRefs = Field(default=None, exclude=True)
    risk_level: RiskLevel
    risk_rationale: NonBlankText
    confidence: Annotated[DomainFloat, Field(ge=0.0, le=1.0)]
    confidence_rationale: NonBlankText
    contributing_window_refs: Annotated[
        tuple[NonEmptyString, ...],
        Field(min_length=1),
    ]
    contributing_cell_refs: tuple[NonEmptyString, ...]
    evidence_refs: Annotated[tuple[NonEmptyString, ...], Field(min_length=1)]

    @field_validator("risk_rationale", "confidence_rationale")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return require_safe_prompt_text(value)

    @field_validator(
        "contributing_window_refs",
        "contributing_cell_refs",
        "evidence_refs",
    )
    @classmethod
    def canonicalize_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        ensure_unique_tuple(value, field_name="synthesis overall references")
        return tuple(sorted(value))


class AnalysisStepDraft(DomainModel):
    guideline_refs: GuidelineRefs = Field(default=None, exclude=True)
    type: ModelItemName
    title: NonBlankText
    content: NonBlankText
    evidence_refs: tuple[NonEmptyString, ...]
    result_cell_refs: tuple[NonEmptyString, ...]

    @field_validator("title", "content")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return require_safe_prompt_text(value)

    @field_validator("evidence_refs", "result_cell_refs")
    @classmethod
    def canonicalize_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        ensure_unique_tuple(value, field_name="analysis step references")
        return tuple(sorted(value))


class AnalysisDetailDraft(DomainModel):
    summary: NonBlankText
    steps: Annotated[tuple[AnalysisStepDraft, ...], Field(min_length=1)]

    @field_validator("summary")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return require_safe_prompt_text(value)


class AttributionFactorDraft(DomainModel):
    factor: NonBlankText
    direction: AttributionDirection
    magnitude: AttributionMagnitude
    basis: AttributionBasis
    explanation: NonBlankText
    evidence_refs: Annotated[tuple[NonEmptyString, ...], Field(min_length=1)]
    result_cell_refs: Annotated[tuple[NonEmptyString, ...], Field(min_length=1)]

    @field_validator("factor", "explanation")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return require_safe_prompt_text(value)

    @field_validator("evidence_refs", "result_cell_refs")
    @classmethod
    def canonicalize_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        ensure_unique_tuple(value, field_name="attribution references")
        return tuple(sorted(value))


class AttributionSummaryDraft(DomainModel):
    summary: NonBlankText
    primary_factors: tuple[AttributionFactorDraft, ...]

    @field_validator("summary")
    @classmethod
    def validate_text(cls, value: str) -> str:
        return require_safe_prompt_text(value)


class OverallSynthesisDraft(DomainModel):
    schema_version: Literal[1] = 1
    decision_type: Literal["overall_synthesis"] = "overall_synthesis"
    scope_digest: NonEmptyString
    series_assessments: Annotated[
        tuple[SynthesisSeriesDraft, ...],
        Field(min_length=1),
    ]
    window_assessments: Annotated[
        tuple[SynthesisWindowDraft, ...],
        Field(min_length=1),
    ]
    overall: SynthesisOverallDraft
    analysis_detail: AnalysisDetailDraft
    attribution_summary: AttributionSummaryDraft


class ValidatedReasoningBatch(DomainModel):
    batch_id: NonEmptyString
    scope_digest: NonEmptyString
    cells: tuple[ReasoningCellDraft, ...]
    validation_digest: NonEmptyString

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        expected = canonical_json_digest(
            self.model_dump(mode="json", exclude={"validation_digest"})
        )
        if self.validation_digest != expected:
            raise ValueError("validated batch digest does not match canonical content")
        return self


class ValidatedReasoningSet(DomainModel):
    batch_plan_digest: NonEmptyString
    batches: tuple[ValidatedReasoningBatch, ...]
    validation_digest: NonEmptyString

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        expected = canonical_json_digest(
            self.model_dump(mode="json", exclude={"validation_digest"})
        )
        if self.validation_digest != expected:
            raise ValueError("validated reasoning-set digest does not match content")
        return self


class ValidatedOverallSynthesis(DomainModel):
    scope_digest: NonEmptyString
    series_assessments: tuple[SynthesisSeriesDraft, ...]
    window_assessments: tuple[SynthesisWindowDraft, ...]
    overall: SynthesisOverallDraft
    analysis_detail: AnalysisDetailDraft
    attribution_summary: AttributionSummaryDraft
    validation_digest: NonEmptyString

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        expected = canonical_json_digest(
            self.model_dump(mode="json", exclude={"validation_digest"})
        )
        if self.validation_digest != expected:
            raise ValueError("validated synthesis digest does not match canonical content")
        return self


def reasoning_batch_response_schema() -> WaterbodyJsonSchema:
    return _response_schema("reasoning_cell_batch", ReasoningBatchDraft)


def overall_synthesis_response_schema() -> WaterbodyJsonSchema:
    return _response_schema("overall_synthesis", OverallSynthesisDraft)


def _response_schema(name: str, model: type[DomainModel]) -> WaterbodyJsonSchema:
    schema = model.model_json_schema()
    return WaterbodyJsonSchema(
        name=name,
        schema_text=canonical_json_text(schema),
    )

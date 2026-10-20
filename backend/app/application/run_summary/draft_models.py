from __future__ import annotations

from typing import Annotated, Literal, Self, get_args

from pydantic import Field, field_validator, model_validator

from app.application.analysis_runs.topology_result_models import (
    GraphCoverage,
    StableSourceReference,
)
from app.application.canonical_json import canonical_json_digest
from app.application.reasoning.model_models import NonBlankText
from app.application.reasoning.prompt_policy import canonical_json_text
from app.application.run_summary.models import RunSummaryJsonSchema
from app.domain import (
    CanonicalDecimal,
    DomainModel,
    NonEmptyString,
    OutcomeStatus,
    ensure_unique_tuple,
)
from app.domain.inference.value_range import validate_result_range

RunSummaryHighlightKind = Literal[
    "notable_value",
    "range_estimate",
    "high_risk",
    "directional_trend",
    "suspicious_quality",
    "insufficient_evidence",
    "invalid_branch",
    "topology_limit",
]
SUMMARY_HIGHLIGHT_KINDS = frozenset(get_args(RunSummaryHighlightKind))


class RunSummaryDraftHighlight(DomainModel):
    kind: RunSummaryHighlightKind
    text: NonBlankText
    source_ref: StableSourceReference
    display_value: CanonicalDecimal | None
    value_lower: CanonicalDecimal | None = None
    value_upper: CanonicalDecimal | None = None
    display_unit: NonEmptyString | None
    window_id: NonEmptyString | None

    @model_validator(mode="after")
    def validate_numeric_shape(self) -> Self:
        validate_result_range(
            OutcomeStatus.QUANTIFIED if self.display_value is not None
            else OutcomeStatus.RANGE_ESTIMATE if self.value_lower is not None
            else OutcomeStatus.DIRECTIONAL,
            self.display_value, self.value_lower, self.value_upper,
        )
        numeric = (
            self.display_value is not None or self.value_lower is not None,
            self.display_unit is not None,
            self.window_id is not None,
        )
        if any(numeric) and not all(numeric):
            raise ValueError("numeric highlight fields must appear together")
        if self.kind == "notable_value" and not all(numeric):
            raise ValueError("notable_value requires exact display fields")
        if self.kind == "range_estimate" and (
            self.display_value is not None or self.value_lower is None
        ):
            raise ValueError("range_estimate requires bounds without a point")
        if self.window_id is not None and self.window_id != self.source_ref.window_id:
            raise ValueError("highlight window must match its source reference")
        return self


class RunSummaryDraft(DomainModel):
    schema_version: Literal[1]
    language: NonEmptyString
    target_comid: NonEmptyString
    graph_coverage: GraphCoverage
    covered_comids: Annotated[tuple[NonEmptyString, ...], Field(min_length=1)]
    plain_language_summary: Annotated[NonBlankText, Field(max_length=12_000)]
    highlights: Annotated[
        tuple[RunSummaryDraftHighlight, ...],
        Field(max_length=32),
    ]

    @field_validator("covered_comids")
    @classmethod
    def validate_covered_comids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        ensure_unique_tuple(value, field_name="covered summary COMIDs")
        return value

    @field_validator("highlights")
    @classmethod
    def validate_highlights(
        cls,
        value: tuple[RunSummaryDraftHighlight, ...],
    ) -> tuple[RunSummaryDraftHighlight, ...]:
        ensure_unique_tuple(
            tuple((item.kind, item.source_ref) for item in value),
            field_name="summary highlight source/kind pairs",
        )
        return value


class ValidatedRunSummaryDraft(DomainModel):
    draft: RunSummaryDraft
    validation_digest: NonEmptyString

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        expected = canonical_json_digest(self.draft.model_dump(mode="json"))
        if self.validation_digest != expected:
            raise ValueError("validated summary digest does not match its draft")
        return self


def run_summary_response_schema() -> RunSummaryJsonSchema:
    schema = RunSummaryDraft.model_json_schema()
    _require_all_object_properties(schema)
    return RunSummaryJsonSchema(
        name="run_summary",
        schema_text=canonical_json_text(schema),
    )


def _require_all_object_properties(value: object) -> None:
    if isinstance(value, dict):
        value.pop("default", None)
        if value.get("type") == "object":
            value["required"] = list(value.get("properties", {}))
            value["additionalProperties"] = False
        for child in value.values():
            _require_all_object_properties(child)
    elif isinstance(value, list):
        for child in value:
            _require_all_object_properties(child)

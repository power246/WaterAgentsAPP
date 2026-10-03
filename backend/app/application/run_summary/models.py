from __future__ import annotations

import json
from enum import Enum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, StrictBool, StrictInt, field_validator, model_validator

from app.application.analysis_runs.models import SessionResultReference
from app.application.analysis_runs.topology_result_models import (
    DependencyStatus,
    GraphCoverage,
    StableSourceReference,
)
from app.application.reasoning.model_models import (
    JsonObjectText,
    ModelItemName,
    NonBlankText,
)
from app.application.canonical_json import canonical_json_digest
from app.application.transcripts.models import (
    GenerationStatus,
    MessageEndReason,
    TranscriptContentRef,
)
from app.domain.common import (
    CanonicalDecimal,
    DomainFloat,
    NonEmptyString,
    UtcDatetime,
    ensure_unique_tuple,
)
from app.domain.domain_enum import DomainEnum
from app.domain.domain_model import DomainModel
from app.domain.inference import (
    AssessmentAvailability,
    InferenceResult,
    InferenceMode,
    ModelExecutionSummary,
    ModelUsageSummary,
    RiskLevel,
)
from app.domain.model_catalog import ModelSelection
from app.domain.time.time_range import TimeRange
from app.domain.time.timezone import get_iana_timezone


class _VersionOneContent(DomainModel):
    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be integer 1")
        return value


class ReportWindowSummary(DomainModel):
    window_id: NonEmptyString
    overall_risk: RiskLevel
    availability: AssessmentAvailability
    confidence: Annotated[DomainFloat, Field(ge=0, le=1)]
    key_reasons: Annotated[tuple[NonEmptyString, ...], Field(max_length=8)] = ()
    source_refs: Annotated[
        tuple[StableSourceReference, ...],
        Field(max_length=32),
    ] = ()

    @field_validator("key_reasons")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return ensure_unique_tuple(value, field_name="window key reasons")

    @field_validator("source_refs")
    @classmethod
    def canonicalize_source_refs(
        cls,
        value: tuple[StableSourceReference, ...],
    ) -> tuple[StableSourceReference, ...]:
        ensure_unique_tuple(value, field_name="window source refs")
        return tuple(sorted(value, key=_source_reference_key))


class NotableFinding(DomainModel):
    kind: NonEmptyString
    text: NonBlankText
    source_ref: StableSourceReference
    display_value: CanonicalDecimal | None = None
    display_unit: NonEmptyString | None = None
    window_id: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_display_value(self) -> Self:
        numeric_fields = (
            self.display_value is not None,
            self.display_unit is not None,
            self.window_id is not None,
        )
        if any(numeric_fields) and not all(numeric_fields):
            raise ValueError("numeric finding display fields must appear together")
        if self.window_id is not None and self.source_ref.window_id != self.window_id:
            raise ValueError("finding window_id must match source_ref")
        return self


class _WaterbodyResultProjection(DomainModel):
    target_comid: NonEmptyString
    session_id: UUID
    graph_position: Annotated[StrictInt, Field(ge=0)]
    direct_upstream_comids: Annotated[
        tuple[NonEmptyString, ...],
        Field(max_length=32),
    ] = ()
    result_ref: SessionResultReference
    mode: InferenceMode
    overall_risk: RiskLevel
    availability: AssessmentAvailability
    confidence: Annotated[DomainFloat, Field(ge=0, le=1)]
    key_reasons: Annotated[tuple[NonEmptyString, ...], Field(max_length=12)] = ()
    window_summaries: Annotated[
        tuple[ReportWindowSummary, ...],
        Field(max_length=64),
    ] = ()
    notable_findings: Annotated[
        tuple[NotableFinding, ...],
        Field(max_length=16),
    ] = ()
    model_execution_summary: ModelExecutionSummary

    @field_validator("direct_upstream_comids")
    @classmethod
    def canonicalize_upstream_comids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        ensure_unique_tuple(value, field_name="projection upstream COMIDs")
        return tuple(sorted(value, key=_comid_key))

    @field_validator("key_reasons")
    @classmethod
    def canonicalize_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        ensure_unique_tuple(value, field_name="projection reasons")
        return tuple(sorted(value))

    @field_validator("window_summaries")
    @classmethod
    def canonicalize_windows(
        cls,
        value: tuple[ReportWindowSummary, ...],
    ) -> tuple[ReportWindowSummary, ...]:
        ensure_unique_tuple(
            tuple(item.window_id for item in value),
            field_name="report window IDs",
        )
        return tuple(sorted(value, key=lambda item: item.window_id))

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        for reference in self.source_refs:
            if (
                reference.run_id != self.result_ref.run_id
                or reference.session_id != self.session_id
                or reference.content_type != "inference_result"
                or reference.content_schema_version != 1
                or reference.result_id is None
            ):
                raise ValueError("projection source refs must remain in its session")
        return self

    @property
    def source_refs(self) -> tuple[StableSourceReference, ...]:
        return tuple(
            sorted(
                (
                    *(
                        reference
                        for item in self.window_summaries
                        for reference in item.source_refs
                    ),
                    *(item.source_ref for item in self.notable_findings),
                ),
                key=_source_reference_key,
            )
        )


class TargetResultProjection(_WaterbodyResultProjection):
    time_range: TimeRange
    analysis_timezone: NonEmptyString
    # Added at the M6 construction boundary.  Older M1 fixtures remain readable,
    # while the production builder below always supplies the complete safe domain
    # result rather than reconstructing it from prose or provider payloads.
    full_result: InferenceResult | None = None

    @field_validator("analysis_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        get_iana_timezone(value)
        return value

    @model_validator(mode="after")
    def validate_full_result(self) -> Self:
        result = self.full_result
        if result is None:
            return self
        if (
            result.run_id != self.result_ref.run_id
            or result.session_id != self.session_id
            or result.target_comid != self.target_comid
            or result.mode is not self.mode
            or result.time_range != self.time_range
            or result.analysis_timezone != self.analysis_timezone
            or result.overall_assessment.risk_level is not self.overall_risk
            or result.overall_assessment.availability is not self.availability
            or result.overall_confidence != self.confidence
            or result.model_execution_summary != self.model_execution_summary
        ):
            raise ValueError("full target result must match its summary projection")
        return self


class WaterbodyReportCard(DomainModel):
    target_comid: NonEmptyString
    session_id: UUID
    graph_position: Annotated[StrictInt, Field(ge=0)]
    direct_upstream_comids: Annotated[
        tuple[NonEmptyString, ...],
        Field(max_length=32),
    ] = ()
    dependency_status: DependencyStatus
    result_ref: SessionResultReference | None = None
    mode: InferenceMode | None = None
    overall_risk: RiskLevel | None = None
    availability: AssessmentAvailability | None = None
    confidence: Annotated[DomainFloat, Field(ge=0, le=1)] | None = None
    key_reasons: Annotated[tuple[NonEmptyString, ...], Field(max_length=12)] = ()
    window_summaries: Annotated[
        tuple[ReportWindowSummary, ...],
        Field(max_length=64),
    ] = ()
    notable_findings: Annotated[
        tuple[NotableFinding, ...],
        Field(max_length=16),
    ] = ()
    model_execution_summary: ModelExecutionSummary | None = None

    @field_validator("direct_upstream_comids")
    @classmethod
    def canonicalize_upstream_comids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        ensure_unique_tuple(value, field_name="card upstream COMIDs")
        return tuple(sorted(value, key=_comid_key))

    @field_validator("key_reasons")
    @classmethod
    def canonicalize_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        ensure_unique_tuple(value, field_name="card reasons")
        return tuple(sorted(value))

    @field_validator("window_summaries")
    @classmethod
    def canonicalize_windows(
        cls,
        value: tuple[ReportWindowSummary, ...],
    ) -> tuple[ReportWindowSummary, ...]:
        ensure_unique_tuple(
            tuple(item.window_id for item in value),
            field_name="report window IDs",
        )
        return tuple(sorted(value, key=lambda item: item.window_id))

    @model_validator(mode="after")
    def validate_status_projection(self) -> Self:
        result_fields = (
            self.result_ref,
            self.mode,
            self.overall_risk,
            self.availability,
            self.confidence,
            self.model_execution_summary,
        )
        has_result = self.dependency_status in {
            DependencyStatus.USABLE,
            DependencyStatus.INSUFFICIENT,
        }
        if has_result != all(value is not None for value in result_fields):
            raise ValueError("card dependency status must match result projection")
        if not has_result and (
            self.key_reasons or self.window_summaries or self.notable_findings
        ):
            raise ValueError("invalid or cancelled cards cannot project result facts")
        if self.result_ref is not None:
            for reference in self.source_refs:
                if (
                    reference.run_id != self.result_ref.run_id
                    or reference.session_id != self.session_id
                    or reference.content_type != "inference_result"
                    or reference.content_schema_version != 1
                    or reference.result_id is None
                ):
                    raise ValueError("card source refs must remain in its result session")
        return self

    @property
    def source_refs(self) -> tuple[StableSourceReference, ...]:
        return tuple(
            sorted(
                (
                    *(
                        reference
                        for item in self.window_summaries
                        for reference in item.source_refs
                    ),
                    *(item.source_ref for item in self.notable_findings),
                ),
                key=_source_reference_key,
            )
        )


class RunSummaryLimitFact(DomainModel):
    code: NonEmptyString
    text: NonBlankText
    source_ref: StableSourceReference


class RunSummaryInput(_VersionOneContent):
    run_id: UUID
    session_id: UUID
    query: NonBlankText
    target_comid: NonEmptyString
    mode: InferenceMode
    time_range: TimeRange
    analysis_timezone: NonEmptyString
    graph_coverage: GraphCoverage
    limit_facts: Annotated[tuple[RunSummaryLimitFact, ...], Field(max_length=32)] = ()
    target_result_projection: TargetResultProjection
    report_cards: tuple[WaterbodyReportCard, ...] = ()
    allowed_source_refs: tuple[StableSourceReference, ...] = ()
    input_digest: NonEmptyString

    @field_validator("analysis_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        get_iana_timezone(value)
        return value

    @field_validator("report_cards")
    @classmethod
    def canonicalize_cards(
        cls,
        value: tuple[WaterbodyReportCard, ...],
    ) -> tuple[WaterbodyReportCard, ...]:
        ensure_unique_tuple(
            tuple(item.target_comid for item in value),
            field_name="report card COMIDs",
        )
        ensure_unique_tuple(
            tuple(item.graph_position for item in value),
            field_name="report card graph positions",
        )
        return tuple(sorted(value, key=lambda item: item.graph_position))

    @field_validator("allowed_source_refs")
    @classmethod
    def canonicalize_allowed_refs(
        cls,
        value: tuple[StableSourceReference, ...],
    ) -> tuple[StableSourceReference, ...]:
        ensure_unique_tuple(value, field_name="allowed source refs")
        return tuple(sorted(value, key=_source_reference_key))

    @model_validator(mode="after")
    def validate_input(self) -> Self:
        target = self.target_result_projection
        if target.target_comid != self.target_comid:
            raise ValueError("target projection must match target_comid")
        if target.result_ref.run_id != self.run_id:
            raise ValueError("target result must remain in the run")
        if target.mode is not self.mode or target.time_range != self.time_range:
            raise ValueError("target mode/time range must match summary input")
        if target.analysis_timezone != self.analysis_timezone:
            raise ValueError("target timezone must match summary input")
        if any(card.target_comid == self.target_comid for card in self.report_cards):
            raise ValueError("target must not be duplicated as a report card")
        positions = tuple(
            sorted((target.graph_position, *(item.graph_position for item in self.report_cards)))
        )
        if positions != tuple(range(len(positions))):
            raise ValueError("target and cards must exactly cover stable graph positions")
        if target.graph_position != len(positions) - 1:
            raise ValueError("target must be the final stable graph projection")
        for card in self.report_cards:
            if card.result_ref is not None and card.result_ref.run_id != self.run_id:
                raise ValueError("card result must remain in the run")
        embedded_refs = {
            *target.source_refs,
            *(reference for card in self.report_cards for reference in card.source_refs),
            *(fact.source_ref for fact in self.limit_facts),
        }
        if embedded_refs != set(self.allowed_source_refs):
            raise ValueError("allowed_source_refs must exactly close over input facts")
        if any(reference.run_id != self.run_id for reference in self.allowed_source_refs):
            raise ValueError("summary source refs must remain in the run")
        expected = canonical_json_digest(
            self.model_dump(mode="json", exclude={"input_digest"})
        )
        if self.input_digest != expected:
            raise ValueError("input_digest must match the canonical summary input")
        return self

    def validate_result(self, result: RunSummaryResult) -> None:
        if (
            result.run_id != self.run_id
            or result.session_id != self.session_id
            or result.input_digest != self.input_digest
        ):
            raise ValueError("summary result identity must match its input")
        allowed = set(self.allowed_source_refs)
        if any(item.source_ref not in allowed for item in result.highlights):
            raise ValueError("summary highlight references a fact outside its input")


class RunSummaryMessageRole(DomainEnum):
    USER = "user"
    ASSISTANT = "assistant"


class RunSummaryModelMessage(DomainModel):
    message_id: NonEmptyString
    role: RunSummaryMessageRole
    text: NonBlankText


class RunSummaryJsonSchema(DomainModel):
    name: ModelItemName
    schema_text: JsonObjectText
    strict: StrictBool = True

    @field_validator("schema_text", mode="after")
    @classmethod
    def canonicalize_schema(cls, value: str) -> str:
        return json.dumps(
            json.loads(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @field_validator("strict")
    @classmethod
    def require_strict_schema(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("run summary model schemas must be strict")
        return value


class RunSummaryModelRequest(_VersionOneContent):
    run_id: UUID
    session_id: UUID
    operation_id: UUID
    model: ModelSelection
    input_ref: TranscriptContentRef
    input_digest: NonEmptyString
    system_prompt: NonBlankText
    messages: Annotated[tuple[RunSummaryModelMessage, ...], Field(min_length=1)]
    response_schema: RunSummaryJsonSchema
    deadline: UtcDatetime
    request_digest: NonEmptyString

    @field_validator("messages")
    @classmethod
    def validate_messages(
        cls,
        value: tuple[RunSummaryModelMessage, ...],
    ) -> tuple[RunSummaryModelMessage, ...]:
        ensure_unique_tuple(
            tuple(item.message_id for item in value),
            field_name="run summary message identities",
        )
        return value

    @model_validator(mode="after")
    def validate_request(self) -> Self:
        if self.input_ref.run_id != self.run_id:
            raise ValueError("summary input_ref must remain in the run")
        expected = canonical_json_digest(
            self.model_dump(mode="json", exclude={"request_digest"})
        )
        if self.request_digest != expected:
            raise ValueError("request_digest must match the canonical request")
        return self


class RunSummaryAgentMessage(_VersionOneContent):
    run_id: UUID
    session_id: UUID
    message_id: NonEmptyString
    operation_id: UUID
    role: Literal[RunSummaryMessageRole.ASSISTANT] = RunSummaryMessageRole.ASSISTANT
    text: str
    generation_status: GenerationStatus
    end_reason: MessageEndReason | None = None

    @model_validator(mode="after")
    def validate_generation_state(self) -> Self:
        if self.generation_status is GenerationStatus.GENERATING:
            if self.text or self.end_reason is not None:
                raise ValueError("generating message must start empty without end reason")
        elif self.end_reason is None:
            raise ValueError("final summary message requires an end reason")
        if (
            self.generation_status is GenerationStatus.COMPLETED
            and self.end_reason is not MessageEndReason.COMPLETED
        ):
            raise ValueError("completed summary message requires completed end reason")
        if (
            self.generation_status is GenerationStatus.INCOMPLETE
            and self.end_reason is MessageEndReason.COMPLETED
        ):
            raise ValueError("incomplete summary message cannot have completed end reason")
        return self


class RunSummaryFinishReason(str, Enum):
    COMPLETED = "completed"
    TRUNCATED = "truncated"
    INCOMPLETE = "incomplete"
    CONTENT_FILTERED = "content_filtered"
    REFUSED = "refused"
    CANCELLED = "cancelled"
    ERROR = "error"


class RunSummaryModelResponse(_VersionOneContent):
    run_id: UUID
    session_id: UUID
    operation_id: UUID
    model: ModelSelection
    finish_reason: RunSummaryFinishReason
    message_refs: tuple[TranscriptContentRef, ...] = ()
    structured_output: JsonObjectText | None = None
    visible_text: str = ""
    usage: ModelUsageSummary | None = None
    provider_response_id: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_response(self) -> Self:
        ensure_unique_tuple(self.message_refs, field_name="summary message refs")
        if any(reference.run_id != self.run_id for reference in self.message_refs):
            raise ValueError("summary response refs must remain in the run")
        if (
            self.finish_reason is not RunSummaryFinishReason.ERROR
            and self.provider_response_id is None
        ):
            raise ValueError("terminal summary responses require provider identity")
        if self.finish_reason is RunSummaryFinishReason.COMPLETED:
            if self.structured_output is None:
                raise ValueError("completed summary response requires structured output")
        elif self.structured_output is not None:
            raise ValueError("only completed summary responses may store structured output")
        return self


class RunSummaryHighlight(DomainModel):
    kind: NonEmptyString
    text: NonBlankText
    source_ref: StableSourceReference
    display_value: CanonicalDecimal | None = None
    display_unit: NonEmptyString | None = None
    window_id: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_display_value(self) -> Self:
        numeric_fields = (
            self.display_value is not None,
            self.display_unit is not None,
            self.window_id is not None,
        )
        if any(numeric_fields) and not all(numeric_fields):
            raise ValueError("numeric highlight display fields must appear together")
        if self.window_id is not None and self.source_ref.window_id != self.window_id:
            raise ValueError("highlight window_id must match source_ref")
        return self


class RunSummaryResult(_VersionOneContent):
    run_id: UUID
    session_id: UUID
    input_digest: NonEmptyString
    model: ModelSelection
    language: NonEmptyString
    plain_language_summary: NonBlankText
    highlights: Annotated[tuple[RunSummaryHighlight, ...], Field(max_length=32)] = ()
    usage: ModelUsageSummary | None = None
    generated_at: UtcDatetime

    @field_validator("highlights")
    @classmethod
    def validate_highlights(
        cls,
        value: tuple[RunSummaryHighlight, ...],
    ) -> tuple[RunSummaryHighlight, ...]:
        identities = tuple(
            (item.kind, item.text, item.source_ref) for item in value
        )
        ensure_unique_tuple(identities, field_name="summary highlights")
        return value


def _source_reference_key(reference: StableSourceReference) -> tuple[str, ...]:
    return (
        str(reference.run_id),
        str(reference.session_id or ""),
        str(reference.item_id),
        reference.content_type,
        str(reference.content_schema_version),
        reference.result_id or "",
        reference.cell_id or "",
        reference.window_id or "",
    )


def _comid_key(comid: str) -> tuple[int, str]:
    if not comid.isdecimal() or comid.startswith("0"):
        raise ValueError("COMID must be a positive decimal string")
    return int(comid), comid

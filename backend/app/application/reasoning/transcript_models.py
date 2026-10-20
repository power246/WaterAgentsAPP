from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from app.application.reasoning.model_models import (
    JsonObjectText,
    RefusalCompleted,
    StructuredOutputCompleted,
    ToolCallCompleted,
    WaterbodyFinishReason,
    WaterbodyJsonSchema,
    WaterbodyMessageRole,
    WaterbodyModelMessage,
    WaterbodyModelPhase,
    WaterbodyModelUsage,
    WaterbodyToolDescriptor,
)
from app.application.reasoning.prompt_policy import canonical_json_digest
from app.application.transcripts.models import (
    GenerationStatus,
    MessageEndReason,
    MessageProjection,
    TranscriptContentRef,
)
from app.domain import (
    DomainModel,
    EvidenceFragmentReference,
    MemoryMode,
    ModelSelection,
    NonEmptyString,
    ParameterKey,
    ensure_unique_tuple,
)
from app.domain.common import UtcDatetime
from app.domain.settings_admin import UnitPreference


class _VersionOneContent(DomainModel):
    schema_version: Literal[1] = 1

    @field_validator("schema_version", mode="before")
    @classmethod
    def require_integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("schema_version must be integer 1")
        return value


class WaterbodyAgentInputV1(_VersionOneContent):
    run_id: UUID
    session_id: UUID
    target_comid: NonEmptyString
    analysis_context_ref: TranscriptContentRef
    topology_ref: TranscriptContentRef
    initial_evidence_bundle_ref: TranscriptContentRef | None = None
    requested_parameters: Annotated[tuple[ParameterKey, ...], Field(min_length=1)]
    supporting_parameters: tuple[ParameterKey, ...] = ()
    unit_preferences: tuple[UnitPreference, ...] = ()
    window_ids: Annotated[tuple[NonEmptyString, ...], Field(min_length=1)]
    waterbody_model: ModelSelection
    memory_mode: MemoryMode

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        references = (
            self.analysis_context_ref,
            self.topology_ref,
            *(
                (self.initial_evidence_bundle_ref,)
                if self.initial_evidence_bundle_ref is not None
                else ()
            ),
        )
        if any(reference.run_id != self.run_id for reference in references):
            raise ValueError("waterbody input references must remain in the run")
        if len({reference.item_id for reference in references}) != len(references):
            raise ValueError("waterbody input references must be unique")
        ensure_unique_tuple(
            self.requested_parameters,
            field_name="requested_parameters",
        )
        ensure_unique_tuple(
            self.supporting_parameters,
            field_name="supporting_parameters",
        )
        if set(self.requested_parameters) & set(self.supporting_parameters):
            raise ValueError("requested and supporting parameters must be disjoint")
        ensure_unique_tuple(
            tuple(item.parameter for item in self.unit_preferences),
            field_name="waterbody input unit preferences",
        )
        if not {item.parameter for item in self.unit_preferences} <= set(
            (*self.requested_parameters, *self.supporting_parameters)
        ):
            raise ValueError("waterbody unit preferences must stay in session scope")
        ensure_unique_tuple(self.window_ids, field_name="window_ids")
        return self


class WaterbodyModelRequestV1(_VersionOneContent):
    operation_id: UUID
    phase: WaterbodyModelPhase
    model: ModelSelection
    scope_digest: NonEmptyString
    prompt_policy_digest: NonEmptyString
    evidence_bundle_ref: TranscriptContentRef
    numeric_plan_ref: TranscriptContentRef | None = None
    numeric_input_refs: tuple[EvidenceFragmentReference, ...] = ()
    batch_id: NonEmptyString | None = None
    system_prompt: NonEmptyString
    messages: Annotated[tuple[WaterbodyModelMessage, ...], Field(min_length=1)]
    tool_descriptors: tuple[WaterbodyToolDescriptor, ...] = ()
    response_schema: WaterbodyJsonSchema
    deadline: UtcDatetime
    request_digest: NonEmptyString

    @model_validator(mode="after")
    def validate_request_digest(self) -> Self:
        values = self.model_dump(mode="json", exclude={"request_digest"})
        if self.numeric_plan_ref is None:
            values.pop("numeric_plan_ref")
        if not self.numeric_input_refs:
            values.pop("numeric_input_refs")
        expected = canonical_json_digest(values)
        if self.request_digest != expected:
            raise ValueError("request_digest must match the canonical request")
        return self


class WaterbodyAgentMessageV1(_VersionOneContent):
    message_id: NonEmptyString
    operation_id: UUID
    phase: WaterbodyModelPhase
    role: Literal[WaterbodyMessageRole.ASSISTANT] = WaterbodyMessageRole.ASSISTANT
    text: str
    generation_status: GenerationStatus
    end_reason: MessageEndReason | None = None

    @model_validator(mode="after")
    def validate_generation_state(self) -> Self:
        if self.generation_status is GenerationStatus.GENERATING:
            if self.text or self.end_reason is not None:
                raise ValueError("generating message must start empty without an end reason")
        elif self.end_reason is None:
            raise ValueError("final message requires an end reason")
        if (
            self.generation_status is GenerationStatus.COMPLETED
            and self.end_reason is not MessageEndReason.COMPLETED
        ):
            raise ValueError("completed message requires completed end reason")
        if (
            self.generation_status is GenerationStatus.INCOMPLETE
            and self.end_reason is MessageEndReason.COMPLETED
        ):
            raise ValueError("incomplete message cannot have completed end reason")
        return self


def waterbody_agent_message_projection(body: object) -> MessageProjection:
    if not isinstance(body, WaterbodyAgentMessageV1):
        raise TypeError("waterbody message adapter received another content type")
    return MessageProjection(
        text=body.text,
        generation_status=body.generation_status,
        end_reason=body.end_reason,
    )


def finalize_waterbody_agent_message(
    text: str,
    generation_status: GenerationStatus,
    end_reason: MessageEndReason | None,
    base_body: object | None = None,
) -> WaterbodyAgentMessageV1:
    if not isinstance(base_body, WaterbodyAgentMessageV1):
        raise TypeError("waterbody message finalization requires its generating body")
    return base_body.model_copy(
        update={
            "text": text,
            "generation_status": generation_status,
            "end_reason": end_reason,
        }
    )


class StoredModelFinishReason(str, Enum):
    COMPLETED = "completed"
    TRUNCATED = "truncated"
    INCOMPLETE = "incomplete"
    CONTENT_FILTERED = "content_filtered"
    REFUSED = "refused"
    CANCELLED = "cancelled"
    ERROR = "error"


class WaterbodyModelResponseV1(_VersionOneContent):
    operation_id: UUID
    phase: WaterbodyModelPhase
    model: ModelSelection
    finish_reason: StoredModelFinishReason
    message_refs: tuple[TranscriptContentRef, ...]
    tool_calls: tuple[ToolCallCompleted, ...] = ()
    structured_outputs: tuple[StructuredOutputCompleted, ...] = ()
    refusal: RefusalCompleted | None = None
    usage: WaterbodyModelUsage | None = None
    provider_response_id: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_response(self) -> Self:
        ensure_unique_tuple(self.message_refs, field_name="message_refs")
        call_ids = tuple(item.provider_call_id for item in self.tool_calls)
        ensure_unique_tuple(call_ids, field_name="tool call identities")
        output_ids = tuple(item.output_id for item in self.structured_outputs)
        ensure_unique_tuple(output_ids, field_name="structured output identities")
        events = (*self.tool_calls, *self.structured_outputs)
        if any(
            item.operation_id != self.operation_id or item.model != self.model
            for item in events
        ):
            raise ValueError("stored response events must match operation and model")
        if self.refusal is not None and (
            self.refusal.operation_id != self.operation_id
            or self.refusal.model != self.model
        ):
            raise ValueError("stored refusal must match operation and model")
        if (
            self.finish_reason not in {
                StoredModelFinishReason.ERROR,
                StoredModelFinishReason.CANCELLED,
            }
            and self.provider_response_id is None
        ):
            raise ValueError("terminal stored responses require provider identity")
        if self.finish_reason is StoredModelFinishReason.REFUSED:
            if self.refusal is None:
                raise ValueError("refused response requires a visible refusal")
        return self


class ReasoningDecisionType(str, Enum):
    TOOL_CALLS_REQUESTED = "tool_calls_requested"
    EVIDENCE_RULES_PROPOSED = "evidence_rules_proposed"
    EVIDENCE_READY = "evidence_ready"
    PLANNING_LIMIT_REACHED = "planning_limit_reached"
    EVIDENCE_SEALED = "evidence_sealed"
    REASONING_BATCH_VALIDATED = "reasoning_batch_validated"
    OVERALL_SYNTHESIS_VALIDATED = "overall_synthesis_validated"


_PLANNING_DECISIONS = frozenset(
    {
        ReasoningDecisionType.TOOL_CALLS_REQUESTED,
        ReasoningDecisionType.EVIDENCE_RULES_PROPOSED,
        ReasoningDecisionType.EVIDENCE_READY,
        ReasoningDecisionType.PLANNING_LIMIT_REACHED,
        ReasoningDecisionType.EVIDENCE_SEALED,
    }
)


class ReasoningDecisionV1(_VersionOneContent):
    decision_type: ReasoningDecisionType
    operation_id: UUID
    phase: WaterbodyModelPhase
    model_response_ref: TranscriptContentRef
    bundle_ref: TranscriptContentRef | None = None
    batch_id: NonEmptyString | None = None
    scope_digest: NonEmptyString | None = None
    rationale: NonEmptyString | None = None
    tool_call_ids: tuple[NonEmptyString, ...] = ()
    accepted_output: JsonObjectText | None = None
    validated_digest: NonEmptyString | None = None
    repair_of_operation_id: UUID | None = None
    validation_issue_codes: tuple[NonEmptyString, ...] = ()
    unit_decisions: tuple[UnitPreference, ...] = ()

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        ensure_unique_tuple(
            tuple(item.parameter for item in self.unit_decisions),
            field_name="reasoning unit decisions",
        )
        ensure_unique_tuple(self.tool_call_ids, field_name="tool_call_ids")
        ensure_unique_tuple(
            self.validation_issue_codes,
            field_name="validation_issue_codes",
        )
        validated = self.decision_type in {
            ReasoningDecisionType.REASONING_BATCH_VALIDATED,
            ReasoningDecisionType.OVERALL_SYNTHESIS_VALIDATED,
        }
        if validated:
            if (
                self.decision_type is ReasoningDecisionType.OVERALL_SYNTHESIS_VALIDATED
                and self.unit_decisions
            ):
                raise ValueError("only reasoning batches select unit preferences")
            if (
                self.scope_digest is None
                or self.accepted_output is None
                or self.validated_digest is None
                or self.rationale is not None
                or self.tool_call_ids
            ):
                raise ValueError("validated reasoning decision has an invalid shape")
            expected_phase = (
                WaterbodyModelPhase.REASONING_BATCH
                if self.decision_type
                is ReasoningDecisionType.REASONING_BATCH_VALIDATED
                else WaterbodyModelPhase.OVERALL_SYNTHESIS
            )
            if self.phase not in {expected_phase, WaterbodyModelPhase.SCHEMA_REPAIR}:
                raise ValueError("validated reasoning decision has the wrong phase")
        elif self.decision_type in _PLANNING_DECISIONS:
            if self.unit_decisions:
                raise ValueError("planning decisions cannot select unit preferences")
            if (
                self.phase is not WaterbodyModelPhase.EVIDENCE_PLANNING
                or self.accepted_output is not None
                or self.validated_digest is not None
                or self.repair_of_operation_id is not None
                or self.validation_issue_codes
            ):
                raise ValueError("planning decision has an invalid shape")
        else:
            raise ValueError("unsupported reasoning decision")
        return self


def stored_finish_reason(value: WaterbodyFinishReason) -> StoredModelFinishReason:
    return StoredModelFinishReason(value.value)

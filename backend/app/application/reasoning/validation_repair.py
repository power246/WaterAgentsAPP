from __future__ import annotations

import json
from collections.abc import Callable
from typing import Protocol, Self, TypeVar
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import model_validator

from app.application.reasoning.batch_planner import (
    ReasoningBatch,
    ReasoningBatchPlan,
    SynthesisInputPlan,
)
from app.application.reasoning.draft_models import (
    DraftValidationIssue,
    ValidatedOverallSynthesis,
    ValidatedReasoningBatch,
    ValidatedReasoningSet,
)
from app.application.reasoning.draft_validator import DraftValidator
from app.application.reasoning.errors import (
    DraftValidationError,
    ValidationRepairFailedError,
)
from app.application.reasoning.model_models import (
    StructuredOutputCompleted,
    ToolCallCompleted,
    VisibleTextDelta,
    WaterbodyModelCompletedResponse,
    WaterbodyModelPhase,
    WaterbodyModelRequest,
)
from app.application.reasoning.model_protocol import (
    CancellationProbe,
    consume_waterbody_model_stream,
)
from app.application.reasoning.prompt_builder import (
    PromptBuilder,
    PromptValidationIssue,
    SchemaRepairPromptInput,
    ValidatedCellPromptSummary,
)
from app.application.reasoning.prompt_policy import canonical_json_text
from app.application.reasoning.result_models import ResultSkeleton
from app.application.reasoning.working_context import (
    WorkingContextProjection,
    require_safe_prompt_payload,
)
from app.domain import DomainModel, NonEmptyString
from app.ports.errors import WaterbodyModelPortError
from app.ports.waterbody_model import WaterbodyModelPort


ValidatedDraftT = TypeVar(
    "ValidatedDraftT",
    ValidatedReasoningBatch,
    ValidatedOverallSynthesis,
)


class ValidationDecisionStore(Protocol):
    async def write_validated_exchange(
        self,
        audit: "ValidationExchangeAudit",
    ) -> object: ...


class ValidationExchangeAudit(DomainModel):
    original_phase: WaterbodyModelPhase
    batch_id: NonEmptyString | None
    initial_request: WaterbodyModelRequest
    initial_response: WaterbodyModelCompletedResponse
    initial_output_id: NonEmptyString | None
    initial_issues: tuple[DraftValidationIssue, ...]
    repair_request: WaterbodyModelRequest | None = None
    repair_response: WaterbodyModelCompletedResponse | None = None
    repair_output_id: NonEmptyString | None = None
    repair_issues: tuple[DraftValidationIssue, ...] = ()
    repair_error_code: NonEmptyString | None = None
    accepted_operation_id: UUID | None = None
    validated_digest: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_correlation(self) -> Self:
        initial = self.initial_request
        if (
            initial.phase is not self.original_phase
            or initial.batch_id != self.batch_id
            or self.initial_response.operation_id != initial.operation_id
            or self.initial_response.phase is not initial.phase
            or self.initial_response.model != initial.model
        ):
            raise ValueError("initial validation exchange identity mismatch")
        if self.repair_request is None:
            if any(
                value
                for value in (
                    self.initial_issues,
                    self.repair_response,
                    self.repair_output_id,
                    self.repair_issues,
                    self.repair_error_code,
                )
            ):
                raise ValueError("unrepaired exchange contains repair state")
        else:
            repair = self.repair_request
            if not self.initial_issues:
                raise ValueError("repair requires initial validation issues")
            if (
                repair.phase is not WaterbodyModelPhase.SCHEMA_REPAIR
                or repair.operation_id == initial.operation_id
                or repair.model != initial.model
                or repair.scope_digest != initial.scope_digest
                or repair.response_schema != initial.response_schema
                or repair.evidence_bundle_ref != initial.evidence_bundle_ref
                or repair.batch_id != initial.batch_id
                or repair.tool_descriptors
            ):
                raise ValueError("repair request drifted from the original exchange")
            if self.repair_response is not None and (
                self.repair_response.operation_id != repair.operation_id
                or self.repair_response.phase is not repair.phase
                or self.repair_response.model != repair.model
            ):
                raise ValueError("repair response identity mismatch")
        if (self.accepted_operation_id is None) != (self.validated_digest is None):
            raise ValueError("validated exchange correlation is incomplete")
        accepted = self.accepted_operation_id
        allowed = {initial.operation_id}
        if self.repair_request is not None:
            allowed.add(self.repair_request.operation_id)
        if accepted is not None and accepted not in allowed:
            raise ValueError("validated exchange accepted an unknown operation")
        if accepted == initial.operation_id and self.initial_issues:
            raise ValueError("invalid initial response cannot be accepted")
        if (
            self.repair_request is not None
            and accepted == self.repair_request.operation_id
            and (
                self.repair_response is None
                or self.repair_output_id is None
                or self.repair_issues
                or self.repair_error_code is not None
            )
        ):
            raise ValueError("accepted repair correlation is incomplete")
        if self.repair_error_code is not None and self.repair_response is not None:
            raise ValueError("failed repair cannot contain a completed response")
        if self.repair_issues and self.repair_response is None:
            raise ValueError("repair validation issues require a completed response")
        return self


class ValidatedBatchExchange(DomainModel):
    validated: ValidatedReasoningBatch
    audit: ValidationExchangeAudit

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        if self.audit.validated_digest != self.validated.validation_digest:
            raise ValueError("batch exchange validation digest mismatch")
        return self


class ValidatedSynthesisExchange(DomainModel):
    validated: ValidatedOverallSynthesis
    audit: ValidationExchangeAudit

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        if self.audit.validated_digest != self.validated.validation_digest:
            raise ValueError("synthesis exchange validation digest mismatch")
        return self


class ValidationExchange:
    def __init__(
        self,
        *,
        model_port: WaterbodyModelPort,
        prompt_builder: PromptBuilder,
        validator: DraftValidator,
        decision_store: ValidationDecisionStore | None = None,
    ) -> None:
        self._model_port = model_port
        self._prompt_builder = prompt_builder
        self._validator = validator
        self._decision_store = decision_store

    async def run_batch(
        self,
        batch: ReasoningBatch,
        *,
        cancellation: CancellationProbe,
    ) -> ValidatedBatchExchange:
        request = self._prompt_builder.build_reasoning_batch(batch.prompt_input())
        validated, audit = await self._run(
            request,
            context=batch.context,
            cancellation=cancellation,
            validate=lambda value: self._validator.validate_batch(batch, value),
        )
        return ValidatedBatchExchange(validated=validated, audit=audit)

    async def run_synthesis(
        self,
        skeleton: ResultSkeleton,
        batch_plan: ReasoningBatchPlan,
        synthesis_plan: SynthesisInputPlan,
        reasoning: ValidatedReasoningSet,
        *,
        cancellation: CancellationProbe,
    ) -> ValidatedSynthesisExchange:
        scopes_by_id = {
            scope.cell_id: scope
            for scope in synthesis_plan.validated_cell_scopes
        }
        summaries = tuple(
            ValidatedCellPromptSummary(
                cell_id=cell.cell_id,
                window_id=scopes_by_id[cell.cell_id].window_id,
                outcome_status=cell.outcome_status,
                risk_level=cell.risk_level,
                risk_rationale=cell.risk_rationale,
                confidence=cell.confidence,
                confidence_rationale=cell.confidence_rationale,
                evidence_refs=cell.evidence_refs,
            )
            for batch in reasoning.batches
            for cell in batch.cells
        )
        request = self._prompt_builder.build_overall_synthesis(
            synthesis_plan.prompt_input(summaries)
        )
        validated, audit = await self._run(
            request,
            context=synthesis_plan.context,
            cancellation=cancellation,
            validate=lambda value: self._validator.validate_synthesis(
                skeleton,
                batch_plan,
                synthesis_plan,
                reasoning,
                value,
            ),
        )
        return ValidatedSynthesisExchange(validated=validated, audit=audit)

    async def _run(
        self,
        request: WaterbodyModelRequest,
        *,
        context: WorkingContextProjection,
        cancellation: CancellationProbe,
        validate: Callable[[str], ValidatedDraftT],
    ) -> tuple[ValidatedDraftT, ValidationExchangeAudit]:
        initial_response = await consume_waterbody_model_stream(
            self._model_port,
            request,
            cancellation=cancellation,
        )
        initial_output = _structured_output(initial_response)
        try:
            if initial_output is None:
                raise DraftValidationError(
                    (
                        DraftValidationIssue(
                            code="missing_structured_output",
                            path="$",
                        ),
                    )
                )
            validated = validate(initial_output.value)
        except DraftValidationError as initial_error:
            initial_issues = _canonical_issues(initial_error)
        else:
            audit = ValidationExchangeAudit(
                original_phase=request.phase,
                batch_id=request.batch_id,
                initial_request=request,
                initial_response=initial_response,
                initial_output_id=initial_output.output_id,
                initial_issues=(),
                accepted_operation_id=request.operation_id,
                validated_digest=validated.validation_digest,
            )
            if self._decision_store is not None:
                await self._decision_store.write_validated_exchange(audit)
            return validated, audit

        repair_request = self._prompt_builder.build_schema_repair(
            SchemaRepairPromptInput(
                operation_id=uuid5(
                    NAMESPACE_URL,
                    f"schema-repair:{request.operation_id}",
                ),
                repair_of_operation_id=request.operation_id,
                model=request.model,
                deadline=request.deadline,
                context=context,
                response_schema=request.response_schema,
                original_phase=request.phase,
                batch_id=request.batch_id,
                original_visible_response=_safe_repair_response(initial_response),
                issues=tuple(
                    PromptValidationIssue(code=issue.code, path=issue.path)
                    for issue in initial_issues[:64]
                ),
            )
        )
        try:
            repair_response = await consume_waterbody_model_stream(
                self._model_port,
                repair_request,
                cancellation=cancellation,
            )
        except WaterbodyModelPortError as error:
            audit = ValidationExchangeAudit(
                original_phase=request.phase,
                batch_id=request.batch_id,
                initial_request=request,
                initial_response=initial_response,
                initial_output_id=(
                    initial_output.output_id if initial_output is not None else None
                ),
                initial_issues=initial_issues,
                repair_request=repair_request,
                repair_error_code=error.code,
            )
            raise ValidationRepairFailedError(
                audit,
                cause_code=error.code,
            ) from error

        repair_output = _structured_output(repair_response)
        try:
            if repair_output is None:
                raise DraftValidationError(
                    (
                        DraftValidationIssue(
                            code="missing_structured_output",
                            path="$",
                        ),
                    )
                )
            validated = validate(repair_output.value)
        except DraftValidationError as repair_error:
            repair_issues = _canonical_issues(repair_error)
            audit = ValidationExchangeAudit(
                original_phase=request.phase,
                batch_id=request.batch_id,
                initial_request=request,
                initial_response=initial_response,
                initial_output_id=(
                    initial_output.output_id if initial_output is not None else None
                ),
                initial_issues=initial_issues,
                repair_request=repair_request,
                repair_response=repair_response,
                repair_output_id=(
                    repair_output.output_id if repair_output is not None else None
                ),
                repair_issues=repair_issues,
            )
            raise ValidationRepairFailedError(
                audit,
                cause_code="reasoning_draft_invalid",
            ) from None

        audit = ValidationExchangeAudit(
            original_phase=request.phase,
            batch_id=request.batch_id,
            initial_request=request,
            initial_response=initial_response,
            initial_output_id=(
                initial_output.output_id if initial_output is not None else None
            ),
            initial_issues=initial_issues,
            repair_request=repair_request,
            repair_response=repair_response,
            repair_output_id=repair_output.output_id,
            accepted_operation_id=repair_request.operation_id,
            validated_digest=validated.validation_digest,
        )
        if self._decision_store is not None:
            await self._decision_store.write_validated_exchange(audit)
        return validated, audit


def _structured_output(
    response: WaterbodyModelCompletedResponse,
) -> StructuredOutputCompleted | None:
    if any(isinstance(event, ToolCallCompleted) for event in response.events):
        return None
    return next(
        (
            event
            for event in response.events
            if isinstance(event, StructuredOutputCompleted)
        ),
        None,
    )


def _canonical_issues(error: DraftValidationError) -> tuple[DraftValidationIssue, ...]:
    if not all(isinstance(issue, DraftValidationIssue) for issue in error.issues):
        raise TypeError("draft validation error contains an untyped issue")
    return tuple(
        DraftValidationIssue(code=code, path=path)
        for code, path in sorted(
            {
                (issue.code, issue.path)
                for issue in error.issues
            }
        )
    )


def _safe_repair_response(
    response: WaterbodyModelCompletedResponse,
) -> str:
    output = next(
        (
            event
            for event in response.events
            if isinstance(event, StructuredOutputCompleted)
        ),
        None,
    )
    if output is None:
        visible = "".join(
            event.text
            for event in response.events
            if isinstance(event, VisibleTextDelta)
        )
        value: object = {
            "response_missing": True,
            "visible_text": visible or "[no visible response]",
        }
    else:
        value = json.loads(output.value)

    def redact(item: object) -> object:
        if isinstance(item, dict):
            result = {}
            for index, (key, child) in enumerate(item.items()):
                try:
                    require_safe_prompt_payload(key)
                except ValueError:
                    key = f"redacted_field_{index}"
                result[key] = redact(child)
            return result
        if isinstance(item, list):
            return [redact(child) for child in item]
        if isinstance(item, str):
            try:
                require_safe_prompt_payload(item)
            except ValueError:
                return "[redacted]"
        return item

    safe = canonical_json_text(redact(value))
    if len(safe) > 131_072:
        return '{"response_omitted":"bounded"}'
    require_safe_prompt_payload(safe)
    return safe

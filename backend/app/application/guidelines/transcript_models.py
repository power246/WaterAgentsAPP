from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from app.application.transcripts.models import TranscriptContentRef
from app.domain import DomainModel, ParameterKey, ensure_unique_tuple
from app.domain.guidelines import (
    GuidelineCitationV1, GuidelineResultUsageBinding, GuidelineRetrievalResult,
    GuidelineRunPin, GuidelineUsageTarget, GuidelineVersionV1,
)
from app.domain.guidelines.canonical import Digest, NormalizedText
from app.domain.guidelines.run_pin import SafeReason

GuidelinePhase = Literal["evidence_planning", "numeric_estimation", "reasoning_batch", "overall_synthesis", "schema_repair"]


class GuidelineRunContextV1(DomainModel):
    schema_version: Literal[1] = 1
    run_id: UUID
    policy_version: Literal[1] = 1
    status: Literal["ready", "unavailable"]
    catalog_revision: Annotated[int, Field(strict=True, ge=0)] | None = None
    generation_id: UUID | None = None
    profile_id: str | None = None
    profile_digest: Digest | None = None
    safe_error_code: SafeReason | None = None

    @classmethod
    def from_pin(cls, pin: GuidelineRunPin):
        catalog = pin.catalog
        profile = catalog.profile if catalog is not None else None
        return cls(run_id=pin.run_id, policy_version=pin.policy_version, status=pin.status,
            catalog_revision=catalog.revision if catalog is not None else None,
            generation_id=catalog.generation_id if catalog is not None else None,
            profile_id=profile.profile_id if profile is not None else None,
            profile_digest=profile.profile_digest if profile is not None else None, safe_error_code=pin.safe_error_code)

    @model_validator(mode="after")
    def readiness(self) -> Self:
        if self.status == "ready" and any(value is None for value in (
            self.catalog_revision, self.generation_id, self.profile_id, self.profile_digest)):
            raise ValueError("ready guideline run context requires a pinned catalog")
        if (self.status == "unavailable") != (self.safe_error_code is not None):
            raise ValueError("guideline run context availability mismatch")
        if self.profile_id is not None and self.profile_id != "guideline:" + (self.profile_digest or ""):
            raise ValueError("guideline run profile identity mismatch")
        return self


class GuidelineSessionRecord(DomainModel):
    schema_version: Literal[1] = 1
    run_id: UUID
    session_id: UUID


class GuidelineRetrievalV1(GuidelineSessionRecord):
    run_context_ref: TranscriptContentRef
    parameter_keys: tuple[ParameterKey, ...]
    selection: GuidelineRetrievalResult

    @model_validator(mode="after")
    def run_scope(self) -> Self:
        if self.run_context_ref.run_id != self.run_id:
            raise ValueError("guideline retrieval belongs to another run")
        ensure_unique_tuple(self.parameter_keys, field_name="guideline retrieval parameters")
        return self


class GuidelineDeliveredVersion(DomainModel):
    alias: Annotated[NormalizedText, Field(min_length=1, max_length=64)]
    version: GuidelineVersionV1


class GuidelineDeliveryV1(GuidelineSessionRecord):
    operation_id: UUID
    phase: GuidelinePhase
    run_context_ref: TranscriptContentRef
    retrieval_ref: TranscriptContentRef
    request_ref: TranscriptContentRef
    selection_status: Literal["retrieved", "empty_library", "no_match", "unavailable", "budget_limited", "cancelled"]
    submitted: Literal[True] = True
    advisory_digest: Digest
    entries: Annotated[tuple[GuidelineDeliveredVersion, ...], Field(max_length=3)]

    @model_validator(mode="after")
    def exact_delivery(self) -> Self:
        if any(ref.run_id != self.run_id for ref in (self.run_context_ref, self.retrieval_ref, self.request_ref)):
            raise ValueError("guideline delivery belongs to another run")
        ensure_unique_tuple(tuple(entry.alias for entry in self.entries), field_name="delivery aliases")
        ensure_unique_tuple(tuple(entry.version.version_id for entry in self.entries), field_name="delivery versions")
        if bool(self.entries) != (self.selection_status == "retrieved"):
            raise ValueError("guideline delivery selection status mismatch")
        return self


class GuidelineUsageV1(GuidelineSessionRecord):
    operation_id: UUID
    phase: GuidelinePhase
    model_response_ref: TranscriptContentRef
    target: GuidelineUsageTarget
    citations: Annotated[tuple[GuidelineCitationV1, ...], Field(min_length=1, max_length=3)]

    @model_validator(mode="after")
    def citation_scope(self) -> Self:
        if self.model_response_ref.run_id != self.run_id or any(
            citation.delivery_ref.run_id != self.run_id or citation.delivery_ref.session_id != self.session_id
            or citation.operation_id != self.operation_id or citation.phase != self.phase for citation in self.citations
        ):
            raise ValueError("guideline usage citation scope mismatch")
        ensure_unique_tuple(tuple(citation.version_id for citation in self.citations), field_name="usage versions")
        return self


class ResultGuidelineUsageV1(GuidelineSessionRecord):
    result_id: Annotated[str, Field(min_length=1)]
    bindings: Annotated[tuple[GuidelineResultUsageBinding, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def result_scope(self) -> Self:
        if any(binding.result_id != self.result_id or binding.target.kind == "numeric_task" for binding in self.bindings):
            raise ValueError("result guideline usage target mismatch")
        ensure_unique_tuple(tuple(binding.target for binding in self.bindings), field_name="result usage targets")
        return self


GUIDELINE_RECORDS = {
    "guideline_run_context": GuidelineRunContextV1,
    "guideline_retrieval": GuidelineRetrievalV1,
    "guideline_delivery": GuidelineDeliveryV1,
    "guideline_usage": GuidelineUsageV1,
    "result_guideline_usage": ResultGuidelineUsageV1,
}


def guideline_references(body):
    if isinstance(body, GuidelineRunContextV1):
        refs = ()
    elif isinstance(body, GuidelineRetrievalV1):
        refs = (body.run_context_ref.item_id,)
    elif isinstance(body, GuidelineDeliveryV1):
        refs = tuple(ref.item_id for ref in (body.run_context_ref, body.retrieval_ref, body.request_ref))
    elif isinstance(body, GuidelineUsageV1):
        refs = (body.model_response_ref.item_id, *(citation.delivery_ref.item_id for citation in body.citations))
    elif isinstance(body, ResultGuidelineUsageV1):
        refs = tuple(item_id for binding in body.bindings for item_id in binding.usage_item_ids)
    else:
        raise TypeError("unsupported guideline transcript record")
    return tuple(dict.fromkeys(refs))


def validate_guideline_identity(body, item):
    if isinstance(body, GuidelineRunContextV1):
        if body.run_id != item.run_id or item.session_id is not None:
            raise ValueError("guideline run context ownership mismatch")
    elif isinstance(body, GuidelineSessionRecord):
        if body.run_id != item.run_id or body.session_id != item.session_id:
            raise ValueError("guideline transcript ownership mismatch")
        if hasattr(body, "operation_id") and body.operation_id != item.operation_id:
            raise ValueError("guideline transcript operation mismatch")

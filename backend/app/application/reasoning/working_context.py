from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    Field,
    StrictBool,
    field_validator,
    model_serializer,
    model_validator,
)

from app.application.analysis_runs.models import SessionResultReference
from app.application.analysis_runs.topology_result_models import (
    DependencyStatus,
    WaterbodyDependencyManifest,
)
from app.application.reasoning.model_models import (
    NonBlankText,
    WaterbodyModelMessage,
)
from app.application.reasoning.prompt_policy import (
    canonical_json_digest,
    canonical_json_text,
)
from app.domain import (
    DomainEnum,
    DomainModel,
    InferenceMode,
    ItemReference,
    NonEmptyString,
    ensure_unique_tuple,
)


_MAX_RECENT_MESSAGES = 16
_MAX_FACTS = 128
_MAX_EVIDENCE_SUMMARIES = 64
_MAX_SUMMARY_CHARACTERS = 2048
_MAX_CONTEXT_BYTES = 131_072
_UNSAFE_TEXT_MARKERS = (
    "authorization: bearer",
    "access_token",
    "api key",
    "api_key",
    "api_key=",
    "api-key=",
    "credential",
    "password",
    "refresh_token",
    "https://",
    "http://",
    "back" + "end_bk",
    "front" + "end_bk",
    "raw_provider_envelope",
    "encrypted_reasoning",
)


class WorkingContextFactKind(DomainEnum):
    GATE = "gate"
    MISSING = "missing"
    BLOCKER = "blocker"
    COVERAGE = "coverage"
    FIXED_CELL = "fixed_cell"
    VALIDATED_CELL = "validated_cell"
    BUDGET = "budget"


class WorkingContextFact(DomainModel):
    fact_id: NonEmptyString
    kind: WorkingContextFactKind
    summary: NonBlankText
    refs: tuple[NonEmptyString, ...] = ()

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return require_safe_prompt_text(value)

    @field_validator("refs")
    @classmethod
    def canonicalize_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("working-context refs must be unique")
        return tuple(sorted(value))


class BoundedEvidenceSummary(DomainModel):
    evidence_ref: NonEmptyString
    summary: NonBlankText
    source_record_item_refs: tuple[NonEmptyString, ...] = ()
    series_refs: tuple[NonEmptyString, ...] = ()
    truncated: StrictBool

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        return require_safe_prompt_text(value)

    @field_validator("source_record_item_refs", "series_refs")
    @classmethod
    def canonicalize_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("evidence summary refs must be unique")
        return tuple(sorted(value))


class DependencyPromptWindow(DomainModel):
    window_id: NonEmptyString
    mode: InferenceMode


class DependencyPromptEntry(DomainModel):
    upstream_comid: NonEmptyString
    dependency_status: DependencyStatus
    result_ref: SessionResultReference | None = None
    source_role: Literal["cross_waterbody_inferred_evidence"] = (
        "cross_waterbody_inferred_evidence"
    )

    @model_validator(mode="after")
    def validate_status_projection(self) -> Self:
        if (self.dependency_status is DependencyStatus.USABLE) != (
            self.result_ref is not None
        ):
            raise ValueError("only usable prompt dependencies expose result_ref")
        return self

    @model_serializer(mode="wrap")
    def serialize_without_empty_result_ref(self, handler):
        value = handler(self)
        if self.result_ref is None:
            value.pop("result_ref", None)
        return value


class WaterbodyDependencyPromptBlock(DomainModel):
    manifest_digest: NonEmptyString
    current_waterbody_comid: NonEmptyString
    mode: InferenceMode
    windows: tuple[DependencyPromptWindow, ...]
    dependencies: tuple[DependencyPromptEntry, ...]
    local_same_parameter_role: Literal["direct_observation"] = "direct_observation"
    local_supporting_role: Literal["supporting_indirect_evidence"] = (
        "supporting_indirect_evidence"
    )
    upstream_role: Literal["cross_waterbody_inferred_evidence"] = (
        "cross_waterbody_inferred_evidence"
    )
    quantified_value_policy: Literal["registered_deterministic_tool_only"] = (
        "registered_deterministic_tool_only"
    )

    @field_validator("windows")
    @classmethod
    def validate_windows(
        cls,
        value: tuple[DependencyPromptWindow, ...],
    ) -> tuple[DependencyPromptWindow, ...]:
        if not value:
            raise ValueError("dependency prompt requires at least one window")
        ensure_unique_tuple(
            tuple(item.window_id for item in value),
            field_name="dependency prompt windows",
        )
        return value

    @field_validator("dependencies")
    @classmethod
    def canonicalize_dependencies(
        cls,
        value: tuple[DependencyPromptEntry, ...],
    ) -> tuple[DependencyPromptEntry, ...]:
        ensure_unique_tuple(
            tuple(item.upstream_comid for item in value),
            field_name="dependency prompt COMIDs",
        )
        return tuple(sorted(value, key=lambda item: item.upstream_comid))


class WorkingContextProjection(DomainModel):
    schema_version: Annotated[int, Field(strict=True, ge=1, le=1)] = 1
    run_id: UUID
    session_id: UUID
    target_comid: NonEmptyString
    scope_digest: NonEmptyString
    bundle_ref: ItemReference
    bundle_digest: NonEmptyString
    rule_plan_digest: NonEmptyString
    capability_registry_digest: NonEmptyString
    reference_allowlist: tuple[NonEmptyString, ...]
    recent_messages: tuple[WaterbodyModelMessage, ...]
    facts: tuple[WorkingContextFact, ...]
    evidence_summaries: tuple[BoundedEvidenceSummary, ...]
    dependency_context: WaterbodyDependencyPromptBlock | None = None
    working_context_digest: NonEmptyString

    @field_validator("target_comid")
    @classmethod
    def validate_target_comid(cls, value: str) -> str:
        return require_safe_prompt_text(value)

    @field_validator("reference_allowlist")
    @classmethod
    def canonicalize_allowlist(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("working-context reference allowlist must be unique")
        return tuple(sorted(value))

    @field_validator("recent_messages")
    @classmethod
    def validate_messages(
        cls,
        value: tuple[WaterbodyModelMessage, ...],
    ) -> tuple[WaterbodyModelMessage, ...]:
        if len(value) > _MAX_RECENT_MESSAGES:
            raise ValueError("working context allows at most 16 recent messages")
        for message in value:
            require_safe_prompt_text(message.text)
        return value

    @field_validator("facts")
    @classmethod
    def canonicalize_facts(
        cls,
        value: tuple[WorkingContextFact, ...],
    ) -> tuple[WorkingContextFact, ...]:
        if len(value) > _MAX_FACTS:
            raise ValueError("working context allows at most 128 facts")
        if len({item.fact_id for item in value}) != len(value):
            raise ValueError("working-context fact IDs must be unique")
        return tuple(sorted(value, key=lambda item: item.fact_id))

    @field_validator("evidence_summaries")
    @classmethod
    def canonicalize_evidence_summaries(
        cls,
        value: tuple[BoundedEvidenceSummary, ...],
    ) -> tuple[BoundedEvidenceSummary, ...]:
        if len(value) > _MAX_EVIDENCE_SUMMARIES:
            raise ValueError("working context allows at most 64 evidence summaries")
        if len({item.evidence_ref for item in value}) != len(value):
            raise ValueError("evidence-summary refs must be unique")
        return tuple(sorted(value, key=lambda item: item.evidence_ref))

    @model_validator(mode="after")
    def validate_projection(self) -> Self:
        allowlist = set(self.reference_allowlist)
        referenced = {
            ref
            for fact in self.facts
            for ref in fact.refs
        } | {
            ref
            for summary in self.evidence_summaries
            for ref in (summary.evidence_ref, *summary.source_record_item_refs)
        }
        if not referenced <= allowlist:
            raise ValueError("working context refs must be in the reference allowlist")
        payload = self.model_dump(mode="json", exclude={"working_context_digest"})
        if len(canonical_json_text(payload).encode("utf-8")) > _MAX_CONTEXT_BYTES:
            raise ValueError("working context exceeds the bounded byte limit")
        if self.working_context_digest != canonical_json_digest(payload):
            raise ValueError("working context digest does not match canonical content")
        return self


def build_working_context_projection(
    *,
    run_id: UUID,
    session_id: UUID,
    target_comid: str,
    scope_digest: str,
    bundle_ref: ItemReference,
    bundle_digest: str,
    rule_plan_digest: str,
    capability_registry_digest: str,
    reference_allowlist: tuple[str, ...],
    recent_messages: tuple[WaterbodyModelMessage, ...],
    facts: tuple[WorkingContextFact, ...],
    evidence_summaries: tuple[BoundedEvidenceSummary, ...],
    dependency_context: WaterbodyDependencyPromptBlock | None = None,
) -> WorkingContextProjection:
    values = {
        "schema_version": 1,
        "run_id": run_id,
        "session_id": session_id,
        "target_comid": target_comid,
        "scope_digest": scope_digest,
        "bundle_ref": bundle_ref,
        "bundle_digest": bundle_digest,
        "rule_plan_digest": rule_plan_digest,
        "capability_registry_digest": capability_registry_digest,
        "reference_allowlist": tuple(sorted(reference_allowlist)),
        "recent_messages": recent_messages,
        "facts": tuple(sorted(facts, key=lambda item: item.fact_id)),
        "evidence_summaries": tuple(
            sorted(evidence_summaries, key=lambda item: item.evidence_ref)
        ),
        "dependency_context": dependency_context,
    }
    return WorkingContextProjection(
        **values,
        working_context_digest=canonical_json_digest(values),
    )


def build_dependency_prompt_block(
    manifest: WaterbodyDependencyManifest,
    *,
    mode: InferenceMode,
    windows: tuple[DependencyPromptWindow, ...],
) -> WaterbodyDependencyPromptBlock:
    return WaterbodyDependencyPromptBlock(
        manifest_digest=manifest.manifest_digest,
        current_waterbody_comid=manifest.target_comid,
        mode=mode,
        windows=windows,
        dependencies=tuple(
            DependencyPromptEntry(
                upstream_comid=item.upstream_comid,
                dependency_status=item.dependency_status,
                result_ref=item.result_ref,
            )
            for item in manifest.dependencies
        ),
    )


def require_safe_prompt_text(value: str) -> str:
    if len(value) > _MAX_SUMMARY_CHARACTERS:
        raise ValueError("safe prompt text exceeds the bounded character limit")
    return require_safe_prompt_payload(value)


def require_safe_prompt_payload(value: str) -> str:
    lowered = value.lower()
    if any(marker in lowered for marker in _UNSAFE_TEXT_MARKERS):
        raise ValueError("safe prompt text must not contain secrets, URLs, archives, or raw provider data")
    return value

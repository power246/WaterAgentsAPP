"""M9 HTTP contract models. Routes are installed by later milestones."""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, SecretStr, StrictBool, StringConstraints, field_validator, model_validator

from app.domain.common import DomainModel, NonEmptyString, UtcDatetime
from app.domain.memory_rag import RAGIndexHealth, RAGProfileHandoffState
from app.domain.settings_admin import (
    AdminActionKind, AdminConfirmation, AdminOperation, AdminSafeError,
    CredentialName, CredentialStatus, Digest, MutableDefaults, Revision,
    SettingsCatalogProjection, UnitPolicyChange, UnitPolicyEntry, UnitPolicyProjection,
    UnitPolicyVersion, Version,
)
from app.api.schemas.analysis_runs import AnalysisRunErrorResponse


class SettingsResponse(DomainModel):
    schema_version: Literal[1] = 1
    revision: Revision
    defaults: MutableDefaults
    catalog: SettingsCatalogProjection
    unit_policy: UnitPolicyProjection
    updated_at: UtcDatetime | None

    @model_validator(mode="after")
    def validate_revision_timestamp(self) -> Self:
        if (self.revision == 0) != (self.updated_at is None):
            raise ValueError("revision zero is the only unsaved settings response")
        return self


class ReplaceSettingsRequest(DomainModel):
    expected_revision: Revision
    defaults: MutableDefaults


class SettingsConflictResponse(AnalysisRunErrorResponse):
    current_settings: SettingsResponse


class PreviewUnitPolicyRequest(DomainModel):
    base_version: Version
    entries: Annotated[tuple[UnitPolicyEntry, ...], Field(max_length=154)]


class UnitPolicyPreviewResponse(DomainModel):
    base_version: Version
    proposed: UnitPolicyProjection
    changes: tuple[UnitPolicyChange, ...]
    affected_dimensions: tuple[NonEmptyString, ...]
    registry_digest: Digest
    confirmation: AdminConfirmation | None


class ConfirmActionRequest(DomainModel):
    confirmation_token: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    confirmation_phrase: Annotated[str, StringConstraints(min_length=1, max_length=100)]


class UnitPolicyResponse(UnitPolicyVersion):
    pass


class KeyStatusItem(CredentialStatus):
    pass


class KeyStatusResponse(DomainModel):
    keys: tuple[KeyStatusItem, ...]

    @model_validator(mode="after")
    def validate_allowlist_order(self) -> Self:
        names = tuple(item.key_name for item in self.keys)
        if names != tuple(name for name in CredentialName if name in names):
            raise ValueError("key status must use the fixed allowlist order")
        return self


class PutCredentialRequest(DomainModel):
    value: SecretStr

    @field_validator("value")
    @classmethod
    def validate_value(cls, secret: SecretStr) -> SecretStr:
        value = secret.get_secret_value()
        if not value.strip() or "\x00" in value or len(value) > 4096:
            raise ValueError("credential value is invalid")
        return secret


class CredentialTestResponse(DomainModel):
    key_name: CredentialName
    status: Literal["succeeded", "failed"]
    safe_error: AdminSafeError | None
    checked_at: UtcDatetime

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if (self.status == "failed") != (self.safe_error is not None):
            raise ValueError("test status and safe error disagree")
        return self


StorageState = Literal["available", "unavailable", "unknown", "not_present"]


class StorageItemStatus(DomainModel):
    state: StorageState


class AdminStorageStatusResponse(DomainModel):
    history: StorageItemStatus
    rag_index: StorageItemStatus
    evidence_cache: StorageItemStatus
    credential_store: StorageItemStatus


class AdminRAGStatusResponse(DomainModel):
    index: RAGIndexHealth
    handoff: RAGProfileHandoffState


class AdminOperationSnapshot(AdminOperation):
    pass


class AdminOperationListResponse(DomainModel):
    items: tuple[AdminOperationSnapshot, ...]
    next_cursor: NonEmptyString | None


class RunSelection(DomainModel):
    kind: Literal["runs"] = "runs"
    run_ids: Annotated[tuple[UUID, ...], Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def validate_unique(self) -> Self:
        if len(self.run_ids) != len(set(self.run_ids)):
            raise ValueError("run ids must be unique")
        return self


class AllHistorySelection(DomainModel):
    kind: Literal["all_history"] = "all_history"


class HistoryCleanupPreviewRequest(DomainModel):
    selection: Annotated[RunSelection | AllHistorySelection, Field(discriminator="kind")]
    cascade_consumers: StrictBool


class HistoryCleanupEstimate(DomainModel):
    runs: Annotated[int, Field(strict=True, ge=0)]
    sessions: Annotated[int, Field(strict=True, ge=0)]
    transcript_items: Annotated[int, Field(strict=True, ge=0)]
    retained_idempotency_tombstones: Annotated[int, Field(strict=True, ge=0)]


class HistoryCleanupPreviewResponse(DomainModel):
    requested_run_ids: tuple[UUID, ...]
    delete_run_ids: tuple[UUID, ...]
    delete_order: tuple[UUID, ...]
    dependencies: tuple[UUID, ...]
    cascade_required: StrictBool
    cascade_authorized: StrictBool
    estimated: HistoryCleanupEstimate
    preserved: tuple[Literal["api_keys", "settings", "unit_policy_versions"], ...]
    confirmation: AdminConfirmation | None


class ProfileHandoffPreviewResponse(DomainModel):
    action: Literal[AdminActionKind.PROFILE_HANDOFF] = AdminActionKind.PROFILE_HANDOFF
    handoff: RAGProfileHandoffState
    estimated_jobs: Annotated[int, Field(strict=True, ge=0)]
    provider_usage_warning: StrictBool
    confirmation: AdminConfirmation | None


SafeErrorCode = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$")]


class RetryFailedJobsPreviewRequest(DomainModel):
    safe_error_codes: Annotated[tuple[SafeErrorCode, ...], Field(min_length=1, max_length=100)]
    limit: Annotated[int, Field(strict=True, ge=1, le=100)]

    @model_validator(mode="after")
    def validate_unique(self) -> Self:
        if len(self.safe_error_codes) != len(set(self.safe_error_codes)):
            raise ValueError("safe error codes must be unique")
        return self


class GenerationPreviewResponse(DomainModel):
    action: Literal[AdminActionKind.RETRY_FAILED_JOBS, AdminActionKind.GENERATION_DELETION]
    generation_id: NonEmptyString
    affected_jobs: Annotated[int, Field(strict=True, ge=0)]
    confirmation: AdminConfirmation | None


M9_PUBLIC_SCHEMAS = (
    SettingsResponse, ReplaceSettingsRequest, SettingsConflictResponse,
    PreviewUnitPolicyRequest,
    UnitPolicyPreviewResponse, ConfirmActionRequest, UnitPolicyResponse,
    KeyStatusItem, KeyStatusResponse, PutCredentialRequest,
    CredentialTestResponse, AdminStorageStatusResponse, AdminRAGStatusResponse,
    AdminOperationSnapshot, AdminOperationListResponse,
    HistoryCleanupPreviewRequest, HistoryCleanupPreviewResponse,
    ProfileHandoffPreviewResponse, RetryFailedJobsPreviewRequest,
    GenerationPreviewResponse,
)

"""Stable public M9 error classes; messages never interpolate unsafe input."""


class SettingsAdminError(Exception):
    code = "settings_admin_error"
    status = 500
    retryable = False
    stage = "settings_admin"
    public_message = "The operation could not be completed."

    def __str__(self) -> str:
        return self.public_message


class SettingsRevisionConflictError(SettingsAdminError):
    code = "settings_revision_conflict"
    status = 409
    public_message = "Settings changed; reload before saving."

    def __init__(self, current=None) -> None:
        self.current = current
        super().__init__()


class SettingsInvalidDefaultsError(SettingsAdminError):
    code = "settings_invalid_defaults"
    status = 422
    public_message = "The settings selection is invalid."


class LocalMutationRejectedError(SettingsAdminError):
    code = "local_mutation_rejected"
    status = 403
    stage = "request_guard"
    public_message = "The local request origin is not allowed."


class SettingsUnavailableError(SettingsAdminError):
    code = "settings_unavailable"
    status = 503
    retryable = True
    public_message = "Settings are unavailable."


class CredentialNotAllowedError(SettingsAdminError):
    code = "credential_not_allowed"
    status = 404
    public_message = "The credential name is not allowed."


class CredentialStoreUnavailableError(SettingsAdminError):
    code = "credential_store_unavailable"
    status = 503
    retryable = True
    public_message = "The credential store is unavailable."


class CredentialMissingError(SettingsAdminError):
    code = "credential_missing"
    status = 409
    public_message = "The credential is not configured."


class CredentialTestFailedError(SettingsAdminError):
    code = "credential_test_failed"
    status = 503
    retryable = True
    public_message = "The connection test failed."


class CredentialValueInvalidError(SettingsAdminError):
    code = "credential_value_invalid"
    status = 422
    public_message = "The credential value is invalid."


class UnitUnknownError(SettingsAdminError):
    code = "unit_unknown"
    status = 422
    public_message = "The parameter or unit is not registered."


class UnitDimensionMismatchError(SettingsAdminError):
    code = "unit_dimension_mismatch"
    status = 422
    public_message = "The unit dimension does not match the parameter."


class UnitNotApplicableError(SettingsAdminError):
    code = "unit_not_applicable"
    status = 422
    public_message = "The selected unit cannot be applied."


class UnitPolicyRevisionConflictError(SettingsAdminError):
    code = "unit_policy_revision_conflict"
    status = 409
    public_message = "The unit policy changed; preview again."


class UnitPolicyIdempotencyConflictError(SettingsAdminError):
    code = "idempotency_key_conflict"
    status = 409
    public_message = "The idempotency key was already used with a different request body."


class AdminPreviewExpiredError(SettingsAdminError):
    code = "admin_preview_expired"
    status = 409
    public_message = "The preview expired; preview again."


class AdminPreviewStaleError(SettingsAdminError):
    code = "admin_preview_stale"
    status = 409
    public_message = "The affected facts changed; preview again."


class AdminConfirmationInvalidError(SettingsAdminError):
    code = "admin_confirmation_invalid"
    status = 409
    public_message = "The confirmation is invalid."


class AdminOperationConflictError(SettingsAdminError):
    code = "admin_operation_conflict"
    status = 409
    public_message = "An operation for this resource is already active."


class AdminOperationNotFoundError(SettingsAdminError):
    code = "admin_operation_not_found"
    status = 404
    public_message = "The operation was not found."


class AdminOperationCursorError(SettingsAdminError):
    code = "admin_operation_cursor_invalid"
    status = 422
    public_message = "The operation cursor is invalid."


class HistoryCleanupDependencyConflictError(SettingsAdminError):
    code = "history_cleanup_dependency_conflict"
    status = 409
    public_message = "Retained history depends on the selected runs."


class HistoryCleanupSelectionTooLargeError(SettingsAdminError):
    code = "history_cleanup_selection_too_large"
    status = 422
    public_message = "The history selection exceeds the Admin limit."


class IdempotencyResourceDeletedError(SettingsAdminError):
    code = "idempotency_resource_deleted"
    status = 410
    stage = "create_run"
    public_message = "The resource for this idempotency key was deleted."


class RAGGenerationConflictError(SettingsAdminError):
    code = "rag_generation_conflict"
    status = 409
    public_message = "The RAG generation changed or is not eligible."


class RAGGenerationNotFoundError(SettingsAdminError):
    code = "rag_generation_not_found"
    status = 404
    public_message = "The RAG generation was not found."


class RAGReaderDrainRequiredError(SettingsAdminError):
    code = "rag_reader_drain_required"
    status = 409
    public_message = "Readers still use this generation."


class RAGRetentionNotElapsedError(SettingsAdminError):
    code = "rag_retention_not_elapsed"
    status = 409
    public_message = "The retention period has not elapsed."


M9_PUBLIC_ERRORS = (
    SettingsRevisionConflictError, SettingsInvalidDefaultsError,
    LocalMutationRejectedError, SettingsUnavailableError,
    CredentialNotAllowedError, CredentialStoreUnavailableError,
    CredentialMissingError, CredentialTestFailedError, CredentialValueInvalidError,
    UnitUnknownError, UnitDimensionMismatchError, UnitNotApplicableError,
    UnitPolicyRevisionConflictError, UnitPolicyIdempotencyConflictError,
    AdminPreviewExpiredError,
    AdminPreviewStaleError, AdminConfirmationInvalidError,
    AdminOperationConflictError, AdminOperationNotFoundError,
    AdminOperationCursorError,
    HistoryCleanupDependencyConflictError, IdempotencyResourceDeletedError,
    HistoryCleanupSelectionTooLargeError,
    RAGGenerationNotFoundError, RAGGenerationConflictError,
    RAGReaderDrainRequiredError, RAGRetentionNotElapsedError,
)

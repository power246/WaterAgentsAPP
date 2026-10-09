from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import UUID

import pytest
from pydantic import SecretStr, ValidationError

from app.application.settings_admin.contracts import (
    project_credential_status, safe_settings_catalog, validate_mutable_defaults,
    validate_unit_policy,
)
from app.application.settings_admin.errors import (
    UnitDimensionMismatchError, UnitPolicyRevisionConflictError, UnitUnknownError,
)
from app.domain import EmbeddingProfile, ModelCapability, ModelCatalogSnapshot, ModelRole
from app.domain.settings_admin import (
    AdminActionKind, AdminOperation, AdminOperationState, AdminProgress,
    ConfirmationBinding, CredentialLease, CredentialName,
    CredentialSource, HistoryCleanupResult, IdempotencyTombstone,
    MutableDefaults, SettingsSnapshot, StoredCredential, UnitPolicyEntry,
    UnitPolicyProjection,
)
from app.infrastructure.config import load_parameter_capability_registry


NOW = datetime(2026, 10, 8, tzinfo=timezone.utc)
DIGEST = "a" * 64
PROFILE = EmbeddingProfile(
    provider="provider", model="embed", output_dimensions=8,
    max_input_tokens=512, semantic_min_similarity=0.7,
)
CATALOG = ModelCatalogSnapshot(capabilities=(
    ModelCapability(provider="provider", model="parser", roles=(ModelRole.QUERY_PARSER,)),
    ModelCapability(
        provider="provider", model="waterbody", roles=(ModelRole.WATERBODY,),
        context_window_tokens=1000, max_output_tokens=500,
    ),
    ModelCapability(
        provider="provider", model="embed", roles=(ModelRole.EMBEDDING,),
        embedding_profile=PROFILE,
    ),
))
DEFAULTS = {
    "analysis_timezone": "America/Chicago",
    "parser_model": {"provider": "provider", "model": "parser"},
    "waterbody_model": {"provider": "provider", "model": "waterbody"},
    "memory_mode": "stateless", "time_resolution": "12h",
    "desired_embedding_profile_id": PROFILE.profile_id,
}


def test_catalog_and_defaults_only_expose_registered_roles() -> None:
    defaults = MutableDefaults.model_validate(DEFAULTS)
    validate_mutable_defaults(defaults, CATALOG)
    projection = safe_settings_catalog(CATALOG, iana_timezones=("UTC", "America/Chicago"))
    assert len(projection.models) == 2
    assert len(projection.embedding_profiles) == 1
    assert "context_window_tokens" not in projection.model_dump_json()
    assert "embedding_profile" not in projection.models[0].model_dump_json()
    with pytest.raises(ValueError, match="waterbody_model"):
        validate_mutable_defaults(
            MutableDefaults.model_validate({**DEFAULTS, "waterbody_model": DEFAULTS["parser_model"]}),
            CATALOG,
        )
    with pytest.raises(ValueError, match="desired_embedding_profile_id"):
        validate_mutable_defaults(
            MutableDefaults.model_validate({**DEFAULTS, "desired_embedding_profile_id": "sha256:" + DIGEST}),
            CATALOG,
        )


def test_settings_revision_and_unit_identity_round_trip() -> None:
    snapshot = SettingsSnapshot(
        revision=2, defaults=MutableDefaults.model_validate(DEFAULTS),
        unit_policy=UnitPolicyProjection(version=3, registry_digest=DIGEST, entries=()),
        updated_at=NOW,
    )
    assert SettingsSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot
    with pytest.raises(ValidationError):
        SettingsSnapshot.model_validate({
            **snapshot.model_dump(), "revision": 0,
        })


@pytest.mark.parametrize("field,value", [
    ("memory_mode", "unknown"), ("analysis_timezone", "Mars/Olympus"),
    ("time_resolution", "10h"), ("extra", "unbounded"),
])
def test_defaults_reject_invalid_or_unknown_fields(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        MutableDefaults.model_validate({**DEFAULTS, field: value})


def test_catalog_boolean_does_not_coerce_numeric_input() -> None:
    option = safe_settings_catalog(CATALOG, iana_timezones=("UTC",)).models[0]
    with pytest.raises(ValidationError):
        option.model_validate({**option.model_dump(), "available": 1})


def test_unit_policy_checks_digest_registration_dimension_and_identity() -> None:
    registry = load_parameter_capability_registry()
    policy = UnitPolicyProjection(
        version=2, registry_digest=registry.manifest_digest, entries=(),
    )
    validate_unit_policy(policy, registry)
    with pytest.raises(UnitPolicyRevisionConflictError):
        validate_unit_policy(policy.model_copy(update={"registry_digest": DIGEST}), registry)
    valid_entry = {"parameter": {"code_system": "usgs", "code": "00060"},
                   "target_unit_id": "cubic_meter_per_second"}
    validate_unit_policy(policy.model_copy(update={
        "entries": (UnitPolicyEntry.model_validate(valid_entry),),
    }), registry)
    for entry in (
        {**valid_entry, "target_unit_id": "not_registered"},
        {**valid_entry, "parameter": {"code_system": "usgs", "code": "99999"}},
    ):
        with pytest.raises(UnitUnknownError):
            validate_unit_policy(UnitPolicyProjection.model_validate({
                "version": 2, "registry_digest": registry.manifest_digest,
                "entries": [entry],
            }), registry)
    with pytest.raises(UnitDimensionMismatchError):
        validate_unit_policy(UnitPolicyProjection.model_validate({
            "version": 2, "registry_digest": registry.manifest_digest,
            "entries": [{**valid_entry, "target_unit_id": "foot"}],
        }), registry)
    with pytest.raises(ValidationError):
        UnitPolicyProjection.model_validate({
            "version": 2, "registry_digest": registry.manifest_digest,
            "entries": [], "formula": "arbitrary",
        })
    with pytest.raises(ValidationError):
        UnitPolicyProjection.model_validate({
            "version": 2, "registry_digest": "invalid", "entries": [],
        })


def test_secret_envelopes_and_public_projection_never_emit_raw_value() -> None:
    secret = "test-secret-1234"
    stored = StoredCredential(value=SecretStr(secret), updated_at=NOW)
    lease = CredentialLease(
        name=CredentialName.OPENAI, value=stored.value,
        source=CredentialSource.USER_STORE, issued_at=NOW,
    )
    status = project_credential_status(
        CredentialName.OPENAI, source=CredentialSource.USER_STORE,
        value=stored.value, updated_at=NOW, store_available=True,
    )
    assert status.masked == "••••1234"
    for public in (repr(stored), repr(lease), repr(status), status.model_dump_json()):
        assert secret not in public
        assert "test-secret" not in public
    for name in ("arbitrary", "GOOGLE_GEOCODING"):
        with pytest.raises(ValueError):
            CredentialName(name)
    with pytest.raises(ValidationError):
        status.model_validate({**status.model_dump(), "value": secret})
    with pytest.raises(ValidationError):
        status.model_validate({**status.model_dump(), "masked": secret})


def _operation(state: AdminOperationState, **changes) -> AdminOperation:
    values = {
        "operation_id": UUID("90000000-0000-4000-8000-000000000001"),
        "kind": AdminActionKind.HISTORY_CLEANUP, "state": state,
        "resource_identity": "history:fingerprint", "progress": AdminProgress(),
        "created_at": NOW, "started_at": NOW + timedelta(seconds=1),
        "finished_at": None, "safe_result": None, "safe_error": None,
    }
    values.update(changes)
    return AdminOperation(**values)


def test_admin_transition_is_closed_and_terminal_snapshot_is_immutable() -> None:
    pending = _operation(AdminOperationState.PENDING, started_at=None)
    running = _operation(AdminOperationState.RUNNING)
    success = _operation(
        AdminOperationState.SUCCEEDED, finished_at=NOW + timedelta(seconds=2),
        safe_result=HistoryCleanupResult(
            deleted_run_ids=(), retained_idempotency_tombstones=0,
        ),
    )
    assert pending.transition(running).transition(success) == success
    with pytest.raises(ValueError, match="transition"):
        success.transition(running)
    with pytest.raises(ValueError, match="identity"):
        pending.transition(running.model_copy(update={"resource_identity": "other"}))
    with pytest.raises(ValidationError):
        pending.transition(running.model_copy(update={"finished_at": NOW}))
    with pytest.raises(ValidationError):
        _operation(AdminOperationState.FAILED, finished_at=NOW + timedelta(seconds=2))
    with pytest.raises(ValidationError):
        _operation(
            AdminOperationState.SUCCEEDED, finished_at=NOW + timedelta(seconds=2),
            safe_result={"kind": "profile_handoff", "active_generation_id": "g1"},
        )


def test_confirmation_binds_action_facts_phrase_and_expiry() -> None:
    token = "opaque-token"
    facts = sha256(b"normalized facts").digest()
    binding = ConfirmationBinding(
        token_digest=sha256(token.encode()).digest(),
        action=AdminActionKind.HISTORY_CLEANUP, fact_fingerprint=facts,
        required_phrase="DELETE 1 RUN", expires_at=NOW + timedelta(minutes=1),
    )
    valid = dict(
        token=token, action=AdminActionKind.HISTORY_CLEANUP,
        fact_fingerprint=facts, phrase="DELETE 1 RUN", now=NOW,
    )
    assert binding.matches(**valid)
    for change in (
        {"token": "other"}, {"action": AdminActionKind.PROFILE_HANDOFF},
        {"fact_fingerprint": sha256(b"changed").digest()},
        {"phrase": "DELETE 2 RUNS"}, {"now": NOW + timedelta(minutes=1)},
    ):
        assert not binding.matches(**{**valid, **change})
    assert token not in repr(binding)


def test_tombstone_rejects_non_v4_key_or_unbound_fingerprint() -> None:
    values = dict(
        idempotency_key=UUID("90000000-0000-4000-8000-000000000001"),
        request_fingerprint=sha256(b"raw request").digest(),
        deleted_run_id=UUID("10000000-0000-4000-8000-000000000001"),
        deleted_at=NOW,
    )
    assert IdempotencyTombstone(**values).deleted_at == NOW
    with pytest.raises(ValueError):
        IdempotencyTombstone(**{**values, "request_fingerprint": b"short"})

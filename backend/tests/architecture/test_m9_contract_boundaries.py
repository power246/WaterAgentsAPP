import inspect
from pathlib import Path
from typing import get_type_hints

from app.api.schemas.settings_admin import M9_PUBLIC_SCHEMAS
from app.domain.settings_admin import CredentialName, StoredCredential
from app.ports import AdminOperationStorePort, KeyStorePort, SettingsStorePort


def test_m9_ports_have_typed_domain_envelopes_and_no_adapter_imports() -> None:
    assert get_type_hints(KeyStorePort.get)["key_name"] is CredentialName
    assert get_type_hints(KeyStorePort.get)["return"] == StoredCredential | None
    for port in (SettingsStorePort, AdminOperationStorePort, KeyStorePort):
        source = inspect.getsource(inspect.getmodule(port))
        assert "app.infrastructure" not in source
        assert "fastapi" not in source.lower()
        assert "keyring" not in source.lower()
        assert "sqlalchemy" not in source.lower()


def test_public_m9_models_have_no_secret_envelope_or_freeform_json() -> None:
    for model in M9_PUBLIC_SCHEMAS:
        assert model.model_config["extra"] == "forbid"
        fields = model.model_fields
        assert "api_key" not in fields
        assert "secret" not in fields
        assert "credential" not in fields
        assert "raw_response" not in fields
        assert "StoredCredential" not in str(model.model_json_schema())
        assert "CredentialLease" not in str(model.model_json_schema())


def test_m9_has_one_current_run_configuration_and_no_adapter_branch() -> None:
    root = Path(__file__).resolve().parents[2] / "app"
    source = (root / "application" / "analysis_runs" / "models.py").read_text(encoding="utf-8")
    assert source.count("class AnalysisConfigurationSnapshot(") == 1
    assert "AnalysisConfigurationSnapshotV2" not in source
    assert "unit_policy_version" in source
    assert "unit_registry_digest" in source
    for path in (root / "domain" / "settings_admin").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "feature_flag" not in text
        assert "dict[str, object]" not in text

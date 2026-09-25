import json
from importlib.resources import files

import pytest

from app.domain.hydrology import ParameterCapabilityConfigurationError
from app.infrastructure.config import (
    AppSettings,
    PARAMETER_CAPABILITY_RESOURCE,
    load_parameter_capability_registry,
    load_parameter_capability_registry_from_bytes,
)
from app.main import create_app


def manifest_payload() -> dict[str, object]:
    return json.loads(
        files("app.infrastructure.config")
        .joinpath(PARAMETER_CAPABILITY_RESOURCE)
        .read_text(encoding="utf-8")
    )


def test_default_manifest_loads_independently_of_current_working_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    registry = load_parameter_capability_registry()
    monkeypatch.chdir(tmp_path)

    assert load_parameter_capability_registry() is registry
    assert len(registry.capabilities) == 154


def test_application_composition_reuses_the_validated_registry() -> None:
    registry = load_parameter_capability_registry()

    application = create_app(AppSettings(_env_file=None))

    assert application.state.parameter_capability_registry is registry


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("availability_providers", ["unknown_provider"]),
        ("allowed_indirect_evidence_rules", ["unknown_rule"]),
    ],
)
def test_loader_fails_closed_for_unknown_manifest_references(
    field: str,
    value: list[str],
) -> None:
    payload = manifest_payload()
    payload["capabilities"][0][field] = value

    with pytest.raises(
        ParameterCapabilityConfigurationError,
        match="invalid parameter capability manifest",
    ):
        load_parameter_capability_registry_from_bytes(
            json.dumps(payload).encode("utf-8")
        )


def test_loader_rejects_missing_and_duplicate_catalog_entries() -> None:
    missing = manifest_payload()
    missing["capabilities"].pop()
    with pytest.raises(ParameterCapabilityConfigurationError, match="exactly 154"):
        load_parameter_capability_registry_from_bytes(
            json.dumps(missing).encode("utf-8")
        )

    duplicate = manifest_payload()
    duplicate["capabilities"][-1] = duplicate["capabilities"][0]
    with pytest.raises(ParameterCapabilityConfigurationError, match="unique"):
        load_parameter_capability_registry_from_bytes(
            json.dumps(duplicate).encode("utf-8")
        )

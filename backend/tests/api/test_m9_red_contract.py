"""Expected-red M9 contracts; --runxfail shows the missing capability failures.

Each strict xfail is owned by a later milestone. Keep these tests against the
production app/types so a typo or missing test-only fixture cannot cause red.
"""

import pytest

from app.application.analysis_runs.models import AnalysisConfigurationSnapshot
from app.infrastructure.config import AppSettings
from app.main import create_app


@pytest.fixture(scope="module")
def application():
    return create_app(AppSettings(_env_file=None))


@pytest.mark.xfail(strict=True, reason="M9 Milestones 1 and 3: settings routes")
def test_settings_routes_follow_the_frozen_contract(application) -> None:
    paths = application.openapi()["paths"]
    assert {"get", "put"} <= set(paths.get("/api/settings", {}))
    assert "post" in paths.get("/api/settings/unit-policies/preview", {})
    assert "post" in paths.get("/api/settings/unit-policies", {})


@pytest.mark.xfail(strict=True, reason="M9 Milestones 1 and 4: OS key status and routes")
def test_keys_expose_masked_status_and_explicit_test_only(application) -> None:
    paths = application.openapi()["paths"]
    assert "get" in paths.get("/api/keys", {})
    assert {"put", "delete"} <= set(paths.get("/api/keys/{key_name}", {}))
    assert "post" in paths.get("/api/keys/{key_name}/test", {})
    schemas = application.openapi()["components"]["schemas"]
    status = schemas["KeyStatusItem"]["properties"]
    assert {"key_name", "configured", "source", "masked", "store_available"} <= set(status)
    assert "value" not in status


@pytest.mark.xfail(strict=True, reason="M9 Milestones 1 and 6: Admin operations")
def test_admin_operation_status_and_preview_routes_exist(application) -> None:
    paths = application.openapi()["paths"]
    assert "get" in paths.get("/api/admin/operations/{operation_id}", {})
    assert "post" in paths.get("/api/admin/history-cleanups/preview", {})
    assert "post" in paths.get("/api/admin/history-cleanups", {})
    properties = application.openapi()["components"]["schemas"][
        "AdminOperationSnapshot"
    ]["properties"]
    assert {"operation_id", "kind", "state", "progress", "safe_error"} <= set(properties)


def test_run_configuration_carries_unit_policy_identity() -> None:
    assert {"unit_policy_version", "unit_registry_digest"} <= set(
        AnalysisConfigurationSnapshot.model_fields
    )


@pytest.mark.xfail(strict=True, reason="M9 Milestones 1 and 6: deleted create key")
def test_create_contract_declares_deleted_idempotency_receipt(application) -> None:
    response_codes = application.openapi()["paths"]["/api/analysis/runs"]["post"]["responses"]
    assert "410" in response_codes


@pytest.mark.xfail(strict=True, reason="M9 Milestone 8: same-origin static frontend")
def test_production_app_registers_static_frontend_navigation(application) -> None:
    assert any(
        (getattr(route, "path", None) == "/")
        or str(getattr(route, "path", "")).endswith(":path}")
        for route in application.routes
    )

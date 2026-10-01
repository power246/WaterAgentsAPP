from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from app.infrastructure.runtime.m5_test import (
    M5TestExecuteRequest,
    create_m5_test_app,
)


HAPPY_FIXTURE = "station-backed-two-windows"


def _payload(application, fixture_scope_id=HAPPY_FIXTURE, **overrides):
    composition = application.state.m5_test_composition
    scope = composition.fixtures[fixture_scope_id].scope
    payload = {
        "fixture_scope_id": fixture_scope_id,
        "tool_name": "collect_water_observations",
        "window_ids": [window.window_id for window in scope.time_windows],
        "parameter_keys": [
            parameter.model_dump(mode="json")
            for parameter in (
                *scope.requested_parameters,
                *scope.supporting_parameters,
            )
        ],
        "options": {},
    }
    payload.update(overrides)
    return payload


def test_happy_fixture_executes_and_reads_refs_from_real_transcript_path() -> None:
    application = create_m5_test_app()
    with TestClient(application) as client:
        response = client.post(
            "/__test__/m5/evidence-tools:execute",
            json=_payload(application),
        )

        assert response.status_code == 200
        body = response.json()
        result = body["result"]
        assert result["execution_status"] == "completed"
        assert result["collection_status"] == "available"
        assert result["source_record_refs"]
        transcript = client.get(
            f"/__test__/m5/runs/{body['run_id']}/transcript"
        )
        assert transcript.status_code == 200
        items = transcript.json()["items"]
        item_ids = {item["item_id"] for item in items}
        assert body["result_ref"]["item_id"] in item_ids
        assert {
            reference["item_id"] for reference in result["source_record_refs"]
        } <= item_ids
        assert all(
            item["parse_status"] == "ok"
            for item in items
            if item["content_type"].startswith("evidence_")
        )


@pytest.mark.parametrize(
    ("fixture_scope_id", "execution_status", "collection_status", "error_code"),
    (
        ("station-backed-empty", "completed", "empty", None),
        (
            "station-backed-unavailable",
            "failed",
            None,
            "evidence_provider_unavailable",
        ),
        (
            "station-backed-invalid",
            "failed",
            None,
            "evidence_provider_response_invalid",
        ),
        ("station-backed-cancelled", "cancelled", None, "evidence_cancelled"),
    ),
)
def test_fixture_outcomes_use_production_error_and_result_semantics(
    fixture_scope_id,
    execution_status,
    collection_status,
    error_code,
) -> None:
    application = create_m5_test_app()
    with TestClient(application) as client:
        response = client.post(
            "/__test__/m5/evidence-tools:execute",
            json=_payload(application, fixture_scope_id),
        )

        assert response.status_code == 200
        result = response.json()["result"]
        assert result["execution_status"] == execution_status
        assert result["collection_status"] == collection_status
        assert (
            None if result["safe_error"] is None else result["safe_error"]["code"]
        ) == error_code


def test_same_request_is_single_flight_deduplicated_and_still_persisted() -> None:
    application = create_m5_test_app()
    with TestClient(application) as client:
        payload = _payload(application)
        first = client.post(
            "/__test__/m5/evidence-tools:execute",
            json=payload,
        ).json()
        second = client.post(
            "/__test__/m5/evidence-tools:execute",
            json=payload,
        ).json()

        assert first["result"]["deduplicated"] is False
        assert second["result"]["deduplicated"] is True
        assert second["result"]["deduplicated_from_result_ref"] == first["result_ref"]
        transcript = client.get(
            f"/__test__/m5/runs/{first['run_id']}/transcript"
        ).json()
        result_ids = {
            item["item_id"]
            for item in transcript["items"]
            if item["content_type"] == "evidence_tool_result"
        }
        assert {first["result_ref"]["item_id"], second["result_ref"]["item_id"]} <= result_ids


def test_inspection_reads_a_bounded_page_from_saved_source_records() -> None:
    application = create_m5_test_app()
    with TestClient(application) as client:
        payload = _payload(application)
        collected = client.post(
            "/__test__/m5/evidence-tools:execute",
            json=payload,
        ).json()
        source_item_id = collected["result"]["source_record_refs"][0]["item_id"]
        inspected = client.post(
            "/__test__/m5/evidence-tools:execute",
            json={
                **payload,
                "tool_name": "inspect_evidence_records",
                "options": {
                    "source_item_id": source_item_id,
                    "record_limit": 1,
                },
            },
        )

        assert inspected.status_code == 200
        body = inspected.json()
        assert body["result"]["execution_status"] == "completed"
        assert body["inspection"]["source_item_id"] == source_item_id
        assert len(body["inspection"]["records"]) == 1
        assert body["inspection"]["total_records"] >= 1


@pytest.mark.parametrize(
    "extra",
    (
        {"provider": "arbitrary"},
        {"base_url": "https://attacker.example"},
        {"secret": "do-not-accept"},
        {"bypass_writer": True},
    ),
)
def test_request_rejects_arbitrary_provider_and_control_fields(extra) -> None:
    application = create_m5_test_app()
    with TestClient(application) as client:
        payload = _payload(application)
        payload.update(extra)

        response = client.post(
            "/__test__/m5/evidence-tools:execute",
            json=payload,
        )

        assert response.status_code == 422


def test_request_rejects_unknown_fixture_tool_window_parameter_and_option() -> None:
    application = create_m5_test_app()
    with TestClient(application) as client:
        unknown_fixture = client.post(
            "/__test__/m5/evidence-tools:execute",
            json={
                **_payload(application),
                "fixture_scope_id": "unknown",
            },
        )
        assert unknown_fixture.status_code == 404

        cases = (
            {"tool_name": "unknown_tool"},
            {"window_ids": ["window-not-authorized"]},
            {
                "parameter_keys": [
                    {"code_system": "usgs", "code": "99999"},
                ]
            },
        )
        for override in cases:
            response = client.post(
                "/__test__/m5/evidence-tools:execute",
                json={**_payload(application), **override},
            )
            assert response.status_code == 200
            assert response.json()["result"]["execution_status"] == "failed"
            assert response.json()["result"]["safe_error"]["code"] == (
                "evidence_scope_violation"
            )

        option = _payload(application)
        option["options"] = {"base_url": "https://attacker.example"}
        assert client.post(
            "/__test__/m5/evidence-tools:execute",
            json=option,
        ).status_code == 422


def test_direct_application_and_http_use_the_same_executor_contract() -> None:
    direct_app = create_m5_test_app()
    with TestClient(direct_app) as direct_client:
        payload = _payload(direct_app)
        request = M5TestExecuteRequest.model_validate(deepcopy(payload))
        _, direct_execution, _ = direct_client.portal.call(
            direct_app.state.m5_test_composition.execute,
            request,
        )

    http_app = create_m5_test_app()
    with TestClient(http_app) as http_client:
        http_result = http_client.post(
            "/__test__/m5/evidence-tools:execute",
            json=_payload(http_app),
        ).json()["result"]

    direct_result = direct_execution.result.model_dump(mode="json")
    for field in (
        "tool_name",
        "execution_status",
        "collection_status",
        "counts",
        "safe_error",
    ):
        assert http_result[field] == direct_result[field]


def test_test_app_has_only_the_two_explicit_internal_routes() -> None:
    paths = set(create_m5_test_app().openapi()["paths"])
    assert paths == {
        "/__test__/m5/evidence-tools:execute",
        "/__test__/m5/runs/{run_id}/transcript",
    }

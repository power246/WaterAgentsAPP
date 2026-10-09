"""Settings and run snapshots through the production PostgreSQL composition."""

import asyncio
import os
from uuid import UUID, uuid4

import httpx
import pytest

from app.application.analysis_runs.models import AnalysisConfigurationSnapshot
from scripts.test_database import temporary_migrated_schema
from tests.m4_support import create_request, create_test_app


pytestmark = pytest.mark.skipif(
    not os.environ.get("WATERAGENTS_TEST_DB_NAME"),
    reason="dedicated PostgreSQL test database was not configured",
)


def test_settings_cas_and_run_history_survive_app_restart() -> None:
    async def round_trip(settings):
        app = create_test_app(settings, shutdown_grace_period=0)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                initial = (await client.get("/api/settings")).json()
                assert initial["revision"] == 0
                old = await client.post(
                    "/api/analysis/runs", json=create_request("old settings"),
                )
                assert old.status_code == 201
                defaults = {**initial["defaults"],
                            "analysis_timezone": "Asia/Shanghai"}
                changed = await client.put("/api/settings", json={
                    "expected_revision": 0, "defaults": defaults,
                })
                assert changed.status_code == 200
                stale = await client.put("/api/settings", json={
                    "expected_revision": 0, "defaults": initial["defaults"],
                })
                assert stale.status_code == 409
                assert stale.json()["current_settings"] == changed.json()
                new = await client.post(
                    "/api/analysis/runs", json=create_request("new settings"),
                )
                assert new.status_code == 201
                legacy_config = AnalysisConfigurationSnapshot.model_validate({
                    **old.json()["configuration"],
                    "unit_policy_version": "legacy",
                    "unit_registry_digest": "unknown",
                })
                legacy = await app.state.run_coordinator.create_run(
                    "legacy persisted run", legacy_config,
                )
                ids = tuple(UUID(response.json()["run_id"]) for response in (old, new))
                for run_id in (*ids, legacy.run_id):
                    for _ in range(500):
                        persistence = await app.state.run_coordinator.get_persistence_info(run_id)
                        if persistence.state.value != "pending":
                            break
                        await asyncio.sleep(0.01)
                    assert persistence.state.value == "up_to_date"

        restarted = create_test_app(settings, shutdown_grace_period=0)
        async with restarted.router.lifespan_context(restarted):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=restarted),
                base_url="http://testserver",
            ) as client:
                current = await client.get("/api/settings")
                assert current.status_code == 200
                assert current.json()["revision"] == 1
                assert current.json()["defaults"] == defaults
                for run_id, timezone, unit_version in (
                    (ids[0], initial["defaults"]["analysis_timezone"], 1),
                    (ids[1], "Asia/Shanghai", 1),
                    (legacy.run_id, initial["defaults"]["analysis_timezone"], "legacy"),
                ):
                    history = await client.get(
                        f"/api/analysis/runs/{run_id}?view=history"
                    )
                    assert history.status_code == 200
                    config = history.json()["run"]["configuration"]
                    assert config["analysis_timezone"] == timezone
                    assert config["unit_policy_version"] == unit_version
                    assert config["unit_registry_digest"] == (
                        "unknown" if unit_version == "legacy"
                        else initial["unit_policy"]["registry_digest"]
                    )

    with temporary_migrated_schema() as settings:
        asyncio.run(round_trip(settings))


def test_unit_policy_confirmation_and_run_snapshot_survive_restart() -> None:
    async def exercise(settings):
        before_run = after_run = None
        key = str(uuid4())
        app = create_test_app(settings, shutdown_grace_period=0)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver",
            ) as client:
                before_run = await client.post(
                    "/api/analysis/runs", json=create_request("before unit policy"),
                )
                assert before_run.status_code == 201
                selection = {"parameter": {"code_system": "usgs", "code": "00060"},
                             "target_unit_id": "cubic_meter_per_second"}
                preview = await client.post(
                    "/api/settings/unit-policies/preview",
                    json={"base_version": 1, "entries": [selection]},
                )
                assert preview.status_code == 200, preview.text
                token = preview.json()["confirmation"]
                body = {"confirmation_token": token["token"],
                        "confirmation_phrase": token["required_phrase"]}
                confirmed = await client.post(
                    "/api/settings/unit-policies", json=body,
                    headers={"Idempotency-Key": key},
                )
                assert confirmed.status_code == 201, confirmed.text
                assert confirmed.json()["version"] == 2
                unexecuted = await client.post(
                    "/api/settings/unit-policies/preview",
                    json={"base_version": 2, "entries": [{**selection,
                          "target_unit_id": "cubic_foot_per_second"}]},
                )
                assert unexecuted.status_code == 200
                stale_token = unexecuted.json()["confirmation"]
                after_run = await client.post(
                    "/api/analysis/runs", json=create_request("after unit policy"),
                )
                assert after_run.status_code == 201
                for response in (before_run, after_run):
                    assert response.json()["configuration"]["unit_policy_version"] == (
                        1 if response is before_run else 2
                    )
                    for _ in range(500):
                        state = await app.state.run_coordinator.get_persistence_info(
                            UUID(response.json()["run_id"])
                        )
                        if state.state.value != "pending":
                            break
                        await asyncio.sleep(0.01)
                    assert state.state.value == "up_to_date"

        restarted = create_test_app(settings, shutdown_grace_period=0)
        async with restarted.router.lifespan_context(restarted):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=restarted), base_url="http://testserver",
            ) as client:
                assert (await client.get("/api/settings")).json()["unit_policy"]["version"] == 2
                replay = await client.post(
                    "/api/settings/unit-policies", json=body,
                    headers={"Idempotency-Key": key},
                )
                assert replay.status_code == 201 and replay.json() == confirmed.json()
                invalid = await client.post(
                    "/api/settings/unit-policies",
                    json={"confirmation_token": stale_token["token"],
                          "confirmation_phrase": stale_token["required_phrase"]},
                    headers={"Idempotency-Key": str(uuid4())},
                )
                assert invalid.status_code == 409
                assert invalid.json()["error"]["code"] == "admin_confirmation_invalid"
                for response, version in ((before_run, 1), (after_run, 2)):
                    history = await client.get(
                        f"/api/analysis/runs/{response.json()['run_id']}?view=history"
                    )
                    assert history.status_code == 200
                    assert history.json()["run"]["configuration"]["unit_policy_version"] == version

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))

"""Unit policy confirmation is the only mutable global unit boundary."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import httpx

from app.infrastructure.config import AppSettings
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from tests.m4_support import create_test_app


@asynccontextmanager
async def _client():
    app = create_test_app(
        AppSettings(_env_file=None), transcript_store=InMemoryTranscriptStore(),
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
            base_url="http://testserver",
        ) as client:
            yield app, client


def _change():
    return {"parameter": {"code_system": "usgs", "code": "00060"},
            "target_unit_id": "cubic_meter_per_second"}


def test_preview_confirm_replay_and_settings_cas() -> None:
    async def exercise():
        async with _client() as (app, client):
            before = (await client.get("/api/settings")).json()
            preview = await client.post(
                "/api/settings/unit-policies/preview",
                json={"base_version": 1, "entries": [_change()]},
            )
            assert preview.status_code == 200, preview.text
            body = preview.json()
            assert body["proposed"]["version"] == 2
            assert len(body["proposed"]["entries"]) == 154
            assert body["affected_dimensions"] == ["volumetric_flow"]
            assert body["changes"][0]["target_unit_id"] == "cubic_meter_per_second"
            confirmation = body["confirmation"]
            key = str(uuid4())
            payload = {"confirmation_token": confirmation["token"],
                       "confirmation_phrase": confirmation["required_phrase"]}
            wrong = await client.post(
                "/api/settings/unit-policies", json={**payload,
                "confirmation_phrase": "wrong"}, headers={"Idempotency-Key": key},
            )
            assert wrong.status_code == 409
            assert wrong.json()["error"]["code"] == "admin_confirmation_invalid"
            assert (await client.get("/api/settings")).json() == before
            confirmed = await client.post(
                "/api/settings/unit-policies", json=payload,
                headers={"Idempotency-Key": key},
            )
            assert confirmed.status_code == 201, confirmed.text
            assert confirmed.json()["version"] == 2
            after = (await client.get("/api/settings")).json()
            assert after["revision"] == 1
            assert after["unit_policy"] == {
                key: value for key, value in confirmed.json().items() if key != "created_at"
            }
            replay = await client.post(
                "/api/settings/unit-policies", json=payload,
                headers={"Idempotency-Key": key},
            )
            assert replay.status_code == 201 and replay.json() == confirmed.json()
            different = await client.post(
                "/api/settings/unit-policies", json={**payload,
                "confirmation_phrase": "other"}, headers={"Idempotency-Key": key},
            )
            assert different.status_code == 409
            assert different.json()["error"]["code"] == "idempotency_key_conflict"
            reused = await client.post(
                "/api/settings/unit-policies", json=payload,
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert reused.status_code == 409
            assert reused.json()["error"]["code"] == "admin_confirmation_invalid"
            assert await app.state.unit_policy_service._store.read_unit_policy(3) is None

    asyncio.run(exercise())


def test_preview_rejects_stale_and_invalid_before_writes() -> None:
    async def exercise():
        async with _client() as (_, client):
            cases = (
                ({"base_version": 2, "entries": [_change()]}, 409,
                 "unit_policy_revision_conflict"),
                ({"base_version": 1, "entries": [_change(), _change()]}, 422,
                 "unit_unknown"),
                ({"base_version": 1, "entries": [{**_change(),
                 "target_unit_id": "missing"}]}, 422, "unit_unknown"),
                ({"base_version": 1, "entries": [{"parameter": {
                    "code_system": "usgs", "code": "99999"},
                    "target_unit_id": "cubic_meter_per_second"}]},
                 422, "unit_unknown"),
                ({"base_version": 1, "entries": [{**_change(),
                 "target_unit_id": "degree_celsius"}]},
                 422, "unit_dimension_mismatch"),
            )
            for payload, status, code in cases:
                response = await client.post(
                    "/api/settings/unit-policies/preview", json=payload,
                )
                assert response.status_code == status, response.text
                assert response.json()["error"]["code"] == code
            assert (await client.get("/api/settings")).json()["revision"] == 0

    asyncio.run(exercise())


def test_preview_facts_become_stale_after_settings_change() -> None:
    async def exercise():
        async with _client() as (app, client):
            initial = (await client.get("/api/settings")).json()
            preview = (await client.post(
                "/api/settings/unit-policies/preview",
                json={"base_version": 1, "entries": [_change()]},
            )).json()
            changed = await client.put("/api/settings", json={
                "expected_revision": 0, "defaults": initial["defaults"],
            })
            assert changed.status_code == 200
            confirmation = preview["confirmation"]
            stale = await client.post(
                "/api/settings/unit-policies",
                json={"confirmation_token": confirmation["token"],
                      "confirmation_phrase": confirmation["required_phrase"]},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert stale.status_code == 409
            assert stale.json()["error"]["code"] == "admin_preview_stale"
            assert (await client.get("/api/settings")).json()["unit_policy"]["version"] == 1
            assert await app.state.unit_policy_service._store.read_unit_policy(2) is None

    asyncio.run(exercise())


def test_registry_drift_rejects_confirmation_without_version_write() -> None:
    async def exercise():
        async with _client() as (app, client):
            preview = (await client.post(
                "/api/settings/unit-policies/preview",
                json={"base_version": 1, "entries": [_change()]},
            )).json()
            app.state.unit_policy_service._registry = SimpleNamespace(
                manifest_digest="f" * 64,
            )
            confirmation = preview["confirmation"]
            rejected = await client.post(
                "/api/settings/unit-policies",
                json={"confirmation_token": confirmation["token"],
                      "confirmation_phrase": confirmation["required_phrase"]},
                headers={"Idempotency-Key": str(uuid4())},
            )
            assert rejected.status_code == 409
            assert rejected.json()["error"]["code"] == "unit_policy_revision_conflict"
            assert await app.state.unit_policy_service._store.read_unit_policy(2) is None
            assert (await client.get("/api/settings")).json()["revision"] == 0

    asyncio.run(exercise())

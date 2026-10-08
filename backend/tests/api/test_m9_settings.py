import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from app.application.settings_admin.errors import SettingsUnavailableError
from app.domain.model_catalog import ModelCapability, ModelRole
from app.domain.memory_rag import EmbeddingProfile
from app.infrastructure.config import AppSettings
from app.infrastructure.config.model_catalog import ModelCatalog
from app.infrastructure.persistence.memory_settings import InMemorySettingsRepository
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from tests.m4_support import MODEL_CAPABILITIES, create_request, create_test_app


SECRET = "private-deployment-key-do-not-echo"


@asynccontextmanager
async def _client(*, settings_store=None, client_address=("127.0.0.1", 12345),
                  model_catalog=None, clock=None):
    app = create_test_app(
        AppSettings(_env_file=None, analysis_timezone="UTC",
                    openai_api_key=SecretStr(SECRET)),
        transcript_store=InMemoryTranscriptStore(),
        settings_store=settings_store,
        model_catalog=model_catalog or ModelCatalog(MODEL_CAPABILITIES),
        clock=clock,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=client_address),
            base_url="http://testserver",
        ) as client:
            yield app, client


def test_settings_get_put_conflict_and_safe_catalog(caplog) -> None:
    async def exercise():
        async with _client() as (app, client):
            initial = await client.get("/api/settings")
            assert initial.status_code == 200
            assert initial.headers["cache-control"] == "no-store"
            assert initial.headers["x-request-id"]
            body = initial.json()
            assert body["revision"] == 0 and body["updated_at"] is None
            assert body["defaults"]["analysis_timezone"] == "UTC"
            assert body["unit_policy"]["version"] == 1
            assert len(body["unit_policy"]["entries"]) == 154
            assert {"UTC", "Asia/Shanghai"} <= set(body["catalog"]["iana_timezones"])
            assert all("embedding" not in option["roles"] for option in body["catalog"]["models"])
            assert SECRET not in initial.text
            assert SECRET not in json.dumps(app.openapi())

            replacement = {**body["defaults"], "analysis_timezone": "Asia/Shanghai",
                           "memory_mode": "memory_augmented"}
            changed = await client.put(
                "/api/settings",
                json={"expected_revision": 0, "defaults": replacement},
            )
            assert changed.status_code == 200
            assert changed.headers["cache-control"] == "no-store"
            assert changed.json()["revision"] == 1
            assert changed.json()["defaults"] == replacement
            assert changed.json()["unit_policy"] == body["unit_policy"]
            assert app.state.run_coordinator._runs == {}
            assert app.state.m8_runtime.wakes == 0
            assert SECRET not in changed.text

            stale = await client.put(
                "/api/settings",
                json={"expected_revision": 0, "defaults": body["defaults"]},
            )
            assert stale.status_code == 409
            assert stale.json()["error"]["code"] == "settings_revision_conflict"
            assert stale.json()["current_settings"] == changed.json()
            assert stale.headers["cache-control"] == "no-store"
            assert stale.headers["x-request-id"]
            assert SECRET not in stale.text
            assert (await client.get("/api/settings")).json()["revision"] == 1
            assert SECRET not in caplog.text

    asyncio.run(exercise())


def test_settings_validation_rejects_unknown_roles_profiles_and_values() -> None:
    async def exercise():
        async with _client() as (_, client):
            defaults = (await client.get("/api/settings")).json()["defaults"]
            cases = (
                ({**defaults, "analysis_timezone": "Not/AZone"}, 422),
                ({**defaults, "parser_model": defaults["waterbody_model"]}, 422),
                ({**defaults, "desired_embedding_profile_id": "sha256:" + "b" * 64}, 422),
                ({**defaults, "memory_mode": "unknown"}, 422),
                ({**defaults, "secret_field": SECRET}, 422),
            )
            for candidate, expected in cases:
                response = await client.put(
                    "/api/settings",
                    json={"expected_revision": 0, "defaults": candidate},
                )
                assert response.status_code == expected
                assert response.headers["cache-control"] == "no-store"
                assert SECRET not in response.text
            forbidden_policy = await client.put(
                "/api/settings",
                json={"expected_revision": 0, "defaults": defaults,
                      "unit_policy": {"version": 999}},
            )
            assert forbidden_policy.status_code == 422
            assert (await client.get("/api/settings")).json()["revision"] == 0

    asyncio.run(exercise())


def test_local_mutation_guard_rejects_before_settings_side_effect() -> None:
    async def exercise():
        async with _client() as (_, client):
            defaults = (await client.get("/api/settings")).json()["defaults"]
            payload = {"expected_revision": 0, "defaults": defaults}
            same_origin = await client.put(
                "/api/settings", json=payload,
                headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"},
            )
            assert same_origin.status_code == 200
            payload["expected_revision"] = 1
            rejected = (
                ({"Origin": "http://elsewhere.invalid"}, 403),
                ({"Origin": "null"}, 403),
                ({"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"}, 403),
                ({"Host": "elsewhere.invalid"}, 403),
                ({"Host": "127.0.0.1:9999"}, 403),
                ({"Forwarded": "host=elsewhere.invalid"}, 403),
                ({"X-Forwarded-Host": "elsewhere.invalid"}, 403),
            )
            for headers, status in rejected:
                response = await client.put("/api/settings", json=payload, headers=headers)
                assert response.status_code == status
                assert response.headers["x-request-id"]
                assert response.headers["cache-control"] == "no-store"
            bad_type = await client.put(
                "/api/settings", content=json.dumps(payload),
                headers={"Content-Type": "text/plain"},
            )
            assert bad_type.status_code == 422
            missing_type = await client.put("/api/settings", content=json.dumps(payload))
            assert missing_type.status_code == 422
            missing_create_type = await client.post(
                "/api/analysis/runs", content=json.dumps(create_request()),
            )
            assert missing_create_type.status_code == 422
            form = await client.put("/api/settings", data={"expected_revision": "1"})
            assert form.status_code == 422
            query = await client.put("/api/settings?secret=1", json=payload)
            assert query.status_code == 422
            assert (await client.get("/api/settings?expected_revision=2")).status_code == 422
            assert (await client.post("/api/settings", json=payload)).status_code == 405
            assert (await client.get("/api/settings")).json()["revision"] == 1
        async with _client(client_address=("198.51.100.1", 12345)) as (_, remote):
            response = await remote.put("/api/settings", json=payload)
            assert response.status_code == 403

    asyncio.run(exercise())


def test_database_unavailable_rejects_settings_and_create_before_run() -> None:
    class DownStore:
        async def read_current(self):
            raise SettingsUnavailableError()

        async def read_unit_policy(self, version):
            raise SettingsUnavailableError()

    async def exercise():
        async with _client(settings_store=DownStore()) as (app, client):
            response = await client.get("/api/settings")
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "settings_unavailable"
            created = await client.post("/api/analysis/runs", json=create_request())
            assert created.status_code == 503
            assert app.state.run_coordinator._runs == {}

    asyncio.run(exercise())


def test_bind_host_must_be_loopback() -> None:
    with pytest.raises(Exception, match="loopback"):
        create_test_app(AppSettings(_env_file=None, host="0.0.0.0"))


def test_desired_profile_save_only_changes_desired_identity() -> None:
    second = EmbeddingProfile(
        provider="test-provider", model="embedding-v2",
        output_dimensions=8, max_input_tokens=512,
        semantic_min_similarity=0.78,
    )
    catalog = ModelCatalog((*MODEL_CAPABILITIES, ModelCapability(
        provider=second.provider, model=second.model,
        roles=(ModelRole.EMBEDDING,), embedding_profile=second,
    )))

    async def exercise():
        async with _client(model_catalog=catalog) as (app, client):
            initial = (await client.get("/api/settings")).json()
            assert len(initial["catalog"]["embedding_profiles"]) == 2
            defaults = {**initial["defaults"],
                        "desired_embedding_profile_id": second.profile_id}
            response = await client.put(
                "/api/settings",
                json={"expected_revision": 0, "defaults": defaults},
            )
            assert response.status_code == 200
            assert response.json()["defaults"] == defaults
            assert app.state.run_coordinator._runs == {}
            assert app.state.m8_runtime.wakes == 0
            assert app.state.m8_runtime.started
            assert catalog.snapshot().embedding_profile.profile_id != second.profile_id

    asyncio.run(exercise())


def test_create_uses_one_settings_snapshot_across_concurrent_put() -> None:
    class GatedSettingsStore(InMemorySettingsRepository):
        def __init__(self, registry):
            super().__init__(registry)
            self.block_next = False
            self.read_done = asyncio.Event()
            self.release = asyncio.Event()

        async def read_current(self):
            snapshot = await super().read_current()
            if self.block_next:
                self.block_next = False
                self.read_done.set()
                await self.release.wait()
            return snapshot

    async def exercise():
        from app.infrastructure.config import load_parameter_capability_registry

        store = GatedSettingsStore(load_parameter_capability_registry())
        async with _client(settings_store=store) as (app, client):
            initial = (await client.get("/api/settings")).json()
            before = await client.put("/api/settings", json={
                "expected_revision": 0, "defaults": initial["defaults"],
            })
            assert before.status_code == 200
            store.block_next = True
            first_task = asyncio.create_task(client.post(
                "/api/analysis/runs", json=create_request(
                    "before settings save", memory_mode="memory_augmented",
                    time_resolution="6h",
                ),
            ))
            await asyncio.wait_for(store.read_done.wait(), 5)
            changed_defaults = {**before.json()["defaults"],
                                "analysis_timezone": "Asia/Shanghai"}
            changed = await client.put("/api/settings", json={
                "expected_revision": 1, "defaults": changed_defaults,
            })
            assert changed.status_code == 200
            store.release.set()
            first = await first_task
            second = await client.post(
                "/api/analysis/runs", json=create_request("after settings save"),
            )
            assert first.status_code == second.status_code == 201
            old_configuration = first.json()["configuration"]
            new_configuration = second.json()["configuration"]
            assert old_configuration["analysis_timezone"] == "UTC"
            assert new_configuration["analysis_timezone"] == "Asia/Shanghai"
            assert old_configuration["memory_mode"] == "memory_augmented"
            assert old_configuration["time_resolution_preference"] == "6h"
            assert new_configuration["memory_mode"] == "stateless"
            assert old_configuration["unit_policy_version"] == new_configuration["unit_policy_version"] == 1
            assert old_configuration["unit_registry_digest"] == new_configuration["unit_registry_digest"]
            for created, expected in ((first, old_configuration), (second, new_configuration)):
                run_id = UUID(created.json()["run_id"])
                for _ in range(300):
                    state = await app.state.run_coordinator.get_persistence_info(run_id)
                    if state.state.value != "pending":
                        break
                    await asyncio.sleep(0)
                assert state.state.value == "up_to_date"
                history = await client.get(f"/api/analysis/runs/{run_id}?view=history")
                assert history.status_code == 200
                assert history.json()["run"]["configuration"] == expected

    asyncio.run(asyncio.wait_for(exercise(), 10))


def test_current_timezone_controls_dst_date_range_boundary() -> None:
    class Clock:
        def now(self):
            return datetime(2026, 3, 8, 1, 0, tzinfo=UTC)

    async def exercise():
        async with _client(clock=Clock()) as (_, client):
            initial = (await client.get("/api/settings")).json()
            defaults = {**initial["defaults"],
                        "analysis_timezone": "America/New_York"}
            assert (await client.put("/api/settings", json={
                "expected_revision": 0, "defaults": defaults,
            })).status_code == 200
            accepted = await client.post("/api/analysis/runs", json=create_request(
                date_range={"start_date": "2026-03-14", "end_date": "2026-03-14"},
            ))
            rejected = await client.post("/api/analysis/runs", json=create_request(
                date_range={"start_date": "2026-03-15", "end_date": "2026-03-15"},
            ))
            assert accepted.status_code == 201
            assert accepted.json()["configuration"]["analysis_timezone"] == "America/New_York"
            assert rejected.status_code == 422
            assert rejected.json()["error"]["code"] == "request_validation_failed"

    asyncio.run(exercise())

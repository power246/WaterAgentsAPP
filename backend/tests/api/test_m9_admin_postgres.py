"""Admin routes and deletion receipts through one real M8/PostgreSQL graph."""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, insert, select

from app.infrastructure.runtime import build_m8_runtime
from app.domain import EmbeddingProfile
from app.domain.model_catalog import ModelCapability, ModelRole
from app.infrastructure.config import ModelCatalog
from app.infrastructure.persistence.database import create_database_engine
from app.infrastructure.persistence.schema import (
    analysis_runs, rag_consumption_links, rag_index_jobs,
)
from scripts.test_database import temporary_migrated_schema
from tests.persistence.rag_test_support import embedding_profile, seed_completed_source
from tests.persistence.test_rag_cleanup import _link, _write_case
from tests.infrastructure.embeddings.fake import DeterministicEmbeddingPort
from tests.m4_support import MODEL_CAPABILITIES, create_request, create_test_app


pytestmark = pytest.mark.skipif(
    not os.environ.get("WATERAGENTS_TEST_DB_NAME"),
    reason="dedicated PostgreSQL test database was not configured",
)


@asynccontextmanager
async def _deterministic_m8(settings, **kwargs):
    async with build_m8_runtime(
        settings, embedding_port=DeterministicEmbeddingPort(),
        lease_owner_factory=lambda: "m9-admin-postgres-test", **kwargs,
    ) as runtime:
        yield runtime


async def _terminal(client, operation_id: str):
    for _ in range(200):
        response = await client.get(f"/api/admin/operations/{operation_id}")
        assert response.status_code == 200, response.text
        item = response.json()
        if item["state"] in {"succeeded", "failed", "interrupted"}:
            return item
        await asyncio.sleep(0.02)
    raise AssertionError("Admin operation did not reach a terminal state")


async def _ready(application, run_id: str):
    for _ in range(300):
        snapshot = await application.state.run_coordinator.get_snapshot(UUID(run_id))
        persistence = await application.state.run_coordinator.get_persistence_info(UUID(run_id))
        if snapshot.status.value == "completed" and persistence.state.value == "up_to_date":
            return
        await asyncio.sleep(0.01)
    raise AssertionError("run was not terminal and persisted")


def test_history_admin_commit_delete_and_create_tombstone() -> None:
    async def exercise(settings):
        application = create_test_app(
            settings, m8_runtime_builder=_deterministic_m8,
            shutdown_grace_period=0,
        )
        async with application.router.lifespan_context(application):
            assert application.state.admin_service._maintenance is application.state.m8_runtime.operations
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application),
                base_url="http://testserver",
            ) as client:
                storage = await client.get("/api/admin/storage")
                assert storage.status_code == 200, storage.text
                assert storage.json()["evidence_cache"] == {"state": "not_present"}
                assert (await client.get("/api/admin/rag")).status_code == 200
                body = create_request("M9 Admin history deletion")
                creation_key = str(uuid4())
                created = await client.post(
                    "/api/analysis/runs", json=body,
                    headers={"Idempotency-Key": creation_key},
                )
                assert created.status_code == 201, created.text
                run_id = created.json()["run_id"]
                await _ready(application, run_id)
                delayed_stream = await application.state.run_coordinator.subscribe_run(UUID(run_id))
                preview = await client.post(
                    "/api/admin/history-cleanups/preview",
                    json={"selection": {"kind": "runs", "run_ids": [run_id]},
                          "cascade_consumers": False},
                )
                assert preview.status_code == 200, preview.text
                data = preview.json()
                assert data["delete_run_ids"] == [run_id]
                assert data["estimated"]["retained_idempotency_tombstones"] == 1
                assert data["preserved"] == ["api_keys", "settings", "unit_policy_versions"]
                confirmation = data["confirmation"]
                execute_body = {
                    "confirmation_token": confirmation["token"],
                    "confirmation_phrase": confirmation["required_phrase"],
                }
                operation_key = str(uuid4())
                wrong = await client.post(
                    "/api/admin/history-cleanups",
                    json={**execute_body, "confirmation_phrase": "wrong"},
                    headers={"Idempotency-Key": operation_key},
                )
                assert wrong.status_code == 409
                assert wrong.json()["error"]["code"] == "admin_confirmation_invalid"
                wrong_action = await client.post(
                    "/api/admin/rag/profile-handoffs", json=execute_body,
                    headers={"Idempotency-Key": str(uuid4())},
                )
                assert wrong_action.status_code == 409
                store = application.state.admin_service._store
                read_receipt = store.read_receipt
                both_read = asyncio.Event()
                initial_reads = 0

                async def synchronized_read(key):
                    nonlocal initial_reads
                    receipt = await read_receipt(key)
                    if str(key) == operation_key and receipt is None:
                        initial_reads += 1
                        if initial_reads == 2:
                            both_read.set()
                        await asyncio.wait_for(both_read.wait(), timeout=3)
                    return receipt

                with patch.object(store, "read_receipt", synchronized_read):
                    accepted, duplicate = await asyncio.gather(*(
                        client.post(
                            "/api/admin/history-cleanups", json=execute_body,
                            headers={"Idempotency-Key": operation_key},
                        ) for _ in range(2)
                    ))
                assert accepted.status_code == 202, accepted.text
                assert duplicate.status_code == 202, duplicate.text
                assert duplicate.json()["operation_id"] == accepted.json()["operation_id"]
                operation_id = accepted.json()["operation_id"]
                terminal = await _terminal(client, operation_id)
                assert terminal["state"] == "succeeded", terminal
                assert terminal["safe_result"] == {
                    "kind": "history_cleanup", "deleted_run_ids": [run_id],
                    "retained_idempotency_tombstones": 1,
                }
                assert (await client.get(f"/api/analysis/runs/{run_id}?view=history")).status_code == 404
                assert (await client.get(f"/api/analysis/runs/{run_id}")).status_code == 404
                assert (await client.get(f"/api/analysis/runs/{run_id}/events")).status_code == 404
                with pytest.raises(StopAsyncIteration):
                    await anext(delayed_stream)
                same = await client.post(
                    "/api/analysis/runs", json=body,
                    headers={"Idempotency-Key": creation_key},
                )
                assert same.status_code == 410, same.text
                assert same.json()["error"]["code"] == "idempotency_resource_deleted"
                assert body["query"] not in same.text
                different = await client.post(
                    "/api/analysis/runs", json=create_request("different query"),
                    headers={"Idempotency-Key": creation_key},
                )
                assert different.status_code == 409
                assert different.json()["error"]["code"] == "idempotency_key_conflict"
                replay = await client.post(
                    "/api/admin/history-cleanups", json=execute_body,
                    headers={"Idempotency-Key": operation_key},
                )
                assert replay.status_code == 202
                assert replay.json()["operation_id"] == operation_id
                conflict = await client.post(
                    "/api/admin/history-cleanups",
                    json={**execute_body, "confirmation_phrase": "other"},
                    headers={"Idempotency-Key": operation_key},
                )
                assert conflict.status_code == 409
                assert conflict.json()["error"]["code"] == "idempotency_key_conflict"
                reused = await client.post(
                    "/api/admin/history-cleanups", json=execute_body,
                    headers={"Idempotency-Key": str(uuid4())},
                )
                assert reused.status_code == 409
                page = await client.get("/api/admin/operations?limit=1")
                assert page.status_code == 200
                assert page.json()["items"][0]["operation_id"] == operation_id

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_all_history_preview_is_stale_when_new_run_appears() -> None:
    async def exercise(settings):
        application = create_test_app(
            settings, m8_runtime_builder=_deterministic_m8,
            shutdown_grace_period=0,
        )
        async with application.router.lifespan_context(application):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application),
                base_url="http://testserver",
            ) as client:
                first = await client.post(
                    "/api/analysis/runs", json=create_request("first history"),
                )
                assert first.status_code == 201
                await _ready(application, first.json()["run_id"])
                preview = await client.post(
                    "/api/admin/history-cleanups/preview",
                    json={"selection": {"kind": "all_history"},
                          "cascade_consumers": False},
                )
                assert preview.status_code == 200, preview.text
                assert preview.json()["requested_run_ids"] == [first.json()["run_id"]]
                confirmation = preview.json()["confirmation"]
                second = await client.post(
                    "/api/analysis/runs", json=create_request("second history"),
                )
                assert second.status_code == 201
                attempt = await client.post(
                    "/api/admin/history-cleanups",
                    json={"confirmation_token": confirmation["token"],
                          "confirmation_phrase": confirmation["required_phrase"]},
                    headers={"Idempotency-Key": str(uuid4())},
                )
                assert attempt.status_code == 409
                assert attempt.json()["error"]["code"] == "admin_preview_stale"
                assert (await client.get("/api/admin/operations")).json()["items"] == []
                assert (await client.get(
                    f"/api/analysis/runs/{first.json()['run_id']}?view=history"
                )).status_code == 200

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_profile_handoff_reader_drain_and_index_only_deletion() -> None:
    second_profile = EmbeddingProfile(
        provider="test-provider", model="embedding-v2", output_dimensions=8,
        max_input_tokens=512, semantic_min_similarity=0.78,
    )
    catalog = ModelCatalog((*MODEL_CAPABILITIES, ModelCapability(
        provider=second_profile.provider, model=second_profile.model,
        roles=(ModelRole.EMBEDDING,), embedding_profile=second_profile,
    )))

    async def exercise(settings):
        application = create_test_app(
            settings.model_copy(update={
                "rag_reconciliation_interval_seconds": 0.01,
                "rag_retired_generation_retention_seconds": 0,
            }),
            model_catalog=catalog, m8_runtime_builder=_deterministic_m8,
            shutdown_grace_period=0,
        )
        async with application.router.lifespan_context(application):
            runtime = application.state.m8_runtime
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application),
                base_url="http://testserver",
            ) as client:
                for _ in range(300):
                    old = await runtime.repository.read_active_generation()
                    if old is not None:
                        break
                    await asyncio.sleep(0.01)
                else:
                    raise AssertionError("initial active generation is missing")
                keys_before = (await client.get("/api/keys")).json()
                settings_before = (await client.get("/api/settings")).json()
                changed = await client.put("/api/settings", json={
                    "expected_revision": settings_before["revision"],
                    "defaults": {**settings_before["defaults"],
                                 "desired_embedding_profile_id": second_profile.profile_id},
                })
                assert changed.status_code == 200, changed.text
                assert (await runtime.repository.read_active_generation()).generation_id == old.generation_id
                async with runtime.operations._generations.pin_active_generation() as pinned:
                    assert pinned.generation_id == old.generation_id
                    preview = await client.post("/api/admin/rag/profile-handoffs/preview")
                    assert preview.status_code == 200, preview.text
                    assert preview.json()["handoff"]["desired_profile_digest"] == second_profile.profile_digest
                    confirmation = preview.json()["confirmation"]
                    accepted = await client.post(
                        "/api/admin/rag/profile-handoffs",
                        json={"confirmation_token": confirmation["token"],
                              "confirmation_phrase": confirmation["required_phrase"]},
                        headers={"Idempotency-Key": str(uuid4())},
                    )
                    assert accepted.status_code == 202, accepted.text
                    finished = await _terminal(client, accepted.json()["operation_id"])
                    assert finished["state"] == "succeeded", finished
                    new_id = finished["safe_result"]["active_generation_id"]
                    assert new_id != old.generation_id
                    draining = (await client.get("/api/admin/rag")).json()
                    assert draining["handoff"]["phase"] == "reader_drain"
                    blocked = await client.post(
                        f"/api/admin/rag/generations/{old.generation_id}/deletion-preview"
                    )
                    assert blocked.status_code == 409
                    assert blocked.json()["error"]["code"] == "rag_reader_drain_required"
                ready = (await client.get("/api/admin/rag")).json()
                assert ready["handoff"]["phase"] == "credential_cleanup_ready"
                deletion = await client.post(
                    f"/api/admin/rag/generations/{old.generation_id}/deletion-preview"
                )
                assert deletion.status_code == 200, deletion.text
                confirmation = deletion.json()["confirmation"]
                accepted = await client.post(
                    f"/api/admin/rag/generations/{old.generation_id}/deletions",
                    json={"confirmation_token": confirmation["token"],
                          "confirmation_phrase": confirmation["required_phrase"]},
                    headers={"Idempotency-Key": str(uuid4())},
                )
                assert accepted.status_code == 202, accepted.text
                finished = await _terminal(client, accepted.json()["operation_id"])
                assert finished["state"] == "succeeded", finished
                assert await runtime.repository.read_generation(old.generation_id) is None
                assert (await runtime.repository.read_active_generation()).generation_id == new_id
                assert (await client.get("/api/keys")).json() == keys_before
                assert (await client.get("/api/settings")).json() == changed.json()

        restarted = create_test_app(
            settings.model_copy(update={
                "rag_reconciliation_interval_seconds": 0.01,
                "rag_retired_generation_retention_seconds": 0,
            }),
            model_catalog=catalog, m8_runtime_builder=_deterministic_m8,
        )
        async with restarted.router.lifespan_context(restarted):
            runtime = restarted.state.m8_runtime
            assert runtime.desired_profile == second_profile
            assert (await runtime.repository.read_active_generation()).generation_id == new_id
            assert await runtime.repository.read_generation(old.generation_id) is None

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_saved_desired_profile_does_not_apply_on_restart_without_confirmation() -> None:
    second_profile = EmbeddingProfile(
        provider="test-provider", model="embedding-v2", output_dimensions=8,
        max_input_tokens=512, semantic_min_similarity=0.78,
    )
    catalog = ModelCatalog((*MODEL_CAPABILITIES, ModelCapability(
        provider=second_profile.provider, model=second_profile.model,
        roles=(ModelRole.EMBEDDING,), embedding_profile=second_profile,
    )))

    async def exercise(settings):
        active_id = None
        first = create_test_app(
            settings, model_catalog=catalog, m8_runtime_builder=_deterministic_m8,
        )
        async with first.router.lifespan_context(first):
            for _ in range(300):
                active = await first.state.m8_runtime.repository.read_active_generation()
                if active is not None:
                    active_id = active.generation_id
                    break
                await asyncio.sleep(0.01)
            assert active_id is not None
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=first),
                base_url="http://testserver",
            ) as client:
                before = (await client.get("/api/settings")).json()
                updated = await client.put("/api/settings", json={
                    "expected_revision": before["revision"],
                    "defaults": {**before["defaults"],
                                 "desired_embedding_profile_id": second_profile.profile_id},
                })
                assert updated.status_code == 200
        restarted = create_test_app(
            settings, model_catalog=catalog, m8_runtime_builder=_deterministic_m8,
        )
        async with restarted.router.lifespan_context(restarted):
            runtime = restarted.state.m8_runtime
            assert runtime.desired_profile.profile_id != second_profile.profile_id
            assert (await runtime.repository.read_active_generation()).generation_id == active_id
            assert all(item.profile != second_profile
                       for item in await runtime.repository.list_generations())
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=restarted),
                base_url="http://testserver",
            ) as client:
                status = (await client.get("/api/admin/rag")).json()
                assert status["handoff"]["desired_profile_digest"] == second_profile.profile_digest
                assert status["handoff"]["phase"] == "build_required"
                preview = await client.post("/api/admin/rag/profile-handoffs/preview")
                assert preview.status_code == 200
                assert preview.json()["confirmation"] is not None

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_failed_job_retry_uses_safe_codes_and_keeps_history() -> None:
    async def exercise(settings):
        application = create_test_app(
            settings.model_copy(update={"rag_reconciliation_interval_seconds": 600}),
            m8_runtime_builder=_deterministic_m8,
        )
        async with application.router.lifespan_context(application):
            runtime = application.state.m8_runtime
            for _ in range(300):
                if await runtime.repository.read_active_generation() is not None:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("initial generation was not activated")
            engine = create_database_engine(settings)
            try:
                source = await seed_completed_source(engine)
                building = await runtime.repository.create_building_generation(
                    embedding_profile(dimensions=4), source.completed_at,
                    str(source.result_item_id),
                )
                now = datetime.now(UTC)
                async with engine.begin() as connection:
                    await connection.execute(insert(rag_index_jobs).values(
                        job_id=uuid4(), generation_id=UUID(building.generation_id),
                        source_run_id=source.run_id, source_session_id=source.session_id,
                        source_result_item_id=source.result_item_id,
                        projection_schema_version=1, status="failed", attempts=1,
                        last_safe_error_code="provider_timeout",
                        created_at=now, updated_at=now, completed_at=now,
                    ))
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=application),
                    base_url="http://testserver",
                ) as client:
                    path = f"/api/admin/rag/generations/{building.generation_id}"
                    invalid = await client.post(
                        path + "/failed-jobs/retry-preview",
                        json={"safe_error_codes": ["made_up_code"], "limit": 25},
                    )
                    assert invalid.status_code == 409
                    preview = await client.post(
                        path + "/failed-jobs/retry-preview",
                        json={"safe_error_codes": ["provider_timeout"], "limit": 25},
                    )
                    assert preview.status_code == 200, preview.text
                    assert preview.json()["affected_jobs"] == 1
                    confirmation = preview.json()["confirmation"]
                    accepted = await client.post(
                        path + "/failed-jobs/retries",
                        json={"confirmation_token": confirmation["token"],
                              "confirmation_phrase": confirmation["required_phrase"]},
                        headers={"Idempotency-Key": str(uuid4())},
                    )
                    assert accepted.status_code == 202, accepted.text
                    finished = await _terminal(client, accepted.json()["operation_id"])
                    assert finished["state"] == "succeeded", finished
                    assert finished["safe_result"] == {
                        "kind": "retry_failed_jobs", "retried_jobs": 1,
                    }
                    async with engine.connect() as connection:
                        assert await connection.scalar(select(func.count()).select_from(
                            analysis_runs
                        ).where(analysis_runs.c.run_id == source.run_id)) == 1
            finally:
                await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_history_admin_cascade_repreviews_new_consumption_and_leaves_no_links() -> None:
    async def exercise(settings):
        application = create_test_app(
            settings.model_copy(update={"rag_reconciliation_interval_seconds": 600}),
            m8_runtime_builder=_deterministic_m8,
        )
        async with application.router.lifespan_context(application):
            runtime = application.state.m8_runtime
            for _ in range(300):
                active = await runtime.repository.read_active_generation()
                if active is not None:
                    break
                await asyncio.sleep(0.01)
            else:
                raise AssertionError("initial generation was not activated")
            engine = create_database_engine(settings)
            try:
                source = await seed_completed_source(engine, target_comid="10000001")
                consumer = await seed_completed_source(engine, target_comid="10000002")
                transitive = await seed_completed_source(engine, target_comid="10000003")
                source_case = await _write_case(
                    runtime.repository, source, active.generation_id, active.profile,
                )
                consumer_case = await _write_case(
                    runtime.repository, consumer, active.generation_id, active.profile,
                )
                await _link(runtime.repository, source_case, consumer)
                await _link(runtime.repository, consumer_case, transitive)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=application),
                    base_url="http://testserver",
                ) as client:
                    request_body = {
                        "selection": {"kind": "runs", "run_ids": [str(source.run_id)]},
                        "cascade_consumers": False,
                    }
                    preview = await client.post(
                        "/api/admin/history-cleanups/preview", json=request_body,
                    )
                    assert preview.status_code == 200, preview.text
                    assert preview.json()["cascade_required"] is True
                    assert preview.json()["confirmation"] is None
                    assert preview.json()["delete_order"] == [
                        str(transitive.run_id), str(consumer.run_id), str(source.run_id),
                    ]
                    authorized = await client.post(
                        "/api/admin/history-cleanups/preview",
                        json={**request_body, "cascade_consumers": True},
                    )
                    assert authorized.status_code == 200
                    stale_confirmation = authorized.json()["confirmation"]
                    newest = await seed_completed_source(engine, target_comid="10000004")
                    transitive_case = await _write_case(
                        runtime.repository, transitive, active.generation_id,
                        active.profile,
                    )
                    await _link(runtime.repository, transitive_case, newest)
                    stale = await client.post(
                        "/api/admin/history-cleanups",
                        json={"confirmation_token": stale_confirmation["token"],
                              "confirmation_phrase": stale_confirmation["required_phrase"]},
                        headers={"Idempotency-Key": str(uuid4())},
                    )
                    assert stale.status_code == 409
                    assert stale.json()["error"]["code"] == "admin_preview_stale"
                    refreshed = await client.post(
                        "/api/admin/history-cleanups/preview",
                        json={**request_body, "cascade_consumers": True},
                    )
                    assert refreshed.status_code == 200, refreshed.text
                    assert len(refreshed.json()["delete_run_ids"]) == 4
                    confirmation = refreshed.json()["confirmation"]
                    accepted = await client.post(
                        "/api/admin/history-cleanups",
                        json={"confirmation_token": confirmation["token"],
                              "confirmation_phrase": confirmation["required_phrase"]},
                        headers={"Idempotency-Key": str(uuid4())},
                    )
                    assert accepted.status_code == 202, accepted.text
                    finished = await _terminal(client, accepted.json()["operation_id"])
                    assert finished["state"] == "succeeded", finished
                    assert set(finished["safe_result"]["deleted_run_ids"]) == {
                        str(source.run_id), str(consumer.run_id),
                        str(transitive.run_id), str(newest.run_id),
                    }
                    async with engine.connect() as connection:
                        assert await connection.scalar(select(func.count()).select_from(
                            analysis_runs
                        )) == 0
                        assert await connection.scalar(select(func.count()).select_from(
                            rag_consumption_links
                        )) == 0
            finally:
                await engine.dispose()

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))

import asyncio
import base64
import json
from unittest.mock import patch
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

import pytest

from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from app.domain.guidelines.canonical import digest
from app.ports.errors import PortUnavailableError
from scripts.test_database import temporary_migrated_schema
from tests.guideline_api_support import GUIDELINE_METHODS, guideline_client
from tests.guideline_bundle_support import bundled_version
from tests.guideline_support import content
from tests.m4_support import create_test_app
from tests.persistence.test_guidelines import active_generation, write

BASE = "/api/guidelines"


@pytest.fixture
def settings():
    with temporary_migrated_schema() as selected:
        yield selected


def headers(key=None):
    return {"Idempotency-Key": str(key or uuid4()), "Origin": "http://localhost:8000"}


async def create(client, body=None, key=None):
    response = await client.post(BASE, json={} if body is None else body, headers=headers(key))
    assert response.status_code == 201, response.text
    return response.json()


async def publication(client, document_id, key=None, **overrides):
    document = (await client.get(f"{BASE}/{document_id}")).json()
    from app.domain.guidelines import GuidelineDraftV1
    digest = GuidelineDraftV1.model_validate(document["draft"]).content_digest
    assert digest == document["draft_digest"]
    payload = {"expected_revision": document["revision"], "content_digest": document["draft_digest"], "review_confirmed": True,
               "reviewer": "fixture-reviewer", "review_note": "Synthetic fixture approval; not an actual content review.", **overrides}
    return await client.post(f"{BASE}/{document_id}/publish", json=payload, headers=headers(key)), payload


def test_create_save_publish_immutable_version_poll_failure_and_retry(settings):
    async def exercise():
        async with guideline_client(settings) as (app, client, repo):
            with patch("app.infrastructure.runtime.provider_bundle.RunProviderBundleFactory.build", side_effect=AssertionError("provider dispatch")) as provider:
                created = await create(client)
                document_id = created["document_id"]
                url = f"{BASE}/{document_id}"
                initial = await client.get(url)
                assert initial.json()["state"] == "draft" and initial.json()["revision"] == 1
                assert initial.headers["Cache-Control"] == "no-store" and initial.headers["X-Request-ID"]
                saved = await client.put(url + "/draft", json={"expected_revision": 1, "draft": content().model_dump(mode="json")}, headers=headers())
                assert saved.status_code == 200, saved.text
                accepted, payload = await publication(client, document_id)
                assert accepted.status_code == 202, accepted.text
                receipt = accepted.json()
                operation_url = f"{BASE}/operations/{receipt['operation_id']}"
                assert (await client.get(operation_url)).json()["state"] == "pending"
                detail = (await client.get(url)).json()
                assert detail["state"] == "preparing" and detail["active_version"] is None
                approved = await client.get(f"{url}/versions/{receipt['version_id']}")
                assert approved.json()["review"]["content_digest"] == payload["content_digest"]
                assert (await client.get(BASE, params={"source": "mine", "state": "preparing", "q": "mechanism"})).json()["items"]
                duplicate, _ = await publication(client, document_id)
                assert duplicate.status_code == 409
                generation = await active_generation(repo)
                claim = await repo.claim_operation("fixture-worker")
                await repo.fail_attempt(claim, "guideline_provider_failed", retryable=False)
                failed = (await client.get(operation_url)).json()
                assert failed["state"] == "failed" and failed["safe_error_code"] == "guideline_provider_failed"
                retry = await client.post(operation_url + "/retry", json={"expected_revision": 3}, headers=headers())
                assert retry.status_code == 202, retry.text
                assert retry.json()["version_id"] == receipt["version_id"]
                assert (await client.get(operation_url)).json()["state"] == "superseded"
                claim = await repo.claim_operation("retry-worker")
                version = await repo.read_version(UUID(document_id), UUID(receipt["version_id"]))
                await write(repo, claim, generation, version)
                await repo.activate_version(claim)
                assert (await client.get(url)).json()["state"] == "available"
                assert not provider.called
    asyncio.run(exercise())


@pytest.mark.parametrize("action", ["create", "save", "publish", "enable", "local-edit", "restore", "retry"])
def test_all_mutations_replay_original_receipt_and_conflict_on_changed_body(settings, action):
    async def exercise():
        async with guideline_client(settings) as (_, client, repo):
            key = uuid4()
            if action == "create":
                url, method = BASE, "post"
                body, changed = {"title": "Original"}, {"title": "Different"}
            else:
                created = await create(client, content().model_dump(mode="json"))
                document_id = created["document_id"]
                if action == "save":
                    url, method = f"{BASE}/{document_id}/draft", "put"
                    body = {"expected_revision": 1, "draft": {"title": "Saved"}}
                    changed = {"expected_revision": 1, "draft": {"title": "Changed"}}
                elif action == "enable":
                    url, method = f"{BASE}/{document_id}/set-enabled", "post"
                    body, changed = {"expected_revision": 1, "enabled": False}, {"expected_revision": 1, "enabled": True}
                elif action == "publish":
                    first, body = await publication(client, document_id, key=key)
                    url, method = f"{BASE}/{document_id}/publish", "post"
                    changed = {**body, "review_note": "A different explicit review"}
                elif action in {"local-edit", "restore"}:
                    base = bundled_version()
                    await repo.import_bundled_version(base)
                    root_id = str(base.identity.guideline_id)
                    if action == "restore":
                        generation = await active_generation(repo)
                        claim = await repo.claim_operation("fixture-worker")
                        await write(repo, claim, generation, base)
                        await repo.activate_version(claim)
                        url, method = f"{BASE}/{root_id}/restore-bundled", "post"
                        body = {"expected_revision": 1, "version_id": str(base.version_id)}
                        changed = {**body, "expected_revision": 0}
                    else:
                        url, method = f"{BASE}/{root_id}/local-edit", "post"
                        body = {"expected_revision": 1, "base_version_id": str(base.version_id), "base_content_digest": base.content.content_digest}
                        changed = {**body, "base_content_digest": "sha256:" + "0" * 64}
                else:
                    accepted, _ = await publication(client, document_id)
                    claim = await repo.claim_operation("fixture-worker")
                    await repo.fail_attempt(claim, "guideline_provider_failed", retryable=False)
                    url, method = f"{BASE}/operations/{accepted.json()['operation_id']}/retry", "post"
                    body, changed = {"expected_revision": 2}, {"expected_revision": 1}
            first = await client.request(method, url, json=body, headers=headers(key))
            assert first.status_code in {200, 201, 202}, first.text
            second = await client.request(method, url, json=body, headers=headers(key))
            assert second.status_code == first.status_code and second.json() == first.json()
            conflict = await client.request(method, url, json=changed, headers=headers(key))
            assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"
    asyncio.run(exercise())


@pytest.mark.parametrize("bad", ["unconfirmed", "digest", "no_source", "incomplete", "origin", "version", "review_record"])
def test_invalid_content_or_forged_authority_is_rejected(settings, bad):
    async def exercise():
        async with guideline_client(settings) as (_, client, _):
            if bad in {"origin", "version", "review_record"}:
                field = {"origin": "origin", "version": "version_id", "review_record": "review"}[bad]
                response = await client.post(BASE, json={field: "forged-private-value"}, headers=headers())
            else:
                body = content().model_dump(mode="json")
                if bad == "no_source":
                    body["source_kind"] = "reference_derived"
                elif bad == "incomplete":
                    body = {"title": "Unfinished"}
                created = await create(client, body)
                overrides = {"review_confirmed": False} if bad == "unconfirmed" else ({"content_digest": "sha256:" + "0" * 64} if bad == "digest" else {})
                response, _ = await publication(client, created["document_id"], **overrides)
            assert response.status_code == 422, response.text
            assert "forged-private-value" not in response.text
    asyncio.run(exercise())


def test_bundled_is_read_only_local_override_editable_and_conflict_returns_current(settings):
    async def exercise():
        async with guideline_client(settings) as (_, client, repo):
            base = bundled_version()
            await repo.import_bundled_version(base)
            root = f"{BASE}/{base.identity.guideline_id}"
            denied = await client.put(root + "/draft", json={"expected_revision": 1, "draft": {}}, headers=headers())
            assert denied.status_code == 403
            edited = await client.post(root + "/local-edit", json={"expected_revision": 1, "base_version_id": str(base.version_id),
                "base_content_digest": base.content.content_digest}, headers=headers())
            assert edited.status_code == 201, edited.text
            url = f"{BASE}/{edited.json()['document_id']}/draft"
            assert (await client.put(url, json={"expected_revision": 1, "draft": {"title": "My edit"}}, headers=headers())).status_code == 200
            stale = await client.put(url, json={"expected_revision": 1, "draft": {}}, headers=headers())
            assert stale.status_code == 409
            assert stale.json()["current_document"]["revision"] == 2
            assert stale.json()["current_document"]["draft"]["title"] == "My edit"
    asyncio.run(exercise())


@pytest.mark.parametrize("bad", ["origin", "host", "forwarded", "cross_site", "form", "key_v5", "missing_key", "query", "bad_json"])
def test_security_boundary_headers_and_safe_validation(settings, bad):
    async def exercise():
        async with guideline_client(settings) as (_, client, _):
            selected_headers = headers()
            body = {"title": "private-body-marker"}
            kwargs = {"json": body}
            url = BASE
            if bad == "origin": selected_headers["Origin"] = "https://evil.invalid"
            elif bad == "host": selected_headers["Host"] = "evil.invalid:8000"
            elif bad == "forwarded": selected_headers["X-Forwarded-Host"] = "localhost:8000"
            elif bad == "cross_site": selected_headers["Sec-Fetch-Site"] = "cross-site"
            elif bad == "form": kwargs = {"data": body}
            elif bad == "key_v5": selected_headers["Idempotency-Key"] = str(uuid5(NAMESPACE_URL, "fixture"))
            elif bad == "missing_key": selected_headers.pop("Idempotency-Key")
            elif bad == "query": url += "?unexpected=private-body-marker"
            elif bad == "bad_json":
                kwargs = {"content": '{"title": "private-body-marker"'}
                selected_headers["Content-Type"] = "application/json"
            response = await client.post(url, headers=selected_headers, **kwargs)
            assert response.status_code == (403 if bad in {"origin", "host", "forwarded", "cross_site"} else 422), response.text
            assert response.headers["Cache-Control"] == "no-store" and response.headers["X-Request-ID"]
            assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]
            assert "private-body-marker" not in response.text and "evil.invalid" not in response.text
    asyncio.run(exercise())


@pytest.mark.parametrize("query", ["limit=0", "limit=51", "source=public", "state=approved", "cursor=bad!", "unexpected=1"])
def test_invalid_list_filters_and_cursor_are_bounded(settings, query):
    async def exercise():
        async with guideline_client(settings) as (_, client, _):
            response = await client.get(BASE + "?" + query)
            assert response.status_code == 422 and response.headers["Cache-Control"] == "no-store"
    asyncio.run(exercise())


def test_missing_resources_operations_and_storage_unavailable_are_safe(settings, application_logs):
    async def exercise():
        async with guideline_client(settings) as (_, client, _):
            for suffix in (f"/{uuid4()}", f"/operations/{uuid4()}", f"/{uuid4()}/versions/{uuid4()}"):
                response = await client.get(BASE + suffix)
                assert response.status_code == 404, response.text
                assert response.headers["Cache-Control"] == "no-store"

        class BrokenRepository:
            async def list_documents(self, **kwargs):
                raise PortUnavailableError("secret diagnostic with private-provider-envelope")

        async with guideline_client(settings, repository=BrokenRepository()) as (_, client, _):
            response = await client.get(BASE)
            assert response.status_code == 503 and response.json()["error"]["retryable"]
            assert "secret diagnostic" not in response.text and "private-provider-envelope" not in response.text
    asyncio.run(exercise())
    assert not any("secret diagnostic" in line or "private-provider-envelope" in line for line in application_logs)


def test_publication_http_replay_after_later_edit_keeps_receipt_and_dirty_draft(settings):
    async def exercise():
        async with guideline_client(settings) as (_, client, _):
            created = await create(client, content().model_dump(mode="json"))
            document_id, key = created["document_id"], uuid4()
            accepted, payload = await publication(client, document_id, key=key)
            assert accepted.status_code == 202
            url = f"{BASE}/{document_id}"
            saved = await client.put(url + "/draft", json={"expected_revision": 2, "draft": {"title": "Later unsent edit"}}, headers=headers())
            assert saved.status_code == 200
            replay = await client.post(url + "/publish", json=payload, headers=headers(key))
            assert replay.status_code == 202 and replay.json() == accepted.json()
            current = (await client.get(url)).json()
            assert current["revision"] == 3 and current["draft"]["title"] == "Later unsent edit"
            assert current["pending_version"]["content"]["title"] == content().title
            assert current["has_unpublished_changes"]
    asyncio.run(exercise())


def test_invalid_cursor_payload_types_and_empty_optional_store_are_safe():
    import httpx

    async def exercise():
        application = create_test_app(transcript_store=InMemoryTranscriptStore())
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://localhost:8000") as client:
            selector = digest({"source": "all", "state": None, "q": ""})
            for value in ([], 42, {"v": 1, "after": [], "selector": selector}, {"v": True, "after": str(uuid4()), "selector": selector}):
                cursor = base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
                response = await client.get(BASE, params={"cursor": cursor})
                assert response.status_code == 422 and response.json()["error"]["code"] == "guideline_cursor_invalid"
            response = await client.get(BASE)
            assert response.status_code == 503 and response.headers["Cache-Control"] == "no-store"
    asyncio.run(exercise())


def test_guideline_openapi_routes_and_client_cannot_supply_approved_versions():
    schema = create_test_app(transcript_store=InMemoryTranscriptStore()).openapi()
    assert {path: set(schema["paths"][path]) for path in GUIDELINE_METHODS} == GUIDELINE_METHODS
    for path in GUIDELINE_METHODS:
        for operation in schema["paths"][path].values():
            for response in operation["responses"].values():
                assert "X-Request-ID" in response["headers"]
    publish = schema["components"]["schemas"]["PublishGuidelineRequest"]["properties"]
    assert "review_confirmed" in publish and not {"version_id", "reviewed_at", "origin", "content"}.intersection(publish)

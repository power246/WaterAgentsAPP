import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
from pydantic import SecretStr

from app.application.settings_admin.errors import CredentialStoreUnavailableError
from app.domain.settings_admin import CredentialName, StoredCredential
from app.infrastructure.config import AppSettings
from app.infrastructure.credentials.memory_key_store import InMemoryKeyStore
from app.infrastructure.credentials.os_key_store import OSKeyStore
from app.infrastructure.credentials.tester import ProviderCredentialTester
from app.infrastructure.http import ProviderHttpClient
from app.infrastructure.observability.json_logging import JsonFormatter
from app.infrastructure.observability.rag_metrics import RAGRuntimeMetrics
from app.application.transcripts.redaction import register_private_value
from app.infrastructure.persistence.memory_store import InMemoryTranscriptStore
from tests.m4_support import create_test_app


DEPLOYMENT = "deployment-key-private-1234"
USER = "user-key-private-5678"


class FakeTester:
    def __init__(self):
        self.calls = []
        self.succeeds = True

    async def test(self, name, value):
        self.calls.append((name, value.get_secret_value()))
        return self.succeeds


@asynccontextmanager
async def _client(*, store=None, tester=None):
    store = store or InMemoryKeyStore()
    tester = tester or FakeTester()
    app = create_test_app(
        AppSettings(_env_file=None, openai_api_key=SecretStr(DEPLOYMENT)),
        transcript_store=InMemoryTranscriptStore(),
        key_store=store,
        credential_tester=tester,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            yield app, client, store, tester


def test_key_api_source_precedence_test_and_safe_errors(caplog) -> None:
    async def exercise():
        async with _client() as (app, client, store, tester):
            initial = await client.get("/api/keys")
            assert initial.status_code == 200
            assert initial.headers["cache-control"] == "no-store"
            assert initial.headers["x-request-id"]
            assert [item["key_name"] for item in initial.json()["keys"]] == [
                "openai", "google_geocoding",
            ]
            assert initial.json()["keys"][0]["source"] == "deployment"
            assert initial.json()["keys"][1]["source"] == "none"
            assert DEPLOYMENT not in initial.text
            saved = await client.put("/api/keys/openai", json={"value": USER})
            assert saved.status_code == 200
            assert saved.json()["source"] == "user_store"
            assert saved.json()["masked"] == "••••5678"
            assert saved.json()["can_delete_user_override"]
            assert tester.calls == []
            assert USER not in saved.text
            assert USER not in json.dumps(app.openapi())
            lease = await app.state.credential_service.lease(CredentialName.OPENAI)
            assert lease.value.get_secret_value() == USER
            assert USER not in repr(lease)
            tester.succeeds = False
            failed = await client.post("/api/keys/openai/test")
            assert failed.status_code == 200
            assert failed.json()["status"] == "failed"
            assert failed.json()["safe_error"]["code"] == "credential_rejected"
            assert tester.calls == [(CredentialName.OPENAI, USER)]
            assert USER not in failed.text
            removed = await client.delete("/api/keys/openai")
            assert removed.status_code == 200
            assert removed.json()["source"] == "deployment"
            assert (await app.state.credential_service.lease(
                CredentialName.OPENAI
            )).value.get_secret_value() == DEPLOYMENT
            assert (await client.delete("/api/keys/openai")).status_code == 200
            missing = await client.post("/api/keys/google_geocoding/test")
            assert missing.status_code == 409
            assert missing.json()["error"]["code"] == "credential_missing"
            assert missing.headers["cache-control"] == "no-store"
            unknown = await client.put("/api/keys/arbitrary", json={"value": USER})
            assert unknown.status_code == 404
            assert unknown.json()["error"]["code"] == "credential_not_allowed"
            for invalid in (" ", "a\x00b", "x" * 4097):
                response = await client.put("/api/keys/openai", json={"value": invalid})
                assert response.status_code == 422
                assert response.json()["error"]["code"] == "credential_value_invalid"
                if invalid.strip():
                    assert invalid not in response.text
            assert USER not in caplog.text and DEPLOYMENT not in caplog.text
            assert len(tester.calls) == 1
            store.available = False
            unavailable = await client.get("/api/keys")
            assert unavailable.status_code == 200
            assert unavailable.json()["keys"][0]["source"] == "deployment"
            assert not unavailable.json()["keys"][0]["store_available"]
            save_unavailable = await client.put("/api/keys/openai", json={"value": USER})
            assert save_unavailable.status_code == 503
            assert save_unavailable.json()["error"]["code"] == "credential_store_unavailable"
            assert (await client.delete("/api/keys/openai")).status_code == 503

    asyncio.run(exercise())


def test_registered_secrets_are_redacted_in_log_message_and_traceback():
    async def exercise():
        async with _client() as (app, client, _, _):
            await client.put("/api/keys/openai", json={"value": USER})
            try:
                raise RuntimeError(f"provider rejected {USER[:14]} and {USER}")
            except RuntimeError as error:
                record = logging.LogRecord(
                    "wateragents.test", logging.ERROR, __file__, 1,
                    f"Authorization: Bearer {USER}", (),
                    (type(error), error, error.__traceback__),
                )
            encoded = JsonFormatter().format(record)
            assert USER not in encoded
            assert USER[:14] not in encoded
            assert "Bearer" not in encoded
            assert "[REDACTED]" in encoded
            status = (await client.get("/api/keys")).json()["keys"][0]
            assert status["masked"] == "••••5678"
            assert USER[:14] not in json.dumps(status)
            assert USER not in json.dumps(app.openapi())
            lease = await app.state.credential_service.lease(CredentialName.OPENAI)
            assert USER not in repr(lease)
            metrics = RAGRuntimeMetrics()
            metrics.observe_embedding(
                stage=USER, outcome=USER, latency_seconds=0.01,
            )
            assert USER not in json.dumps(metrics.snapshot())
            register_private_value("shutdown-attempt-private-0002")
            event_record = logging.makeLogRecord({
                "name": "wateragents.test", "levelname": "INFO",
                "msg": "Lifecycle complete.", "event_name": "service_shutdown",
            })
            assert json.loads(JsonFormatter().format(event_record))["event_name"] == "service_shutdown"

    asyncio.run(exercise())


def test_explicit_provider_test_uses_one_fake_request_and_safe_transport_errors():
    async def exercise():
        status = 200
        calls = []

        async def handler(request):
            calls.append((request.url.path, request.headers.get("authorization")))
            if status != 200:
                return httpx.Response(status, json={"error": USER})
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json={"status": "ZERO_RESULTS", "results": []})

        tester = ProviderCredentialTester(
            openai_base_url="https://provider.test/v1",
            http_factory=lambda: ProviderHttpClient(
                timeout_seconds=5, max_response_bytes=16_384,
                retry_delay_seconds=0, attempts=1,
                transport=httpx.MockTransport(handler),
            ),
        )
        async with _client(tester=tester) as (_, client, _, _):
            assert (await client.put("/api/keys/openai", json={"value": USER})).status_code == 200
            assert calls == []
            good = await client.post("/api/keys/openai/test")
            assert good.status_code == 200 and good.json()["status"] == "succeeded"
            assert calls == [("/v1/models", f"Bearer {USER}")]
            status = 401
            rejected = await client.post("/api/keys/openai/test")
            assert rejected.status_code == 200
            assert rejected.json()["safe_error"]["code"] == "credential_rejected"
            assert USER not in rejected.text
            status = 503
            unavailable = await client.post("/api/keys/openai/test")
            assert unavailable.status_code == 503
            assert unavailable.json()["error"]["code"] == "credential_test_failed"
            assert USER not in unavailable.text
            assert len(calls) == 3
            status = 200
            assert (await client.put("/api/keys/google_geocoding", json={"value": USER})).status_code == 200
            google = await client.post("/api/keys/google_geocoding/test")
            assert google.status_code == 200 and google.json()["status"] == "succeeded"
            assert len(calls) == 4
            assert USER not in google.text

    asyncio.run(exercise())


def test_os_key_store_envelope_and_backend_fail_closed_without_system_keychain() -> None:
    class FakeBackend:
        def __init__(self):
            self.values = {}
            self.fail = False

        def get_password(self, service, account):
            if self.fail:
                raise RuntimeError("unsafe OS path and secret")
            return self.values.get((service, account))

        def set_password(self, service, account, value):
            if self.fail:
                raise RuntimeError("unsafe OS path and secret")
            self.values[(service, account)] = value

        def delete_password(self, service, account):
            if self.fail:
                raise RuntimeError("unsafe OS path and secret")
            self.values.pop((service, account), None)

    async def exercise():
        backend = FakeBackend()
        rejected = OSKeyStore(backend=backend)
        assert (await rejected.availability()).value == "unavailable"
        try:
            await rejected.get(CredentialName.OPENAI)
        except CredentialStoreUnavailableError:
            pass
        else:
            raise AssertionError("an unapproved backend was accepted")
        store = OSKeyStore(backend=backend, trusted_test_backend=True)
        assert (await store.availability()).value == "available"
        assert await store.get(CredentialName.OPENAI) is None
        entry = StoredCredential(
            SecretStr(USER), datetime(2026, 10, 8, tzinfo=UTC),
        )
        await store.set(CredentialName.OPENAI, entry)
        encoded = next(iter(backend.values.values()))
        assert json.loads(encoded) == {
            "schema_version": 1, "value": USER,
            "updated_at": "2026-10-08T00:00:00+00:00",
        }
        assert await store.get(CredentialName.OPENAI) == entry
        replacement = StoredCredential(
            SecretStr(DEPLOYMENT), datetime(2026, 10, 8, 1, tzinfo=UTC),
        )
        await store.set(CredentialName.OPENAI, replacement)
        assert await store.get(CredentialName.OPENAI) == replacement
        assert len(backend.values) == 1
        backend.values[next(iter(backend.values))] = json.dumps({
            "schema_version": 2, "value": USER,
            "updated_at": "2026-10-08T00:00:00+00:00",
        })
        try:
            await store.get(CredentialName.OPENAI)
        except CredentialStoreUnavailableError as error:
            assert USER not in str(error)
        else:
            raise AssertionError("an unknown envelope was accepted")
        await store.set(CredentialName.OPENAI, entry)
        backend.fail = True
        try:
            await store.delete(CredentialName.OPENAI)
        except CredentialStoreUnavailableError as error:
            assert "unsafe" not in str(error)
            assert error.__context__ is None
        else:
            raise AssertionError("an OS failure was accepted")
        backend.fail = False
        await store.delete(CredentialName.OPENAI)
        await store.delete(CredentialName.OPENAI)
        assert await store.get(CredentialName.OPENAI) is None
        try:
            await store.get("arbitrary")
        except TypeError:
            pass
        else:
            raise AssertionError("an arbitrary account was accepted")

    asyncio.run(exercise())

"""Offline production composition through HTTP and an isolated PostgreSQL schema."""

import asyncio
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

import app.infrastructure.runtime.m4 as m4_runtime
import app.infrastructure.runtime.m6 as m6_runtime
import app.infrastructure.runtime.m8 as m8_runtime
import app.infrastructure.runtime.m5 as m5_runtime
import app.infrastructure.runtime.provider_bundle as provider_bundle
from app.application.reasoning.model_models import (
    StructuredOutputCompleted, Terminal, UsageReported, WaterbodyFinishReason,
    WaterbodyModelRequest,
)
from app.infrastructure.config import load_parameter_capability_registry
from app.domain import (
    DischargeObservationRecord, EvidenceLocation, Provenance, ProviderQuality,
    WaterObservationRecord,
)
from app.infrastructure.credentials.memory_key_store import InMemoryKeyStore
from app.main import create_app
from scripts.run_sse_smoke import running_server, stream_headers
from scripts.test_database import temporary_migrated_schema
from tests.infrastructure.embeddings.fake import DeterministicEmbeddingPort
from tests.infrastructure.test_m4_runtime import catalog
from tests.application.reasoning.test_runner import AdaptiveWaterbodyModelPort
from tests.application.evidence_tools.fakes import complete_batch
from scripts.m4_workflow_smoke import (
    FixtureGeocoder, FixtureNldi, FixtureParser,
    FixtureProfile, FixtureUsgsMetadata,
)


pytestmark = pytest.mark.skipif(
    not os.environ.get("WATERAGENTS_TEST_DB_NAME"),
    reason="dedicated PostgreSQL test database was not configured",
)


class _ParameterMetadata(FixtureUsgsMetadata):
    async def find_candidates(self, concept):
        del concept
        return ()


class _FixedClock:
    def __init__(self):
        self._now = datetime.now(UTC)

    def now(self):
        return self._now


class _GateParser(FixtureParser):
    def __init__(self, started, release):
        super().__init__(query_fallback=False)
        self._started = started
        self._release = release

    async def generate(self, request, on_delta):
        self._started.set()
        await self._release.wait()
        return await super().generate(request, on_delta)


class _CountingTester:
    def __init__(self):
        self.calls = 0

    async def test(self, name, value):
        del name, value
        self.calls += 1
        return True


class _OfflineModel:
    calls = []
    planning_gate = None
    def __init__(self, *args, **kwargs):
        del args, kwargs
        self._waterbody = AdaptiveWaterbodyModelPort()

    async def stream(self, request):
        type(self).calls.append(getattr(request, "phase", "summary"))
        if isinstance(request, WaterbodyModelRequest):
            if (type(self).planning_gate is not None
                    and getattr(request.phase, "value", request.phase) == "evidence_planning"):
                started, release = type(self).planning_gate
                started.set()
                await release.wait()
            async for event in self._waterbody.stream(request):
                yield event
            return
        summary_input = json.loads(request.messages[0].text.split("\n", 1)[1])
        payload = {
            "schema_version": 1,
            "language": "en",
            "target_comid": summary_input["target_comid"],
            "graph_coverage": summary_input["graph_coverage"],
            "covered_comids": [
                *(card["target_comid"] for card in summary_input["report_cards"]),
                summary_input["target_comid"],
            ],
            "plain_language_summary": "The target has a supported water-quality assessment.",
            "highlights": [],
        }
        yield StructuredOutputCompleted(
            operation_id=request.operation_id, model=request.model,
            output_id=f"output:{request.operation_id}", value=json.dumps(payload),
        )
        yield UsageReported(
            operation_id=request.operation_id, model=request.model,
            input_tokens=10, output_tokens=5,
        )
        yield Terminal(
            operation_id=request.operation_id,
            model=request.model,
            provider_response_id="offline-summary",
            finish_reason=WaterbodyFinishReason.COMPLETED,
        )


class _OfflineObservation:
    def __init__(self, *args, **kwargs):
        del args, kwargs
        self._registry = load_parameter_capability_registry()

    async def collect(self, request, *, deadline=None):
        del deadline
        parameter = request.parameter_keys[0]
        capability = self._registry.get(parameter)
        policy = self._registry.get_channel_policy(parameter, request.provider, request.channel)
        record_type = (DischargeObservationRecord if parameter.code == "00060"
                       else WaterObservationRecord)
        record = record_type(
            provider=request.provider.value, channel=request.channel,
            source_uri="https://example.test/observations",
            retrieved_at=request.request_range.end,
            observed_time=datetime(2026, 10, 8, 12, tzinfo=UTC),
            location=EvidenceLocation(point=None, comid="100", station_id=request.station_id,
                                      distance_to_target_km=0),
            provider_quality=ProviderQuality(approval="approved"),
            raw_unit=capability.canonical_unit,
            provenance=Provenance(
                operation_id=request.operation_id,
                request_identity=f"request:{request.operation_id}",
                page_identity=f"page:{request.operation_id}",
                provider_series_id=request.series_refs[0],
                provider_record_id=f"{request.operation_id}:{parameter.code}",
            ),
            parameter=parameter, value="7", medium=policy.medium_constraint,
            fraction=policy.fraction_constraint, method=policy.accepted_methods[0],
            statistic=policy.accepted_statistics[0],
        )
        return complete_batch(request, (record,))


def _offline_providers(monkeypatch, *, gate_parser=True, gate_waterbody=False):
    registry = load_parameter_capability_registry()
    _OfflineModel.calls = []
    model_started, model_release = asyncio.Event(), asyncio.Event()
    _OfflineModel.planning_gate = (model_started, model_release) if gate_waterbody else None
    started, release = asyncio.Event(), asyncio.Event()
    leases = {"parser": [], "waterbody": [], "google": [], "run_embedding": []}

    def parser(*args, **kwargs):
        del args
        leases["parser"].append(kwargs["api_key"])
        return (_GateParser(started, release) if gate_parser and len(leases["parser"]) == 1
                else FixtureParser(query_fallback=False))

    def geocoder(*args, **kwargs):
        del args
        leases["google"].append(kwargs["api_key"])
        return FixtureGeocoder()

    def waterbody(*args, **kwargs):
        del args
        leases["waterbody"].append(kwargs["api_key"])
        return _OfflineModel()

    def embedding(*args, **kwargs):
        del args
        leases["run_embedding"].append(kwargs["api_key"])
        return DeterministicEmbeddingPort()

    monkeypatch.setattr(m4_runtime, "OpenAIResponsesParserAdapter", parser)
    monkeypatch.setattr(m4_runtime, "GoogleGeocodingAdapter", geocoder)
    monkeypatch.setattr(m4_runtime, "NldiAdapter", lambda *a, **kw: FixtureNldi(FixtureProfile.FULL))
    monkeypatch.setattr(m4_runtime, "UsgsWaterDataAdapter", lambda *a, **kw: _ParameterMetadata(FixtureProfile.FULL, registry))
    monkeypatch.setattr(m6_runtime, "OpenAIResponsesWaterbodyAdapter", waterbody)
    monkeypatch.setattr(m5_runtime, "UsgsObservationAdapter", _OfflineObservation)
    monkeypatch.setattr(m8_runtime, "OpenAIEmbeddingAdapter", lambda *a, **kw: DeterministicEmbeddingPort())
    monkeypatch.setattr(provider_bundle, "OpenAIEmbeddingAdapter", embedding)
    return started, release, model_started, model_release, leases


async def _browser(client, **run_ids):
    browser_env = {
        name: value for name, value in os.environ.items()
        if not name.upper().startswith("WATERAGENTS_")
    }
    browser_env.update({
        "WATERAGENTS_BROWSER_CHANNEL": "chrome",
        "WATERAGENTS_E2E_BASE_URL": str(client.base_url),
        **run_ids,
    })
    browser = await asyncio.to_thread(
        subprocess.run,
        ["npm.cmd" if os.name == "nt" else "npm", "--prefix", "frontend", "run", "test:e2e", "--", "e2e/production.spec.ts"],
        cwd=Path(__file__).resolve().parents[3],
        env=browser_env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=90,
        check=False,
    )
    assert browser.returncode == 0, browser.stdout + browser.stderr


def test_production_routes_settings_run_sse_history_and_restart(monkeypatch):
    started, release, _, _, leases = _offline_providers(monkeypatch)
    with temporary_migrated_schema() as database:
        settings = database.model_copy(update={
            "model_catalog": catalog(),
            "openai_api_key": SecretStr("fixture-openai-only"),
            "google_geocoding_api_key": SecretStr("fixture-google-only"),
            "analysis_timezone": "America/New_York",
            "log_level": "warning",
            "live_state_grace_seconds": 5,
        })
        keys = InMemoryKeyStore()
        tester = _CountingTester()

        def application():
            return create_app(
                settings, clock=_FixedClock(), key_store=keys,
                credential_tester=tester,
                shutdown_grace_period=0,
            )

        async def exercise():
            first = application()
            async with running_server(first) as client:
                root = await client.get("/", headers={"Accept": "text/html"})
                assert root.status_code == 200
                assert root.headers["cache-control"] == "no-cache"
                initial = await client.get("/api/settings")
                assert initial.status_code == 200
                old_defaults = initial.json()["defaults"]
                assert old_defaults["analysis_timezone"] == "America/New_York"
                assert (await client.get("/api/keys")).status_code == 200
                first_openai = "fixture-openai-first"
                first_google = "fixture-google-first"
                second_openai = "fixture-openai-second"
                second_google = "fixture-google-second"
                assert (await client.put("/api/keys/openai", json={"value": first_openai})).status_code == 200
                assert (await client.put("/api/keys/google_geocoding", json={"value": first_google})).status_code == 200
                assert tester.calls == 0
                assert leases["parser"] == []
                body = {
                    "query": "Assess dissolved oxygen for the selected target and dates.",
                    "target": {"comid": "100"},
                    "date_range": {"start_date": "2026-10-08", "end_date": "2026-10-08"},
                    "parser_model": {"provider": "openai", "model": "parser-v1"},
                    "waterbody_model": {"provider": "openai", "model": "waterbody-v1"},
                    "memory_mode": "stateless",
                }
                creation_key = str(uuid4())
                created = await client.post("/api/analysis/runs", json=body,
                                            headers={"Idempotency-Key": creation_key})
                assert created.status_code == 201, created.text
                run_id = created.json()["run_id"]
                assert created.json()["configuration"]["analysis_timezone"] == "America/New_York"
                await asyncio.wait_for(started.wait(), timeout=5)
                assert leases["parser"] == [first_openai]
                assert leases["waterbody"] == [first_openai]
                assert leases["google"] == [first_google]
                assert leases["run_embedding"] == [first_openai]
                changed = await client.put("/api/settings", json={
                    "expected_revision": initial.json()["revision"],
                    "defaults": {**old_defaults, "analysis_timezone": "Asia/Shanghai"},
                })
                assert changed.status_code == 200, changed.text
                assert (await client.put("/api/keys/openai", json={"value": second_openai})).status_code == 200
                assert (await client.put("/api/keys/google_geocoding", json={"value": second_google})).status_code == 200
                assert tester.calls == 0
                assert leases["parser"] == [first_openai]
                release.set()
                for _ in range(300):
                    detail = await client.get(f"/api/analysis/runs/{run_id}")
                    if detail.status_code == 200 and detail.json()["run"]["status"] in {"completed", "failed", "cancelled"}:
                        break
                    await asyncio.sleep(0.02)
                assert detail.status_code == 200, detail.text
                waterbody = next(s for s in detail.json()["sessions"] if s["agent_role"] == "waterbody")
                transcript = await client.get(f"/api/analysis/runs/{run_id}/sessions/{waterbody['session_id']}/transcript")
                assert detail.json()["run"]["status"] == "completed", _OfflineModel.calls
                assert detail.json()["run"]["result_ref"] is not None
                assert waterbody["status"] == "completed"
                assert waterbody["result_ref"] is not None
                assert any(i["content_type"] == "evidence_bundle" for i in transcript.json()["items"])
                fragments = [i for i in transcript.json()["items"] if i["content_type"] == "evidence_fragment"]
                assert any(i["content"]["body"]["applicability"] == "direct_measurement" for i in fragments)
                empty_facts = [i for i in fragments if i["content"]["body"]["collection_status"] == "empty"]
                assert empty_facts
                assert all(UUID(i["correlation"]["operation_id"]) for i in empty_facts)
                inference = next(i for i in transcript.json()["items"] if i["content_type"] == "inference_result")
                assert inference["item_id"] == waterbody["result_ref"]["item_id"]
                assert any(getattr(phase, "value", phase) == "reasoning_batch" for phase in _OfflineModel.calls), (
                    _OfflineModel.calls,
                    [(i["content_type"], i["content"].get("body", {}).get("collection_status"),
                      i["content"].get("body", {}).get("execution_status"))
                     for i in transcript.json()["items"] if i["content_type"] in {"evidence_fragment", "evidence_tool_result"}],
                )
                content = await client.get(f"/api/analysis/runs/{run_id}/transcript/{inference['item_id']}/content")
                assert content.status_code == 200
                assert "overall_assessment" in content.json()["text"]
                summary = next(s for s in detail.json()["sessions"] if s["agent_role"] == "run_summarizer")
                assert summary["status"] == "completed"
                assert summary["result_ref"] is not None
                assert detail.json()["run"]["configuration"]["analysis_timezone"] == "America/New_York"
                async with client.stream("GET", f"/api/analysis/runs/{run_id}/events") as response:
                    stream_headers(response)
                    assert "event: run_snapshot" in (await response.aread()).decode()
                async with client.stream("GET", f"/api/analysis/runs/{run_id}/sessions/{waterbody['session_id']}/events") as response:
                    stream_headers(response)
                    assert "event: transcript_item" in (await response.aread()).decode()
                model_calls = len(_OfflineModel.calls)
                retry = await client.post(f"/api/analysis/runs/{run_id}/persistence/retry")
                assert retry.status_code == 200
                assert retry.json()["persistence"]["state"] == "up_to_date"
                assert len(_OfflineModel.calls) == model_calls
                history = await client.get(f"/api/analysis/runs/{run_id}?view=history")
                assert history.status_code == 200, history.text
                assert history.json()["run"]["configuration"]["analysis_timezone"] == "America/New_York"
                assert history.json()["run"]["result_ref"] == detail.json()["run"]["result_ref"]
                if os.environ.get("WATERAGENTS_M9_BROWSER_E2E") == "1":
                    await _browser(client, WATERAGENTS_E2E_RUN_ID=run_id)
                checked = await client.post("/api/keys/openai/test")
                assert checked.status_code == 200
                assert checked.json()["status"] == "succeeded"
                assert tester.calls == 1
                second_creation_key = str(uuid4())
                second = await client.post(
                    "/api/analysis/runs", json=body,
                    headers={"Idempotency-Key": second_creation_key},
                )
                assert second.status_code == 201, second.text
                second_run_id = second.json()["run_id"]
                assert second.json()["configuration"]["analysis_timezone"] == "Asia/Shanghai"
                assert leases["parser"] == [first_openai, second_openai]
                assert leases["waterbody"] == [first_openai, second_openai]
                assert leases["google"] == [first_google, second_google]
                assert leases["run_embedding"] == [first_openai, second_openai]
                for _ in range(300):
                    second_detail = await client.get(f"/api/analysis/runs/{second_run_id}")
                    if second_detail.status_code == 200 and second_detail.json()["run"]["status"] in {"completed", "failed", "cancelled"}:
                        break
                    await asyncio.sleep(0.02)
                assert second_detail.json()["run"]["status"] == "completed"
                assert second_detail.json()["run"]["configuration"]["analysis_timezone"] == "Asia/Shanghai"
                assert (await client.get("/api/admin/storage")).status_code == 200
            restarted = application()
            async with running_server(restarted) as client:
                current = await client.get("/api/settings")
                assert current.status_code == 200
                assert current.json()["defaults"]["analysis_timezone"] == "Asia/Shanghai"
                history = await client.get(f"/api/analysis/runs/{run_id}?view=history")
                assert history.status_code == 200, history.text
                assert history.json()["run"]["configuration"]["analysis_timezone"] == "America/New_York"
                second_history = await client.get(f"/api/analysis/runs/{second_run_id}?view=history")
                assert second_history.status_code == 200
                assert second_history.json()["run"]["configuration"]["analysis_timezone"] == "Asia/Shanghai"
                assert (await client.get("/", headers={"Accept": "text/html"})).status_code == 200
                assert (await client.get("/api/keys")).json()["keys"][0]["source"] == "user_store"
                assert (await client.get("/api/admin/rag")).status_code == 200
                preview = await client.post("/api/admin/history-cleanups/preview", json={
                    "selection": {"kind": "runs", "run_ids": [second_run_id]},
                    "cascade_consumers": False,
                })
                assert preview.status_code == 200, preview.text
                assert preview.json()["delete_run_ids"] == [second_run_id]
                confirmation = preview.json()["confirmation"]
                accepted = await client.post(
                    "/api/admin/history-cleanups",
                    json={
                        "confirmation_token": confirmation["token"],
                        "confirmation_phrase": confirmation["required_phrase"],
                    },
                    headers={"Idempotency-Key": str(uuid4())},
                )
                assert accepted.status_code == 202, accepted.text
                for _ in range(300):
                    operation = await client.get(f"/api/admin/operations/{accepted.json()['operation_id']}")
                    if operation.status_code == 200 and operation.json()["state"] in {"succeeded", "failed"}:
                        break
                    await asyncio.sleep(0.02)
                assert operation.json()["state"] == "succeeded", operation.text
                assert (await client.get(f"/api/analysis/runs/{second_run_id}?view=history")).status_code == 404
                tombstone = await client.post(
                    "/api/analysis/runs", json=body,
                    headers={"Idempotency-Key": second_creation_key},
                )
                assert tombstone.status_code == 410
                assert tombstone.json()["error"]["code"] == "idempotency_resource_deleted"
                assert (await client.get("/api/settings")).json()["defaults"]["analysis_timezone"] == "Asia/Shanghai"
                assert (await client.get("/api/keys")).json()["keys"][0]["source"] == "user_store"
                assert (await client.get(f"/api/analysis/runs/{run_id}?view=history")).status_code == 200
                if os.environ.get("WATERAGENTS_M9_BROWSER_E2E") == "1":
                    await _browser(
                        client, WATERAGENTS_E2E_RUN_ID=run_id,
                        WATERAGENTS_E2E_OPERATION_ID=accepted.json()["operation_id"],
                        WATERAGENTS_E2E_DELETED_RUN_ID=second_run_id,
                    )

        asyncio.run(asyncio.wait_for(exercise(), timeout=45))


def test_production_cancel_converges_real_session_without_result(monkeypatch):
    _, _, model_started, model_release, _ = _offline_providers(
        monkeypatch, gate_parser=False, gate_waterbody=True,
    )
    with temporary_migrated_schema() as database:
        settings = database.model_copy(update={
            "model_catalog": catalog(),
            "openai_api_key": SecretStr("fixture-openai-cancel"),
            "google_geocoding_api_key": SecretStr("fixture-google-cancel"),
            "log_level": "warning",
            "live_state_grace_seconds": 5,
        })
        application = create_app(
            settings, clock=_FixedClock(), key_store=InMemoryKeyStore(),
            credential_tester=_CountingTester(), shutdown_grace_period=0,
        )

        async def exercise():
            async with running_server(application) as client:
                created = await client.post("/api/analysis/runs", json={
                    "query": "Assess dissolved oxygen for the selected target and dates.",
                    "target": {"comid": "100"},
                    "date_range": {"start_date": "2026-10-08", "end_date": "2026-10-08"},
                    "parser_model": {"provider": "openai", "model": "parser-v1"},
                    "waterbody_model": {"provider": "openai", "model": "waterbody-v1"},
                    "memory_mode": "stateless",
                }, headers={"Idempotency-Key": str(uuid4())})
                assert created.status_code == 201, created.text
                run_id = created.json()["run_id"]
                try:
                    await asyncio.wait_for(model_started.wait(), timeout=20)
                    cancelled = await client.post(f"/api/analysis/runs/{run_id}/cancel")
                    assert cancelled.status_code == 200, cancelled.text
                finally:
                    model_release.set()
                for _ in range(300):
                    detail = await client.get(f"/api/analysis/runs/{run_id}")
                    if detail.status_code == 200 and detail.json()["run"]["status"] == "cancelled":
                        break
                    await asyncio.sleep(0.02)
                assert detail.json()["run"]["status"] == "cancelled"
                waterbodies = {
                    session["target_comid"]: session["status"]
                    for session in detail.json()["sessions"]
                    if session["agent_role"] == "waterbody"
                }
                assert waterbodies["100"] == "cancelled"
                assert set(waterbodies.values()) == {"cancelled"}, waterbodies
                assert detail.json()["run"]["result_ref"] is None
                async with client.stream("GET", f"/api/analysis/runs/{run_id}/events") as response:
                    stream_headers(response)
                    assert '"status":"cancelled"' in (await response.aread()).decode()
                history = await client.get(f"/api/analysis/runs/{run_id}?view=history")
                assert history.status_code == 200
                assert history.json()["run"]["status"] == "cancelled"
                if os.environ.get("WATERAGENTS_M9_BROWSER_E2E") == "1":
                    await _browser(client, WATERAGENTS_E2E_CANCEL_RUN_ID=run_id)

        asyncio.run(asyncio.wait_for(exercise(), timeout=60))

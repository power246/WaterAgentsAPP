"""Opt-in browser integration: real HTTP/PostgreSQL, deterministic test-only providers."""
import asyncio
import json
import os
import subprocess
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from app.application.guidelines.retrieval import GuidelineRunContext
from app.application.reasoning import WaterbodyAgentRunStatus
from app.application.transcripts.read_models import ReadView
from app.domain.guidelines import GuidelinePolicy
from app.infrastructure.config import load_parameter_capability_registry
from app.infrastructure.runtime.guidelines import GuidelineRuntime
from scripts.run_sse_smoke import running_server
from scripts.test_database import temporary_migrated_schema
from tests.application.guidelines.test_transcript import AdvisoryModel
from tests.application.guidelines.test_worker import Embedding
from tests.guideline_support import profile
from tests.m6_reasoning_support import cancel_run, create_reasoning_test_app, prepare_waterbody
from tests.persistence.test_m6_reasoning_persistence import _wait_persisted


@pytest.mark.skipif(os.environ.get('WATERAGENTS_GUIDELINE_BROWSER_E2E') != '1',
                    reason='Opt-in: requires built frontend and installed Chrome/Playwright')
def test_browser_management_to_saved_run_and_history(monkeypatch, tmp_path):
    selected = profile(max_input_tokens=8192)
    embedding = Embedding()

    class OfflineGuidelines(GuidelineRuntime):
        def __init__(self, **options):
            super().__init__(**options)
            self.desired_profile = selected
            self.safe_error_code = None

        async def embedding_for_attempt(self):
            return embedding

    monkeypatch.setattr('app.main.GuidelineRuntime', OfflineGuidelines)

    async def exercise(settings):
        application, workflow, water = create_reasoning_test_app(monkeypatch,
            model_port=AdvisoryModel(), transcript_store=None, settings=settings.model_copy(update={
                'memory_enabled': False, 'log_level': 'warning', 'transcript_inline_max_bytes': 768,
                'transcript_preview_bytes': 80, 'db_operation_timeout_seconds': 30,
            }))
        manifest = tmp_path / 'browser-fixture.json'
        async with running_server(application) as client:
            browser_env = {key: value for key, value in os.environ.items() if not key.upper().startswith('WATERAGENTS_')}
            browser_env.update(WATERAGENTS_E2E_BASE_URL=str(client.base_url),
                WATERAGENTS_BROWSER_CHANNEL='chrome', WATERAGENTS_GUIDELINE_BROWSER_FIXTURE=str(manifest))
            browser = asyncio.create_task(asyncio.to_thread(subprocess.run,
                ['node', 'node_modules/@playwright/test/cli.js', 'test', 'e2e/guidelines.spec.ts', '--grep', 'real backend'],
                cwd=Path(__file__).resolve().parents[3] / 'frontend', env=browser_env,
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=150, check=False))
            try:
                service = application.state.guideline_service
                for _ in range(400):
                    if browser.done():
                        completed = await browser
                        pytest.fail(completed.stdout + completed.stderr)
                    documents, _ = await service.list(source='mine')
                    created = next((d for d in documents if d.title == 'M10 actual API original' and d.state == 'available'), None)
                    if created:
                        break
                    await asyncio.sleep(.1)
                else:
                    raise AssertionError('Browser did not publish an available guideline')
                repository = application.state.guideline_runtime._repository
                prepared = await prepare_waterbody(application, workflow, water, query='M10 browser deterministic mechanism comparison')
                runtime = application.state.m6_runtime.runner
                run = await GuidelineRunContext.prepare(prepared.run_id, repository=repository, embedding=embedding,
                    profile=selected, registry=load_parameter_capability_registry(), policy=GuidelinePolicy(),
                    clock=runtime._clock, cancellation=prepared.task.task.cancellation_token)
                assert run.pin.status == 'ready'
                session = run.session(prepared.task, deadline=runtime._clock.now() + timedelta(minutes=2))
                try:
                    task = replace(prepared.task, task=replace(prepared.task.task, guidelines=session))
                    outcome = await runtime.build_runner(task).run(task, deadline=runtime.session_deadline() - timedelta(seconds=1))
                    assert outcome.status is WaterbodyAgentRunStatus.COMPLETED, outcome.error
                    await _wait_persisted(application, prepared.run_id)
                    await cancel_run(application, prepared.run_id)
                    await _wait_persisted(application, prepared.run_id)
                    manifest.write_text(json.dumps({'run_id': str(prepared.run_id), 'session_id': str(prepared.session_id),
                        'document_id': str(created.identity.guideline_id)}), encoding='utf-8')
                    completed = await browser
                    assert completed.returncode == 0, completed.stdout + completed.stderr
                    changed = await service.get(created.identity.guideline_id)
                    assert changed.active_version.content.title == 'M10 updated library B'
                    assert changed.active_version.version_number == 2
                    history = await application.state.transcript_reader.read_registered_run(prepared.run_id, ReadView.HISTORY)
                    deliveries = [entry for entry in history.items if entry.item.content_type == 'guideline_delivery']
                    assert deliveries and all(not entry.parse_error for entry in deliveries)
                    assert all(v.version.content.title == 'M10 actual API original' for entry in deliveries for v in entry.body.entries)
                    assert any(entry.item.content_type == 'result_guideline_usage' for entry in history.items)
                finally:
                    await session.aclose()
            finally:
                if not browser.done():
                    # subprocess.run owns and bounds the browser; let it report diagnostics before server teardown.
                    await browser

    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))

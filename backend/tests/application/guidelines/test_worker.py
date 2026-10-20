import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from app.application.guidelines.authoring import GuidelineAuthoringService
from app.application.guidelines.worker import GuidelinePreparationWorker
from app.domain.guidelines import GuidelineDraftV1, GuidelineEmbeddingBatch, GuidelinePolicy
from app.domain.guidelines.mutation_bodies import action_body
from app.domain.memory_rag.embedding_vector import EmbeddingVector
from app.infrastructure.config import load_parameter_capability_registry
from app.infrastructure.persistence.guideline_repository import PostgresGuidelineRepository
from app.ports import PortAuthenticationError, PortUnavailableError
from scripts.test_database import temporary_migrated_schema
from tests.guideline_bundle_support import bundled_version
from tests.guideline_support import PH, content, profile
from tests.persistence.test_guidelines import mutation
from app.domain.guidelines.operation_kind import GuidelineOperationKind as Kind

REGISTRY = load_parameter_capability_registry()


class Clock:
    def __init__(self): self.value = datetime.now(timezone.utc)
    def now(self): return self.value
    def advance(self, seconds): self.value += timedelta(seconds=seconds)


class Embedding:
    def __init__(self, callback=None):
        self.calls = []
        self.leases = 0
        self.callback = callback

    async def factory(self):
        self.leases += 1
        return self

    async def embed(self, selected, inputs, deadline, token):
        self.calls.append((selected, inputs, deadline))
        if self.callback:
            returned = await self.callback(selected, inputs, token)
            if returned is not None: return returned
        return GuidelineEmbeddingBatch(profile=selected, vectors=(EmbeddingVector(values=(1.,) + (0.,) * (selected.output_dimensions - 1)),))


@pytest.fixture
def settings():
    with temporary_migrated_schema() as selected: yield selected


@asynccontextmanager
async def system(settings):
    clock = Clock()
    repository = PostgresGuidelineRepository(settings, clock=clock)
    service = GuidelineAuthoringService(repository, REGISTRY, clock)
    try: yield repository, service, clock
    finally: await repository.aclose()


def worker(repository, embedding, selected=None, **policy):
    return GuidelinePreparationWorker(repository, REGISTRY, selected or profile(max_input_tokens=8192),
        GuidelinePolicy(worker_max_concurrency=1, **policy), embedding.factory)


async def publish(service, document_id):
    document = await service.get(document_id)
    return await service.publish(document_id, expected_revision=document.revision, content_digest=document.draft_digest,
        review_confirmed=True, reviewer="fixture-reviewer", review_note="Synthetic fixture approval; not actual content review.", key=uuid4())


async def custom(service, title="Approved A"):
    created = await service.create(GuidelineDraftV1.model_validate(content(title=title).model_dump()), uuid4())
    return await publish(service, created.document_id)


async def edit(service, document_id, title):
    document = await service.get(document_id)
    await service.save(document_id, GuidelineDraftV1.model_validate(content(title=title).model_dump()), document.revision, uuid4())


async def ready(repository, service):
    embedding = Embedding()
    selected = worker(repository, embedding)
    receipt = await custom(service)
    await selected.run_once()
    return receipt, selected


async def finish_generation(selected, repository):
    for _ in range(8):
        await selected.run_once()
        if (await repository.read_catalog()).profile == selected.profile: return
    raise AssertionError("generation did not finish")


def test_first_generation_and_approved_a_inflight_do_not_publish_later_draft_b(settings):
    async def exercise():
        async with system(settings) as (repository, service, _):
            entered, release = asyncio.Event(), asyncio.Event()
            async def block(selected, inputs, token):
                entered.set()
                await release.wait()
            embedding = Embedding(block)
            selected = worker(repository, embedding)
            receipt = await custom(service)
            assert (await repository.read_catalog()).generation_id is None
            task = asyncio.create_task(selected.run_once())
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(edit(service, receipt.document_id, "Unapproved B"), 2)
            assert (await service.get_operation(receipt.operation_id)).state.value == "running"
            release.set()
            await task
            document = await service.get(receipt.document_id)
            assert document.active_version.content.title == "Approved A" and document.draft.title == "Unapproved B"
            assert document.has_unpublished_changes and document.state == "available"
            assert (await service.get_operation(receipt.operation_id)).state.value == "succeeded"
            assert len(embedding.calls) == 1 and embedding.calls[0][1][0].model_view.title == "Approved A"
            assert (await repository.read_catalog()).profile == selected.profile
    asyncio.run(exercise())


@pytest.mark.parametrize("action", ["disable", "restore", "new_review", "new_builtin"])
def test_late_override_preparation_respects_new_family_intent(settings, action):
    async def exercise():
        async with system(settings) as (repository, service, clock):
            base = bundled_version()
            await repository.import_bundled_version(base)
            await worker(repository, Embedding()).run_once()
            root = await service.get(base.identity.guideline_id)
            created = await service.local_edit(root.identity.guideline_id, base.version_id, base.content.content_digest, root.revision, uuid4())
            receipt = await publish(service, created.document_id)
            entered, release = asyncio.Event(), asyncio.Event()
            async def block(selected, inputs, token):
                entered.set()
                await release.wait()
            selected = worker(repository, Embedding(block))
            task = asyncio.create_task(selected.run_once())
            await asyncio.wait_for(entered.wait(), 2)
            root = await service.get(base.identity.guideline_id)
            fresh = None
            if action == "disable": await service.set_enabled(root.identity.guideline_id, False, root.revision, uuid4())
            elif action == "restore": await service.restore(root.identity.guideline_id, base.version_id, root.revision, uuid4())
            elif action == "new_review":
                clock.advance(61)
                replacement = await repository.claim_operation("recovered-worker")
                await repository.fail_attempt(replacement, "guideline_embedding_timeout", retryable=False)
                await edit(service, created.document_id, "Fresh reviewed C")
                fresh = await publish(service, created.document_id)
            else: await repository.import_bundled_version(bundled_version(2))
            release.set()
            await task
            operation = await service.get_operation(receipt.operation_id)
            if action == "new_builtin":
                assert operation.state.value == "succeeded"
                assert (await service.get(created.document_id)).active_version_id == receipt.version_id
            else:
                assert operation.state.value == "superseded"
                assert (await service.get(created.document_id)).active_version_id is None
            catalog = await repository.read_catalog()
            effective = await repository.read_effective_versions(catalog, (PH,))
            if action == "disable": assert not effective
            elif action == "restore": assert effective == (base,)
            elif action == "new_review":
                await worker(repository, Embedding()).run_once()
                assert (await service.get(created.document_id)).active_version_id == fresh.version_id
    asyncio.run(exercise())


@pytest.mark.parametrize("action", ["publication", "disable", "restore"])
def test_generation_rebuild_reconciles_catalog_changes_before_atomic_switch(settings, action):
    async def exercise():
        async with system(settings) as (repository, service, _):
            base = bundled_version()
            await repository.import_bundled_version(base)
            old = worker(repository, Embedding())
            await old.run_once()
            root = await service.get(base.identity.guideline_id)
            local = await service.local_edit(root.identity.guideline_id, base.version_id, base.content.content_digest, root.revision, uuid4())
            first = await publish(service, local.document_id)
            await old.run_once()
            pinned = await repository.read_catalog()
            entered, release = asyncio.Event(), asyncio.Event()
            async def block(selected, inputs, token):
                entered.set()
                await release.wait()
            embedding = Embedding(block)
            next_profile = profile(model="fixture-next", output_dimensions=4, max_input_tokens=8192)
            rebuilding = worker(repository, embedding, next_profile)
            task = asyncio.create_task(rebuilding.run_once())
            await asyncio.wait_for(entered.wait(), 2)
            root = await service.get(base.identity.guideline_id)
            if action == "publication":
                await edit(service, local.document_id, "Approved after rebuild snapshot")
                latest = await publish(service, local.document_id)
                await old.run_once()
                expected = latest.version_id
            elif action == "disable":
                await service.set_enabled(root.identity.guideline_id, False, root.revision, uuid4())
                expected = None
            else:
                await service.restore(root.identity.guideline_id, base.version_id, root.revision, uuid4())
                expected = base.version_id
            assert (await repository.read_catalog()).generation_id == pinned.generation_id
            release.set()
            await task
            embedding.callback = None
            await finish_generation(rebuilding, repository)
            catalog = await repository.read_catalog()
            assert catalog.generation_id != pinned.generation_id and catalog.profile == next_profile
            effective = await repository.read_effective_versions(catalog, (PH,))
            assert (effective[0].version_id if effective else None) == expected
            assert (await repository.read_effective_versions(pinned, (PH,)))[0].version_id == first.version_id
            async with repository._engine.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM guideline_index_generations WHERE state = 'active'")) == 1
                assert not await connection.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM guideline_documents d WHERE d.active_version_id IS NOT NULL AND NOT EXISTS "
                    "(SELECT 1 FROM guideline_embeddings b WHERE b.version_id = d.active_version_id AND b.generation_id = :gen))"
                ), {"gen": catalog.generation_id})
            if action == "disable":
                root = await service.get(base.identity.guideline_id)
                await service.set_enabled(root.identity.guideline_id, True, root.revision, uuid4())
                assert (await repository.read_effective_versions(await repository.read_catalog(), (PH,)))[0].version_id == first.version_id
    asyncio.run(exercise())


def test_generation_switch_during_publication_recomputes_current_profile_with_same_attempt_lease(settings):
    async def exercise():
        async with system(settings) as (repository, service, _):
            entered, release = asyncio.Event(), asyncio.Event()
            async def block_first(selected, inputs, token):
                if selected.model == "fixture-embedding":
                    entered.set()
                    await release.wait()
            embedding = Embedding(block_first)
            selected = worker(repository, embedding)
            receipt = await custom(service)
            task = asyncio.create_task(selected.run_once())
            await asyncio.wait_for(entered.wait(), 2)
            selected.profile = profile(model="fixture-next", output_dimensions=4, max_input_tokens=8192)
            await selected.reconcile_generation()
            current = await repository.read_catalog()
            assert current.profile == selected.profile
            release.set()
            await task
            assert [item[0].model for item in embedding.calls] == ["fixture-embedding", "fixture-next"]
            assert embedding.leases == 1
            assert (await service.get_operation(receipt.operation_id)).state.value == "succeeded"
            async with repository._engine.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM guideline_embeddings")) == 1
            assert (await repository.read_embedding(current.generation_id, receipt.version_id)).values == (1., 0., 0., 0.)
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["transient", "credentials"])
def test_failed_preparation_keeps_old_content_and_manual_retry_reuses_approved_version(settings, failure):
    async def exercise():
        async with system(settings) as (repository, service, clock):
            first, selected = await ready(repository, service)
            await edit(service, first.document_id, "Approved B")
            second = await publish(service, first.document_id)
            async def fail(selected, inputs, token):
                if failure == "credentials": raise PortAuthenticationError("private-key diagnostic")
                raise PortUnavailableError("private-provider-envelope")
            embedding = Embedding(fail)
            selected = worker(repository, embedding)
            for attempt in range(1 if failure == "credentials" else 3):
                await selected.run_once()
                if failure == "transient" and attempt < 2:
                    assert await selected.run_once() == 0
                    clock.advance(2 if attempt == 0 else 10)
            failed = await service.get_operation(second.operation_id)
            assert failed.state.value == "failed" and "private" not in failed.model_dump_json()
            assert (await service.get(first.document_id)).active_version_id == first.version_id
            assert await selected.run_once() == 0
            retry = await service.retry(second.operation_id, (await service.get(first.document_id)).revision, uuid4())
            assert retry.version_id == second.version_id
            embedding.callback = None
            await selected.run_once()
            assert (await service.get(first.document_id)).active_version_id == second.version_id
            async with repository._engine.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM guideline_versions")) == 2
    asyncio.run(exercise())


def test_expired_inflight_attempt_cannot_overwrite_restarted_worker_result(settings):
    async def exercise():
        async with system(settings) as (repository, service, clock):
            entered, release = asyncio.Event(), asyncio.Event()
            async def old(selected, inputs, token):
                entered.set()
                await release.wait()
            original = worker(repository, Embedding(old))
            receipt = await custom(service)
            task = asyncio.create_task(original.run_once())
            await asyncio.wait_for(entered.wait(), 2)
            clock.advance(61)
            recovered_repo = PostgresGuidelineRepository(settings, clock=clock)
            async def different(selected, inputs, token):
                return GuidelineEmbeddingBatch(profile=selected, vectors=(EmbeddingVector(values=(0., 1., 0.)),))
            try:
                await worker(recovered_repo, Embedding(different)).run_once()
                release.set()
                await task
                operation = await service.get_operation(receipt.operation_id)
                assert operation.state.value == "succeeded" and operation.attempts == 2
                catalog = await repository.read_catalog()
                assert (await repository.read_embedding(catalog.generation_id, receipt.version_id)).values == (0., 1., 0.)
            finally: await recovered_repo.aclose()
    asyncio.run(exercise())


def test_atomic_completion_rolls_back_vector_pointer_catalog_and_receipt_on_failure(settings, monkeypatch):
    async def exercise():
        async with system(settings) as (repository, service, _):
            previous, selected = await ready(repository, service)
            old_catalog = await repository.read_catalog()
            await edit(service, previous.document_id, "Approved replacement")
            receipt = await publish(service, previous.document_id)
            original = repository._refresh_catalog
            async def fail(connection, root):
                await original(connection, root)
                raise PortUnavailableError("transaction fixture failure")
            monkeypatch.setattr(repository, "_refresh_catalog", fail)
            await selected.run_once()
            document = await service.get(receipt.document_id)
            assert document.active_version_id == previous.version_id and document.pending_version_id == receipt.version_id
            assert await repository.read_catalog() == old_catalog
            assert (await repository.read_effective_versions(old_catalog, (PH,)))[0].version_id == previous.version_id
            assert (await service.get_operation(receipt.operation_id)).state.value == "pending"
            async with repository._engine.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM guideline_embeddings")) == 1
                assert await connection.scalar(text("SELECT count(*) FROM guideline_catalog_entries")) == 1
    asyncio.run(exercise())


def test_attempt_timeout_cancels_provider_and_exhausts_only_three_durable_attempts(settings):
    async def exercise():
        async with system(settings) as (repository, service, clock):
            receipt = await custom(service)
            cancelled = 0
            async def timeout(selected, inputs, token):
                nonlocal cancelled
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled += 1
            embedding = Embedding(timeout)
            selected = worker(repository, embedding, worker_timeout_seconds=1)
            for attempt in range(3):
                await selected.run_once()
                operation = await service.get_operation(receipt.operation_id)
                assert operation.attempts == attempt + 1 and operation.lease_owner is None
                if attempt < 2:
                    assert operation.state.value == "pending"
                    assert await selected.run_once() == 0
                    clock.advance(2 if attempt == 0 else 10)
            assert operation.state.value == "failed" and operation.safe_error_code == "guideline_embedding_timeout"
            assert len(embedding.calls) == cancelled == embedding.leases == 3
            assert await selected.run_once() == 0
            assert (await service.get(receipt.document_id)).active_version_id is None
    asyncio.run(exercise())


def test_failed_rebuild_is_durable_bounded_and_explicit_retry_resumes_same_generation(settings):
    async def exercise():
        async with system(settings) as (repository, service, _):
            _, old = await ready(repository, service)
            await custom(service, "Second family")
            await old.run_once()
            old_catalog = await repository.read_catalog()
            async def fail(selected, inputs, token): raise PortAuthenticationError("private credentials")
            embedding = Embedding(fail)
            selected = worker(repository, embedding, profile(model="fixture-rebuild", max_input_tokens=8192))
            await selected.run_once()
            async with repository._engine.connect() as connection:
                failed_id = await connection.scalar(text("SELECT operation_id FROM guideline_operations WHERE state = 'failed'"))
                generation_id = await connection.scalar(text("SELECT generation_id FROM guideline_index_generations WHERE state = 'failed'"))
                assert await connection.scalar(text("SELECT count(*) FROM guideline_operations WHERE generation_id = :id AND state = 'superseded'"), {"id": generation_id}) == 1
            assert generation_id is not None and failed_id is not None
            assert (await repository.read_catalog()) == old_catalog
            for _ in range(3): assert await selected.run_once() == 0
            assert len(embedding.calls) == 1
            failed = await service.get_operation(failed_id)
            details = await service.get(failed.receipt.document_id)
            assert details.preparation.receipt.operation_id == failed_id and details.state == "preparation_failed"
            listed = await service.list(source="all", state="preparation_failed", q="", limit=20, cursor=None)
            assert any(item.identity.guideline_id == failed.receipt.document_id for item in listed[0])
            receipt = await service.retry(details.preparation.receipt.operation_id, details.revision, uuid4())
            assert receipt.version_id == failed.receipt.version_id
            embedding.callback = None
            await finish_generation(selected, repository)
            assert (await repository.read_catalog()).generation_id == generation_id
            assert (await service.get_operation(receipt.operation_id)).state.value == "succeeded"
            assert (await service.get(failed.receipt.document_id)).preparation is None
            async with repository._engine.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM guideline_versions")) == 2
    asyncio.run(exercise())


def test_profile_change_supersedes_inflight_obsolete_rebuild_and_preserves_history(settings):
    async def exercise():
        async with system(settings) as (repository, service, _):
            await ready(repository, service)
            entered, release = asyncio.Event(), asyncio.Event()
            async def block(selected, inputs, token):
                if selected.model == "obsolete":
                    entered.set()
                    await release.wait()
            embedding = Embedding(block)
            selected = worker(repository, embedding, profile(model="obsolete", max_input_tokens=8192))
            task = asyncio.create_task(selected.run_once())
            await asyncio.wait_for(entered.wait(), 2)
            selected.profile = profile(model="current", max_input_tokens=8192)
            await selected.reconcile_generation()
            release.set()
            await task
            await finish_generation(selected, repository)
            assert (await repository.read_catalog()).profile == selected.profile
            async with repository._engine.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM guideline_index_generations")) == 3
                assert await connection.scalar(text("SELECT count(*) FROM guideline_embeddings b JOIN guideline_index_generations g ON g.generation_id = b.generation_id WHERE g.profile->>'model' = 'obsolete'")) == 0
    asyncio.run(exercise())


@pytest.mark.parametrize("target", ["original", "another_profile"])
def test_reconfiguration_supersedes_obsolete_failed_rebuild_and_clears_management_error(settings, target):
    async def exercise():
        async with system(settings) as (repository, service, _):
            receipt, original = await ready(repository, service)
            async def fail(selected, inputs, token):
                raise PortAuthenticationError("private credentials")
            embedding = Embedding(fail)
            selected = worker(repository, embedding, profile(model="failed-profile", max_input_tokens=8192))
            await selected.run_once()
            details = await service.get(receipt.document_id)
            failed_id = details.preparation.receipt.operation_id
            assert details.state == "preparation_failed" and details.active_version_id == receipt.version_id
            selected.profile = original.profile if target == "original" else profile(model="new-profile", max_input_tokens=8192)
            embedding.callback = None
            await finish_generation(selected, repository)
            assert (await service.get_operation(failed_id)).state.value == "superseded"
            details = await service.get(receipt.document_id)
            assert details.preparation is None and details.state == "available"
    asyncio.run(exercise())


def test_worker_concurrency_is_bounded_and_stop_does_not_dispatch_more_tasks(settings):
    async def exercise():
        async with system(settings) as (repository, service, _):
            receipts = [await custom(service, f"Family {index}") for index in range(3)]
            two_entered, release = asyncio.Event(), asyncio.Event()
            active, maximum = 0, 0
            async def block(selected, inputs, token):
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                if active == 2: two_entered.set()
                try: await release.wait()
                finally: active -= 1
            embedding = Embedding(block)
            selected = GuidelinePreparationWorker(repository, REGISTRY, profile(max_input_tokens=8192), GuidelinePolicy(), embedding.factory)
            task = asyncio.create_task(selected.run_once())
            await asyncio.wait_for(two_entered.wait(), 2)
            assert len(embedding.calls) == maximum == 2
            selected.request_stop()
            release.set()
            await task
            assert await selected.run_once() == 0 and len(embedding.calls) == 2
            states = [(await service.get_operation(receipt.operation_id)).state.value for receipt in receipts]
            assert states.count("running") == 2 and states.count("pending") == 1
    asyncio.run(exercise())


def test_worker_rechecks_lease_after_waiting_for_catalog_lock(settings):
    async def exercise():
        async with system(settings) as (repository, service, clock):
            entered, release = asyncio.Event(), asyncio.Event()
            async def block(selected, inputs, token):
                entered.set()
                await release.wait()
            selected = worker(repository, Embedding(block))
            receipt = await custom(service)
            task = asyncio.create_task(selected.run_once())
            await asyncio.wait_for(entered.wait(), 2)
            async with repository._engine.connect() as gate:
                transaction = await gate.begin()
                await gate.execute(text("SELECT singleton_id FROM guideline_catalog_state WHERE singleton_id = 1 FOR UPDATE"))
                release.set()
                async with asyncio.timeout(3):
                    while True:
                        await gate.execute(text("SELECT pg_stat_clear_snapshot()"))
                        waiting = await gate.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type = 'Lock' AND query LIKE '%SELECT singleton_id FROM guideline_catalog_state%'"))
                        if waiting: break
                        await asyncio.sleep(0.005)
                clock.advance(61)
                await transaction.commit()
            await task
            assert (await service.get(receipt.document_id)).active_version_id is None
            async with repository._engine.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM guideline_embeddings")) == 0
            await worker(repository, Embedding()).run_once()
            assert (await service.get_operation(receipt.operation_id)).attempts == 2
            assert (await service.get(receipt.document_id)).active_version_id == receipt.version_id
    asyncio.run(exercise())

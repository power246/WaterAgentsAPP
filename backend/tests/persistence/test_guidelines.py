import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.domain.guidelines import (
    CanonicalGuidelineDocumentV2, GuidelineDraftV1, GuidelineIdentity, GuidelineMutation,
    GuidelineOperationKind as Kind,
)
from app.domain.guidelines.identity import bundled_guideline_id
from app.domain.guidelines.mutation_bodies import action_body, create_body, publish_body, save_body
from app.domain.memory_rag.embedding_vector import EmbeddingVector
from app.infrastructure.config import load_parameter_capability_registry
from app.infrastructure.persistence.database import (
    IncompatibleSchemaError, alembic_config, check_database_schema, create_database_engine, upgrade_database,
)
from app.infrastructure.persistence.guideline_repository import PostgresGuidelineRepository
from scripts.test_database import temporary_migrated_schema
from tests.guideline_support import PH, content, identity, profile, version
from app.ports.errors import GuidelineConflictError


@pytest.fixture
def settings():
    with temporary_migrated_schema() as selected:
        yield selected


def mutation(kind, body, key=None):
    return GuidelineMutation.for_request(key or uuid4(), kind, body)


def draft(selected=None):
    return GuidelineDraftV1.model_validate((selected or content()).model_dump())


async def create(repository, selected_identity=None, selected_content=None, *, base_revision=None):
    selected_identity = selected_identity or identity()
    selected_draft = draft(selected_content)
    receipt = await repository.create_draft(selected_identity, selected_draft,
        mutation(Kind.CREATE, create_body(selected_identity, selected_draft, base_revision)),
        expected_base_revision=base_revision)
    return selected_identity, receipt


async def publish(repository, selected_version, expected_revision):
    return await repository.publish_version(selected_version, expected_revision=expected_revision,
        mutation=mutation(Kind.PUBLISH, publish_body(selected_version, expected_revision)))


async def active_generation(repository, selected_profile=None):
    generation = await repository.create_generation(selected_profile or profile())
    catalog = await repository.read_catalog()
    await repository.activate_generation(generation.generation_id, expected_catalog_revision=catalog.revision)
    return generation


async def write(repository, claim, generation, selected_version, *, now=None, vector=None):
    from app.domain.guidelines import canonical_guideline_document
    canonical = canonical_guideline_document(
        selected_version.content, generation.profile, load_parameter_capability_registry())
    await repository.write_embedding(claim, generation.generation_id, canonical,
        vector or EmbeddingVector(values=(1., 0., 0.)), now=now)


def test_summary_generation_rebuild_preserves_legacy_pin_and_original_body(settings):
    from app.domain.guidelines import canonical_guideline_document

    async def exercise():
        repository = PostgresGuidelineRepository(settings)
        try:
            legacy = await active_generation(repository, profile(text_contract_version=1, document_template_version=1))
            body = content(body_markdown="Full original conditions, exceptions and scientific explanation.")
            identity, receipt = await create(repository, selected_content=body)
            original = version(body, identity)
            await ready(repository, original, receipt.accepted_revision, legacy)
            old_catalog = await repository.read_catalog()
            current = await repository.create_generation(profile())
            with pytest.raises(GuidelineConflictError):
                await repository.activate_generation(current.generation_id, expected_catalog_revision=old_catalog.revision)
            document = canonical_guideline_document(original.content, current.profile, load_parameter_capability_registry())
            assert original.content.body_markdown not in document.canonical_text
            await repository.write_building_embedding(current.generation_id, original.version_id, document,
                EmbeddingVector(values=(1., 0., 0.)))
            await repository.activate_generation(current.generation_id, expected_catalog_revision=old_catalog.revision)
            for catalog in (old_catalog, await repository.read_catalog()):
                matches = await repository.rank_guidelines(catalog, (PH,), EmbeddingVector(values=(1., 0., 0.)), cutoff=.35, limit=3)
                assert matches[0].version == original
                assert matches[0].version.model_view.body == original.model_view.body
            assert (await repository.read_generation(legacy.generation_id)).state == "retired"
            assert await repository.read_embedding(legacy.generation_id, original.version_id) is not None
        finally:
            await repository.aclose()
    asyncio.run(exercise())


async def ready(repository, selected_version, revision, generation):
    receipt = await publish(repository, selected_version, revision)
    claim = await repository.claim_operation("fixture-worker")
    assert claim.receipt.operation_id == receipt.operation_id
    await write(repository, claim, generation, selected_version)
    await repository.activate_version(claim)
    return receipt


def bundled(key="fixture.mechanism"):
    root = bundled_guideline_id(key)
    return GuidelineIdentity(guideline_id=root, family_root_id=root, origin="bundled", bundle_key=key)


def override(base):
    return GuidelineIdentity(guideline_id=uuid4(), family_root_id=base.identity.family_root_id,
        origin="local_override", base_bundle_key=base.identity.bundle_key,
        based_on_version_id=base.version_id, base_content_digest=base.content.content_digest)


async def race(engine, *calls):
    """Both connections must block on a held real catalog row before release."""
    async with engine.connect() as gate:
        transaction = await gate.begin()
        await gate.execute(text("SELECT singleton_id FROM guideline_catalog_state WHERE singleton_id = 1 FOR UPDATE"))
        tasks = [asyncio.create_task(call()) for call in calls]
        try:
            async with asyncio.timeout(4):
                while True:
                    await gate.execute(text("SELECT pg_stat_clear_snapshot()"))
                    waiting = await gate.scalar(text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                        "AND wait_event_type = 'Lock' AND query LIKE '%SELECT singleton_id FROM guideline_catalog_state%'"
                    ))
                    if waiting == len(calls):
                        break
                    await asyncio.sleep(0.005)
            assert not any(task.done() for task in tasks)
            await transaction.commit()
            return await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if transaction.is_active:
                await transaction.rollback()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def test_migration_upgrade_downgrade_and_schema_guards(settings):
    config = alembic_config(settings)
    command.downgrade(config, "20261020_01")
    assert upgrade_database(settings) == "20261020_02"
    assert asyncio.run(check_database_schema(settings)) == "20261020_02"

    async def exercise():
        engine = create_database_engine(settings)
        try:
            async with engine.begin() as connection:
                await connection.execute(text("ALTER TABLE guideline_versions DISABLE TRIGGER guideline_versions_guard"))
            with pytest.raises(IncompatibleSchemaError, match="guideline"):
                await check_database_schema(settings, engine=engine)
            async with engine.begin() as connection:
                await connection.execute(text("ALTER TABLE guideline_versions ENABLE TRIGGER guideline_versions_guard"))
            assert await check_database_schema(settings, engine=engine) == "20261020_02"
        finally:
            await engine.dispose()
    asyncio.run(exercise())


def test_draft_save_receipt_precedes_cas_and_replays_lost_response(settings):
    async def exercise():
        repository = PostgresGuidelineRepository(settings)
        try:
            selected, created = await create(repository)
            new_draft = draft(content(title="Edited fixture"))
            request = mutation(Kind.SAVE_DRAFT, save_body(selected.guideline_id, new_draft, 1))
            committed = await repository.save_draft(selected.guideline_id, new_draft, expected_revision=1, mutation=request)
            # The connection/response disappears after COMMIT; a fresh repository retries old CAS.
            await repository.aclose()
            repository = PostgresGuidelineRepository(settings)
            replay = await repository.save_draft(selected.guideline_id, new_draft, expected_revision=1, mutation=request)
            assert replay.model_dump_json() == committed.model_dump_json()
            assert await repository.read_receipt(request) == committed
            document = await repository.read_document(selected.guideline_id)
            assert document.revision == 2 and document.activation_epoch == 0
            changed = draft(content(title="Different body"))
            changed_key = mutation(Kind.SAVE_DRAFT, save_body(selected.guideline_id, changed, 1), request.idempotency_key)
            with pytest.raises(GuidelineConflictError) as error:
                await repository.save_draft(selected.guideline_id, changed, expected_revision=1, mutation=changed_key)
            assert error.value.code == "guideline_idempotency_conflict"
            assert (await repository.read_document(selected.guideline_id)).draft == new_draft
            assert (await repository.read_operation(created.operation_id)).state.value == "succeeded"
        finally:
            await repository.aclose()
    asyncio.run(exercise())


def test_publication_replay_keeps_original_ids_and_approved_content(settings):
    async def exercise():
        repository = PostgresGuidelineRepository(settings)
        try:
            selected, _ = await create(repository)
            first = version(selected_identity=selected)
            request = mutation(Kind.PUBLISH, publish_body(first, 1))
            accepted = await repository.publish_version(first, expected_revision=1, mutation=request)
            regenerated = version(selected_identity=selected)
            assert regenerated.version_id != first.version_id
            replay = await repository.publish_version(regenerated, expected_revision=1, mutation=request)
            assert replay == accepted and accepted.status_code == 202
            assert await repository.read_version(selected.guideline_id, first.version_id) == first
            assert await repository.read_version(selected.guideline_id, regenerated.version_id) is None
            changed = version(content(title="Different publication"), selected)
            changed_request = mutation(Kind.PUBLISH, publish_body(changed, 1), request.idempotency_key)
            with pytest.raises(GuidelineConflictError) as error:
                await repository.publish_version(changed, expected_revision=1, mutation=changed_request)
            assert error.value.code == "guideline_idempotency_conflict"
            assert await repository.read_version(selected.guideline_id, changed.version_id) is None
            newer = draft(content(title="Next draft"))
            await repository.save_draft(selected.guideline_id, newer, expected_revision=2,
                mutation=mutation(Kind.SAVE_DRAFT, save_body(selected.guideline_id, newer, 2)))
            doc = await repository.read_document(selected.guideline_id)
            assert doc.activation_epoch == 1 and doc.revision == 3
            generation = await active_generation(repository)
            claim = await repository.claim_operation("publication-worker")
            await write(repository, claim, generation, first)
            await repository.activate_version(claim)
            assert (await repository.read_document(selected.guideline_id)).draft == newer
            assert await repository.read_effective_versions(await repository.read_catalog(), (PH,)) == (first,)
        finally:
            await repository.aclose()
    asyncio.run(exercise())


def test_create_receipt_replays_canonical_body_with_new_server_identity(settings):
    async def exercise():
        repository = PostgresGuidelineRepository(settings)
        try:
            first_identity, retry_identity = identity(), identity()
            first = GuidelineDraftV1(title="Cafe\u0301", body_markdown="A\r\nB")
            retried = GuidelineDraftV1(title="Café", body_markdown="A\nB")
            request = mutation(Kind.CREATE, create_body(first_identity, first, None))
            assert mutation(Kind.CREATE, create_body(retry_identity, retried, None), request.idempotency_key) == request
            accepted = await repository.create_draft(first_identity, first, request)
            assert await repository.create_draft(retry_identity, retried, request) == accepted
            assert await repository.read_document(retry_identity.guideline_id) is None
            assert (await repository.read_document(first_identity.guideline_id)).revision == 1
        finally:
            await repository.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("scenario", ["save", "replay", "override", "activation", "claim"])
def test_two_connection_races_have_one_legal_outcome(settings, scenario):
    async def exercise():
        engine = create_database_engine(settings)
        first = PostgresGuidelineRepository(settings, engine=engine)
        second = PostgresGuidelineRepository(settings, engine=engine)
        try:
            if scenario in {"save", "replay"}:
                selected, _ = await create(first)
                edited = draft(content(title="Concurrent draft"))
                request = mutation(Kind.SAVE_DRAFT, save_body(selected.guideline_id, edited, 1))
                other = request if scenario == "replay" else mutation(Kind.SAVE_DRAFT, save_body(selected.guideline_id, edited, 1))
                results = await race(engine,
                    lambda: first.save_draft(selected.guideline_id, edited, expected_revision=1, mutation=request),
                    lambda: second.save_draft(selected.guideline_id, edited, expected_revision=1, mutation=other))
                assert (await first.read_document(selected.guideline_id)).revision == 2
            elif scenario == "override":
                base = version(selected_identity=bundled())
                await publish(first, base, 0)
                selected_draft = draft()
                left, right = override(base), override(base)
                results = await race(engine,
                    lambda: first.create_draft(left, selected_draft,
                        mutation(Kind.CREATE, create_body(left, selected_draft, 1)), expected_base_revision=1),
                    lambda: second.create_draft(right, selected_draft,
                        mutation(Kind.CREATE, create_body(right, selected_draft, 1)), expected_base_revision=1))
            elif scenario == "claim":
                selected, _ = await create(first)
                accepted = await publish(first, version(selected_identity=selected), 1)
                results = await race(engine, lambda: first.claim_operation("first"), lambda: second.claim_operation("second"))
                claims = [result for result in results if result is not None]
                assert len(claims) == 1 and claims[0].receipt == accepted
            else:
                generation = await active_generation(first)
                selected, _ = await create(first)
                approved = version(selected_identity=selected)
                await publish(first, approved, 1)
                claim = await first.claim_operation("race-worker")
                await write(first, claim, generation, approved)
                before = await first.read_catalog()
                results = await race(engine, lambda: first.activate_version(claim), lambda: second.activate_version(claim))
                after = await first.read_catalog()
                assert after.revision == before.revision + 1
                assert await first.read_effective_versions(after, (PH,)) == (approved,)
            errors = [result for result in results if isinstance(result, BaseException)]
            if scenario == "claim":
                assert not errors
            elif scenario == "replay":
                assert not errors and results[0] == results[1]
            else:
                assert len(errors) == 1 and isinstance(errors[0], GuidelineConflictError), results
        finally:
            await engine.dispose()
    asyncio.run(exercise())


def test_lease_recovery_fences_old_attempt_and_manual_retry_preserves_version(settings):
    async def exercise():
        repository = PostgresGuidelineRepository(settings)
        try:
            generation = await active_generation(repository)
            selected, _ = await create(repository)
            approved = version(selected_identity=selected)
            accepted = await publish(repository, approved, 1)
            now = datetime.now(timezone.utc) + timedelta(seconds=1)
            first = await repository.claim_operation("same-owner", now=now, lease_seconds=5)
            await repository.aclose()
            repository = PostgresGuidelineRepository(settings)
            assert await repository.claim_operation("other", now=now + timedelta(seconds=4)) is None
            later = now + timedelta(seconds=6)
            second = await repository.claim_operation("same-owner", now=later, lease_seconds=5)
            assert second.attempts == 2 and second.receipt == first.receipt
            for stale in (lambda: write(repository, first, generation, approved, now=later),
                          lambda: repository.activate_version(first, now=later),
                          lambda: repository.fail_attempt(first, "guideline_provider_failed", retryable=False, now=later)):
                with pytest.raises(GuidelineConflictError, match="reload"):
                    await stale()
            await repository.fail_attempt(second, "guideline_provider_failed", retryable=False, now=later)
            retry_request = mutation(Kind.RETRY, {"operation_id": str(accepted.operation_id), "expected_revision": 2})
            retry = await repository.retry_operation(accepted.operation_id, expected_revision=2, mutation=retry_request)
            assert retry.version_id == approved.version_id and retry.operation_id != accepted.operation_id
            assert await repository.retry_operation(accepted.operation_id, expected_revision=2, mutation=retry_request) == retry
            third = await repository.claim_operation("retry-owner", now=later)
            assert third.attempts == 1
            await write(repository, third, generation, approved, now=later)
            await repository.activate_version(third, now=later)
            assert (await repository.read_operation(accepted.operation_id)).state.value == "superseded"
            assert await repository.read_version(selected.guideline_id, approved.version_id) == approved
        finally:
            await repository.aclose()
    asyncio.run(exercise())


def test_bounded_automatic_retry_and_expired_last_lease(settings):
    async def exercise():
        repository = PostgresGuidelineRepository(settings)
        try:
            selected, _ = await create(repository)
            accepted = await publish(repository, version(selected_identity=selected), 1)
            now = datetime.now(timezone.utc) + timedelta(seconds=1)
            for attempt, backoff in ((1, 2), (2, 10)):
                claim = await repository.claim_operation("worker", now=now, lease_seconds=1)
                assert claim.attempts == attempt
                await repository.fail_attempt(claim, "guideline_provider_failed", retryable=True, now=now)
                assert await repository.claim_operation("worker", now=now + timedelta(seconds=backoff - 1)) is None
                now += timedelta(seconds=backoff)
            last = await repository.claim_operation("worker", now=now, lease_seconds=1)
            assert last.attempts == 3
            assert await repository.claim_operation("replacement", now=now + timedelta(seconds=2)) is None
            failed = await repository.read_operation(accepted.operation_id)
            assert failed.state.value == "failed" and failed.safe_error_code == "guideline_attempts_exhausted"
            assert (await repository.read_document(selected.guideline_id)).pending_version_id == accepted.version_id
        finally:
            await repository.aclose()
    asyncio.run(exercise())


def test_disable_restore_invalidates_claims_and_preserves_historical_catalog(settings):
    async def exercise():
        repository = PostgresGuidelineRepository(settings)
        try:
            generation = await active_generation(repository)
            base = version(selected_identity=bundled())
            await ready(repository, base, 0, generation)
            old_catalog = await repository.read_catalog()
            local = override(base)
            await create(repository, local, base_revision=1)
            approved = version(content(title="Override fixture"), local)
            edited = draft(approved.content)
            await repository.save_draft(local.guideline_id, edited, expected_revision=1,
                mutation=mutation(Kind.SAVE_DRAFT, save_body(local.guideline_id, edited, 1)))
            await publish(repository, approved, 2)
            claim = await repository.claim_operation("override-worker")
            await write(repository, claim, generation, approved)
            root = await repository.read_document(base.identity.guideline_id)
            disable = mutation(Kind.SET_ENABLED, action_body(root.identity.guideline_id, root.revision, enabled=False))
            await repository.set_enabled(root.identity.guideline_id, False, expected_revision=root.revision, mutation=disable)
            assert await repository.read_effective_versions(await repository.read_catalog(), (PH,)) == ()
            assert await repository.read_effective_versions(old_catalog, (PH,)) == (base,)
            with pytest.raises(GuidelineConflictError):
                await repository.activate_version(claim)
            root = await repository.read_document(root.identity.guideline_id)
            restore = mutation(Kind.RESTORE_BUNDLED,
                action_body(root.identity.guideline_id, root.revision, version_id=str(base.version_id)))
            await repository.restore_bundled(root.identity.guideline_id, base.version_id,
                expected_revision=root.revision, mutation=restore)
            assert await repository.read_effective_versions(await repository.read_catalog(), (PH,)) == (base,)
            assert (await repository.read_operation(claim.receipt.operation_id)).state.value == "superseded"
        finally:
            await repository.aclose()
    asyncio.run(exercise())


def test_generation_switch_requires_complete_profile_and_retains_old_vectors(settings):
    async def exercise():
        repository = PostgresGuidelineRepository(settings)
        try:
            first = await active_generation(repository)
            selected, _ = await create(repository)
            approved = version(selected_identity=selected)
            await publish(repository, approved, 1)
            claim = await repository.claim_operation("worker")
            await write(repository, claim, first, approved)
            second = await repository.create_generation(profile(model="fixture-next"))
            await repository.activate_generation(second.generation_id,
                expected_catalog_revision=(await repository.read_catalog()).revision)
            with pytest.raises(GuidelineConflictError) as error:
                await repository.activate_version(claim)
            assert error.value.code == "guideline_embedding_not_ready"
            await write(repository, claim, second, approved)
            await repository.activate_version(claim)
            third = await repository.create_generation(profile(model="fixture-third"))
            with pytest.raises(GuidelineConflictError) as error:
                await repository.activate_generation(third.generation_id,
                    expected_catalog_revision=(await repository.read_catalog()).revision)
            assert error.value.code == "guideline_generation_incomplete"
            canonical = CanonicalGuidelineDocumentV2.from_content(
                approved.content, third.profile, load_parameter_capability_registry())
            await repository.write_building_embedding(third.generation_id, approved.version_id, canonical,
                                                       EmbeddingVector(values=(1., 0., 0.)))
            catalog = await repository.read_catalog()
            await repository.activate_generation(third.generation_id, expected_catalog_revision=catalog.revision)
            assert (await repository.read_catalog()).generation_id == third.generation_id
            assert await repository.read_effective_versions(catalog, (PH,)) == (approved,)
            assert (await repository.read_generation(first.generation_id)).state == "retired"
            async with repository._engine.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM guideline_embeddings")) == 3
        finally:
            await repository.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("constraint", ["version_number", "foreign_key", "interval", "override", "active", "building", "embedding", "immutable_version", "immutable_receipt"])
def test_postgres_constraints_and_immutable_guards(settings, constraint):
    async def exercise():
        engine = create_database_engine(settings)
        repository = PostgresGuidelineRepository(settings, engine=engine)
        try:
            generation = await active_generation(repository)
            base = version(selected_identity=bundled())
            accepted = await ready(repository, base, 0, generation)
            local = override(base)
            await create(repository, local, base_revision=1)
            params = {"id": uuid4(), "root": base.identity.guideline_id, "version": base.version_id,
                      "gen": generation.generation_id, "op": accepted.operation_id}
            statements = {
                "version_number": "INSERT INTO guideline_versions SELECT :id, document_id, family_root_id, version_number, content_digest, snapshot FROM guideline_versions WHERE version_id = :version",
                "foreign_key": "INSERT INTO guideline_catalog_entries VALUES (:id, :version, 999, NULL)",
                "interval": "INSERT INTO guideline_catalog_entries VALUES (:root, :version, 999, NULL)",
                "override": "INSERT INTO guideline_documents SELECT :id, family_root_id, identity, origin, bundle_key, based_on_version_id, base_content_digest, draft, draft_digest, revision, activation_epoch, disabled, active_version_id, pending_version_id FROM guideline_documents WHERE origin = 'local_override'",
                "active": "INSERT INTO guideline_index_generations SELECT :id, profile, profile_id, profile_digest, dimensions, state, catalog_revision FROM guideline_index_generations WHERE generation_id = :gen",
                "embedding": "INSERT INTO guideline_embeddings SELECT generation_id, version_id, profile_id, dimensions, canonical_document_digest, embedding FROM guideline_embeddings",
                "immutable_version": "UPDATE guideline_versions SET content_digest = 'changed' WHERE version_id = :version",
                "immutable_receipt": "UPDATE guideline_operations SET accepted_response = '{}' WHERE operation_id = :op",
            }
            if constraint == "building":
                building = await repository.create_generation(profile(model="other"))
                params["gen"] = building.generation_id
                statements["building"] = statements["active"]
            with pytest.raises(DBAPIError) as error:
                async with engine.begin() as connection:
                    await connection.execute(text(statements[constraint]), params)
            assert error.value.orig.sqlstate == {
                "foreign_key": "23503", "interval": "23P01",
                "immutable_version": "P0001", "immutable_receipt": "P0001",
            }.get(constraint, "23505")
            assert await repository.read_version(base.identity.guideline_id, base.version_id) == base
            assert await check_database_schema(settings, engine=engine) == "20261020_02"
        finally:
            await engine.dispose()
    asyncio.run(exercise())


def test_invalid_base_fk_rolls_back_document_and_receipt(settings):
    async def exercise():
        repository = PostgresGuidelineRepository(settings)
        try:
            base = version(selected_identity=bundled())
            await publish(repository, base, 0)
            invalid = override(base).model_copy(update={"based_on_version_id": uuid4()})
            request = mutation(Kind.CREATE, create_body(invalid, draft(), 1))
            with pytest.raises(GuidelineConflictError):
                await repository.create_draft(invalid, draft(), request, expected_base_revision=1)
            assert await repository.read_document(invalid.guideline_id) is None
            assert await repository.read_receipt(request) is None
            assert (await repository.read_document(base.identity.guideline_id)).revision == 1
        finally:
            await repository.aclose()
    asyncio.run(exercise())


def test_failed_republication_preserves_old_effective_version_and_new_intent_wins(settings):
    async def exercise():
        repository = PostgresGuidelineRepository(settings)
        try:
            generation = await active_generation(repository)
            selected, _ = await create(repository)
            first = version(selected_identity=selected)
            await ready(repository, first, 1, generation)
            pinned = await repository.read_catalog()
            second_content = content(title="Second fixture")
            next_draft = draft(second_content)
            await repository.save_draft(selected.guideline_id, next_draft, expected_revision=2,
                mutation=mutation(Kind.SAVE_DRAFT, save_body(selected.guideline_id, next_draft, 2)))
            second = version(second_content, selected).model_copy(update={"version_number": 2})
            pending = await publish(repository, second, 3)
            claim = await repository.claim_operation("worker")
            await repository.fail_attempt(claim, "guideline_provider_failed", retryable=False)
            assert await repository.read_effective_versions(await repository.read_catalog(), (PH,)) == (first,)
            third_content = content(title="Third fixture")
            third_draft = draft(third_content)
            await repository.save_draft(selected.guideline_id, third_draft, expected_revision=4,
                mutation=mutation(Kind.SAVE_DRAFT, save_body(selected.guideline_id, third_draft, 4)))
            third = version(third_content, selected).model_copy(update={"version_number": 3})
            await publish(repository, third, 5)
            assert (await repository.read_operation(pending.operation_id)).state.value == "superseded"
            assert (await repository.read_document(selected.guideline_id)).activation_epoch == 3
            with pytest.raises(GuidelineConflictError):
                await repository.activate_version(claim)
            retry = mutation(Kind.RETRY, {"operation_id": str(pending.operation_id), "expected_revision": 6})
            with pytest.raises(GuidelineConflictError):
                await repository.retry_operation(pending.operation_id, expected_revision=6, mutation=retry)
            fresh = await repository.claim_operation("worker")
            await write(repository, fresh, generation, third)
            await repository.activate_version(fresh)
            assert await repository.read_effective_versions(pinned, (PH,)) == (first,)
            assert await repository.read_version(selected.guideline_id, second.version_id) == second
            for enabled, revision in ((False, 6), (True, 7)):
                await repository.set_enabled(selected.guideline_id, enabled, expected_revision=revision,
                    mutation=mutation(Kind.SET_ENABLED, action_body(selected.guideline_id, revision, enabled=enabled)))
                assert await repository.read_effective_versions(await repository.read_catalog(), (PH,)) == ((third,) if enabled else ())
        finally:
            await repository.aclose()
    asyncio.run(exercise())


def test_bundle_refresh_keeps_override_intent_and_active_override(settings):
    async def exercise():
        repository = PostgresGuidelineRepository(settings)
        try:
            generation = await active_generation(repository)
            base = version(selected_identity=bundled())
            await ready(repository, base, 0, generation)
            local = override(base)
            await create(repository, local, base_revision=1)
            approved = version(selected_identity=local)
            await publish(repository, approved, 1)
            local_claim = await repository.claim_operation("override-worker")
            before = await repository.read_document(local.guideline_id)
            for number in (2, 3):
                new_base = version(content(title=f"Bundled fixture {number}"), base.identity).model_copy(update={"version_number": number})
                root = await repository.read_document(base.identity.guideline_id)
                await publish(repository, new_base, root.revision)
                assert (await repository.read_document(local.guideline_id)).activation_epoch == before.activation_epoch
                base_claim = await repository.claim_operation("bundle-worker")
                await write(repository, base_claim, generation, new_base)
                await repository.activate_version(base_claim)
                if number == 2:
                    await write(repository, local_claim, generation, approved)
                    await repository.activate_version(local_claim)
                assert await repository.read_effective_versions(await repository.read_catalog(), (PH,)) == (approved,)
            assert (await repository.read_document(local.guideline_id)).identity.based_on_version_id == base.version_id
        finally:
            await repository.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("bad_input", ["dimensions", "zero", "content", "profile", "canonical_metadata"])
def test_embedding_rows_bind_complete_approved_content_and_profile(settings, bad_input):
    async def exercise():
        repository = PostgresGuidelineRepository(settings)
        try:
            generation = await active_generation(repository)
            selected, _ = await create(repository)
            approved = version(selected_identity=selected)
            await publish(repository, approved, 1)
            claim = await repository.claim_operation("worker")
            selected_content = content(title="Unapproved edit") if bad_input == "content" else approved.content
            selected_profile = profile(model="wrong-profile") if bad_input == "profile" else generation.profile
            canonical = CanonicalGuidelineDocumentV2.from_content(selected_content, selected_profile, load_parameter_capability_registry())
            if bad_input == "canonical_metadata":
                canonical = canonical.model_copy(update={"keywords": ("injected",)})
            values = (1., 0.) if bad_input == "dimensions" else ((0., 0., 0.) if bad_input == "zero" else (1., 0., 0.))
            with pytest.raises(ValueError):
                await repository.write_embedding(claim, generation.generation_id, canonical, EmbeddingVector(values=values))
            with pytest.raises(GuidelineConflictError):
                await repository.activate_version(claim)
            await write(repository, claim, generation, approved)
            await repository.activate_version(claim)
        finally:
            await repository.aclose()
    asyncio.run(exercise())


def test_lease_expiry_during_catalog_lock_wait_cannot_complete(settings):
    async def exercise():
        engine = create_database_engine(settings)
        repository = PostgresGuidelineRepository(settings, engine=engine)
        try:
            generation = await active_generation(repository)
            selected, _ = await create(repository)
            approved = version(selected_identity=selected)
            await publish(repository, approved, 1)
            claim = await repository.claim_operation("short-lease-worker", lease_seconds=1)
            await write(repository, claim, generation, approved)
            async with engine.connect() as gate:
                transaction = await gate.begin()
                await gate.execute(text("SELECT singleton_id FROM guideline_catalog_state WHERE singleton_id = 1 FOR UPDATE"))
                completion = asyncio.create_task(repository.activate_version(claim))
                try:
                    async with asyncio.timeout(3):
                        while True:
                            await gate.execute(text("SELECT pg_stat_clear_snapshot()"))
                            waiting = await gate.scalar(text(
                                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                                "AND wait_event_type = 'Lock' AND query LIKE '%SELECT singleton_id FROM guideline_catalog_state%'"
                            ))
                            if waiting == 1:
                                break
                            await asyncio.sleep(0.005)
                    assert datetime.now(timezone.utc) < claim.lease_until
                    await asyncio.sleep((claim.lease_until - datetime.now(timezone.utc)).total_seconds() + 0.05)
                    await transaction.commit()
                    with pytest.raises(GuidelineConflictError) as error:
                        await completion
                    assert error.value.code == "guideline_stale_attempt"
                finally:
                    if transaction.is_active:
                        await transaction.rollback()
                    if not completion.done():
                        completion.cancel()
                    await asyncio.gather(completion, return_exceptions=True)
            assert await repository.read_effective_versions(await repository.read_catalog(), (PH,)) == ()
            assert (await repository.claim_operation("replacement")).attempts == 2
        finally:
            await engine.dispose()
    asyncio.run(exercise())

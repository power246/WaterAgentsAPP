import asyncio
import json
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.application.guidelines.bundled import BundledGuidelineImporter
from app.domain.guidelines import GuidelineDraftV1
from app.domain.guidelines.mutation_bodies import action_body, save_body
from app.infrastructure.guidelines.bundled_source import PackageBundledGuidelineSource
from app.infrastructure.persistence.database import create_database_engine
from app.infrastructure.persistence.guideline_repository import PostgresGuidelineRepository
from app.ports.errors import GuidelineBundleError, GuidelineConflictError, PortUnavailableError
from scripts.test_database import temporary_migrated_schema
from tests.guideline_bundle_support import bundled_version, write_bundle
from tests.guideline_support import PH, content, version
from tests.persistence.test_guidelines import active_generation, mutation, publish, race, write
from app.domain.guidelines.operation_kind import GuidelineOperationKind as Kind

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures/guidelines/bundled_reviewed"


@pytest.fixture
def settings():
    with temporary_migrated_schema() as selected:
        yield selected


def test_production_package_has_no_unreviewed_or_fixture_approval():
    assert PackageBundledGuidelineSource().read_versions() == ()


def test_fixture_resource_is_review_bound_and_stable(tmp_path):
    source = PackageBundledGuidelineSource(root=FIXTURES)
    first = source.read_versions()
    assert len(first) == 1 and first[0].review.review_note.startswith("Synthetic fixture approval")
    assert first[0].content.source_kind.value == "reference_derived"
    assert "https://" in first[0].content.body_markdown and "https://" not in first[0].model_view.body
    manifest = write_bundle(tmp_path, first)
    resource = tmp_path / manifest["documents"][0]["body_resource"]
    resource.write_bytes(resource.read_bytes().replace(b"\n", b"\r\n"))
    assert PackageBundledGuidelineSource(root=tmp_path).read_versions() == first


@pytest.mark.parametrize("bad", [
    "body_digest", "content_digest", "model_view_digest", "version", "bundle_key", "review_missing",
    "review_false", "review_digest", "review_identity", "review_note", "metadata_body", "parameter", "source",
    "missing_body", "duplicate_entry", "duplicate_field", "schema_version", "missing_manifest",
    "path_parent", "path_absolute", "path_windows", "path_empty_segment", "path_suffix",
])
def test_invalid_bundle_fails_closed_with_safe_error(tmp_path, bad):
    selected = PackageBundledGuidelineSource(root=FIXTURES).read_versions()[0]
    manifest = write_bundle(tmp_path, (selected,))
    entry = manifest["documents"][0]
    if bad in {"body_digest", "content_digest", "model_view_digest"}:
        entry[bad] = "sha256:" + "0" * 64
    elif bad == "version":
        entry["version"] = 0
    elif bad == "bundle_key":
        entry["bundle_key"] = "../bad"
    elif bad == "review_missing":
        del entry["review"]
    elif bad == "review_false":
        entry["review"]["review_confirmed"] = False
    elif bad == "review_digest":
        entry["review"]["content_digest"] = "sha256:" + "0" * 64
    elif bad == "review_identity":
        entry["review"]["guideline_id"] = str(uuid4())
    elif bad == "review_note":
        entry["review"]["review_note"] = " "
    elif bad == "metadata_body":
        entry["metadata"]["body_markdown"] = "Second content authority"
    elif bad == "parameter":
        entry["metadata"]["parameter_keys"][0]["code"] = "99999"
    elif bad == "source":
        entry["metadata"]["sources"] = []
    elif bad == "missing_body":
        (tmp_path / entry["body_resource"]).unlink()
    elif bad == "duplicate_entry":
        manifest["documents"].append(dict(entry))
    elif bad == "schema_version":
        manifest["schema_version"] = 2
    elif bad.startswith("path_"):
        entry["body_resource"] = {"path_parent": "../outside.md", "path_absolute": "/outside.md",
            "path_windows": "C:\\outside.md", "path_empty_segment": "a//b.md", "path_suffix": "body.txt"}[bad]
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    if bad == "duplicate_field":
        path.write_text('{"schema_version":1,"schema_version":2,"documents":[]}', encoding="utf-8")
    elif bad == "missing_manifest":
        path.unlink()
    with pytest.raises(GuidelineBundleError) as error:
        PackageBundledGuidelineSource(root=tmp_path).read_versions()
    assert str(tmp_path) not in str(error.value)
    assert error.value.code == "guideline_bundle_invalid"


def test_importer_reports_safe_errors_and_continues_other_families():
    first, second = bundled_version(), bundled_version(key="fixture.other")

    class Source:
        def read_versions(self):
            return first, second

    class Repository:
        def __init__(self):
            self.seen = []

        async def import_bundled_version(self, selected):
            self.seen.append(selected)
            if selected == first:
                raise PortUnavailableError("private provider diagnostic")
            return "accepted-fixture"

    repository = Repository()
    report = asyncio.run(BundledGuidelineImporter(Source(), repository).import_all())
    assert repository.seen == [first, second]
    assert report.receipts == ("accepted-fixture",) and report.error_codes == ("guideline_import_unavailable",)


def test_import_is_idempotent_and_only_prepares_approved_content(settings, tmp_path):
    async def exercise():
        repository = PostgresGuidelineRepository(settings)
        try:
            source = PackageBundledGuidelineSource(root=FIXTURES)
            approved = source.read_versions()[0]
            importer = BundledGuidelineImporter(source, repository)
            first = await importer.import_all()
            assert not first.error_codes and first.receipts[0].status_code == 202
            assert (await importer.import_all()) == first
            assert (await repository.read_document(approved.identity.guideline_id)).active_version_id is None
            assert await repository.read_effective_versions(await repository.read_catalog(), (PH,)) == ()
            await repository.aclose()
            repository = PostgresGuidelineRepository(settings)
            assert await repository.import_bundled_version(approved) == first.receipts[0]
            assert not (await BundledGuidelineImporter(PackageBundledGuidelineSource(), repository).import_all()).error_codes
            generation = await active_generation(repository)
            claim = await repository.claim_operation("fixture-import-worker")
            await write(repository, claim, generation, approved)
            await repository.activate_version(claim)
            assert await repository.read_effective_versions(await repository.read_catalog(), (PH,)) == (approved,)
            assert await repository.import_bundled_version(approved) == first.receipts[0]
            # A corrupt release leaves the existing library and effective catalog intact.
            manifest = write_bundle(tmp_path, (approved,))
            (tmp_path / manifest["documents"][0]["body_resource"]).unlink()
            bad = await BundledGuidelineImporter(PackageBundledGuidelineSource(root=tmp_path), repository).import_all()
            assert bad.error_codes and not bad.receipts
            assert await repository.read_effective_versions(await repository.read_catalog(), (PH,)) == (approved,)
            async with repository._engine.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM guideline_versions")) == 1
                assert await connection.scalar(text("SELECT count(*) FROM guideline_operations WHERE kind = 'import_bundled'")) == 1
        finally:
            await repository.aclose()
    asyncio.run(exercise())


def test_same_version_conflict_does_not_overwrite_or_block_other_imports(settings):
    async def exercise():
        repository = PostgresGuidelineRepository(settings)
        try:
            first = bundled_version()
            accepted = await repository.import_bundled_version(first)
            changed = bundled_version(selected_content=content(title="Conflicting fixture"))
            other = bundled_version(key="fixture.other")

            class Source:
                def read_versions(self):
                    return changed, other

            report = await BundledGuidelineImporter(Source(), repository).import_all()
            assert report.error_codes == ("guideline_bundle_version_conflict",)
            assert len(report.receipts) == 1
            assert await repository.read_version(first.identity.guideline_id, first.version_id) == first
            assert await repository.read_operation(accepted.operation_id) is not None
        finally:
            await repository.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("race_kind", ["import", "local_edit"])
def test_concurrent_import_or_local_edit_returns_one_durable_identity(settings, race_kind):
    async def exercise():
        engine = create_database_engine(settings)
        repository = PostgresGuidelineRepository(settings, engine=engine)
        try:
            approved = bundled_version()
            if race_kind == "import":
                results = await race(engine, lambda: repository.import_bundled_version(approved),
                    lambda: repository.import_bundled_version(approved))
                assert results[0] == results[1]
            else:
                await repository.import_bundled_version(approved)
                body = action_body(approved.identity.guideline_id, 1,
                    base_version_id=str(approved.version_id), base_content_digest=approved.content.content_digest)
                requests = [mutation(Kind.LOCAL_EDIT, body) for _ in range(2)]
                results = await race(engine, *(
                    lambda request=request: repository.local_edit(approved.identity.guideline_id, approved.version_id,
                        approved.content.content_digest, expected_revision=1, mutation=request) for request in requests))
                assert not any(isinstance(result, BaseException) for result in results), results
                assert results[0].document_id == results[1].document_id
                assert {result.status_code for result in results} == {200, 201}
                assert (await repository.read_document(results[0].document_id)).draft.source_kind.value == "user_authored"
            async with engine.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM guideline_versions")) == 1
        finally:
            await engine.dispose()
    asyncio.run(exercise())


async def local_edit(repository, base, expected_revision):
    request = mutation(Kind.LOCAL_EDIT, action_body(base.identity.guideline_id, expected_revision,
        base_version_id=str(base.version_id), base_content_digest=base.content.content_digest))
    return await repository.local_edit(base.identity.guideline_id, base.version_id, base.content.content_digest,
                                       expected_revision=expected_revision, mutation=request)


async def import_ready(repository, approved, generation):
    receipt = await repository.import_bundled_version(approved)
    claim = await repository.claim_operation("import-worker")
    assert claim.receipt.operation_id == receipt.operation_id
    await write(repository, claim, generation, approved)
    await repository.activate_version(claim)
    return receipt


def test_upgrade_local_edit_override_disable_and_explicit_restore_keep_history(settings):
    async def exercise():
        repository = PostgresGuidelineRepository(settings)
        try:
            generation = await active_generation(repository)
            base = bundled_version()
            await import_ready(repository, base, generation)
            created = await local_edit(repository, base, 1)
            override = await repository.read_document(created.document_id)
            assert override.identity.based_on_version_id == base.version_id and override.active_version_id is None
            assert override.draft.body_markdown == base.content.body_markdown
            edited = GuidelineDraftV1.model_validate(content(title="Private local draft").model_dump())
            await repository.save_draft(created.document_id, edited, expected_revision=1,
                mutation=mutation(Kind.SAVE_DRAFT, save_body(created.document_id, edited, 1)))
            new_base = bundled_version(2, selected_content=content(title="Bundled upgrade"))
            await import_ready(repository, new_base, generation)
            current = await local_edit(repository, new_base, 3)
            assert current.document_id == created.document_id
            assert (await repository.read_document(created.document_id)).draft == edited
            approved_override = version(edited.publication_content(repository._registry), override.identity)
            await publish(repository, approved_override, 2)
            claim = await repository.claim_operation("local-worker")
            await repository.fail_attempt(claim, "guideline_provider_failed", retryable=False)
            assert await repository.read_effective_versions(await repository.read_catalog(), (PH,)) == (new_base,)
            retry = await repository.retry_operation(claim.receipt.operation_id, expected_revision=3,
                mutation=mutation(Kind.RETRY, {"operation_id": str(claim.receipt.operation_id), "expected_revision": 3}))
            claim = await repository.claim_operation("local-worker")
            assert claim.receipt == retry
            await write(repository, claim, generation, approved_override)
            await repository.activate_version(claim)
            pinned = await repository.read_catalog()
            third = bundled_version(3, selected_content=content(title="Another bundled upgrade"))
            await import_ready(repository, third, generation)
            assert await repository.read_effective_versions(await repository.read_catalog(), (PH,)) == (approved_override,)
            root = await repository.read_document(base.identity.guideline_id)
            await repository.set_enabled(root.identity.guideline_id, False, expected_revision=root.revision,
                mutation=mutation(Kind.SET_ENABLED, action_body(root.identity.guideline_id, root.revision, enabled=False)))
            fourth = bundled_version(4, selected_content=content(title="Upgrade while disabled"))
            await import_ready(repository, fourth, generation)
            assert (await repository.read_document(root.identity.guideline_id)).disabled
            assert await repository.read_effective_versions(await repository.read_catalog(), (PH,)) == ()
            root = await repository.read_document(root.identity.guideline_id)
            with pytest.raises(GuidelineConflictError) as error:
                await repository.restore_bundled(root.identity.guideline_id, third.version_id, expected_revision=root.revision,
                    mutation=mutation(Kind.RESTORE_BUNDLED, action_body(root.identity.guideline_id, root.revision, version_id=str(third.version_id))))
            assert error.value.code == "guideline_bundle_target_changed"
            await repository.restore_bundled(root.identity.guideline_id, fourth.version_id, expected_revision=root.revision,
                mutation=mutation(Kind.RESTORE_BUNDLED, action_body(root.identity.guideline_id, root.revision, version_id=str(fourth.version_id))))
            assert await repository.read_effective_versions(await repository.read_catalog(), (PH,)) == (fourth,)
            assert await repository.read_effective_versions(pinned, (PH,)) == (approved_override,)
            restored = await repository.read_document(created.document_id)
            assert restored.draft == edited and restored.active_version_id is None
            assert await repository.read_version(created.document_id, approved_override.version_id) == approved_override
            # Future package upgrades keep the explicit restore choice.
            fifth = bundled_version(5, selected_content=content(title="Upgrade after restore"))
            await import_ready(repository, fifth, generation)
            assert await repository.read_effective_versions(await repository.read_catalog(), (PH,)) == (fifth,)
        finally:
            await repository.aclose()
    asyncio.run(exercise())


def test_new_bundle_intent_supersedes_only_old_base_and_keeps_local_claim(settings):
    async def exercise():
        repository = PostgresGuidelineRepository(settings)
        try:
            generation = await active_generation(repository)
            base = bundled_version()
            await import_ready(repository, base, generation)
            created = await local_edit(repository, base, 1)
            local = await repository.read_document(created.document_id)
            approved = version(local.draft.publication_content(repository._registry), local.identity)
            await publish(repository, approved, 1)
            local_claim = await repository.claim_operation("local-worker")
            second = bundled_version(2, selected_content=content(title="Second base"))
            stale_receipt = await repository.import_bundled_version(second)
            stale_claim = await repository.claim_operation("base-worker")
            third = bundled_version(3, selected_content=content(title="Third base"))
            current = await repository.import_bundled_version(third)
            assert (await repository.read_operation(stale_receipt.operation_id)).state.value == "superseded"
            assert (await repository.read_document(created.document_id)).activation_epoch == local.activation_epoch + 1
            with pytest.raises(GuidelineConflictError):
                await write(repository, stale_claim, generation, second)
            await write(repository, local_claim, generation, approved)
            await repository.activate_version(local_claim)
            base_claim = await repository.claim_operation("base-worker")
            assert base_claim.receipt == current
            await write(repository, base_claim, generation, third)
            await repository.activate_version(base_claim)
            assert await repository.read_effective_versions(await repository.read_catalog(), (PH,)) == (approved,)
            assert await repository.import_bundled_version(second) == stale_receipt
        finally:
            await repository.aclose()
    asyncio.run(exercise())


def test_local_edit_cas_base_digest_and_replay(settings):
    async def exercise():
        repository = PostgresGuidelineRepository(settings)
        try:
            base = bundled_version()
            await repository.import_bundled_version(base)
            for revision, digest in ((0, base.content.content_digest), (1, "sha256:" + "0" * 64)):
                request = mutation(Kind.LOCAL_EDIT, action_body(base.identity.guideline_id, revision,
                    base_version_id=str(base.version_id), base_content_digest=digest))
                with pytest.raises(GuidelineConflictError):
                    await repository.local_edit(base.identity.guideline_id, base.version_id, digest,
                        expected_revision=revision, mutation=request)
                assert await repository.read_receipt(request) is None
            request = mutation(Kind.LOCAL_EDIT, action_body(base.identity.guideline_id, 1,
                base_version_id=str(base.version_id), base_content_digest=base.content.content_digest))
            accepted = await repository.local_edit(base.identity.guideline_id, base.version_id,
                base.content.content_digest, expected_revision=1, mutation=request)
            await repository.aclose()
            repository = PostgresGuidelineRepository(settings)
            assert await repository.local_edit(base.identity.guideline_id, base.version_id,
                base.content.content_digest, expected_revision=1, mutation=request) == accepted
            override = await repository.read_document(accepted.document_id)
            assert override.active_version_id is None and override.pending_version_id is None
            async with repository._engine.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM guideline_versions")) == 1
        finally:
            await repository.aclose()
    asyncio.run(exercise())


def test_repeat_import_preserves_manual_retry_and_bounded_failed_state(settings):
    async def exercise():
        repository = PostgresGuidelineRepository(settings)
        try:
            base = bundled_version()
            first = await repository.import_bundled_version(base)
            claim = await repository.claim_operation("worker")
            await repository.fail_attempt(claim, "guideline_provider_failed", retryable=False)
            assert await repository.import_bundled_version(base) == first
            retry = await repository.retry_operation(first.operation_id, expected_revision=1,
                mutation=mutation(Kind.RETRY, {"operation_id": str(first.operation_id), "expected_revision": 1}))
            assert await repository.import_bundled_version(base) == retry
            next_claim = await repository.claim_operation("retry-worker")
            assert await repository.import_bundled_version(base) == retry
            assert (await repository.read_operation(next_claim.receipt.operation_id)).state.value == "running"
        finally:
            await repository.aclose()
    asyncio.run(exercise())


def test_fresh_install_accepts_release_version_gaps_and_downgrade_keeps_newer_intent(settings):
    async def exercise():
        repository = PostgresGuidelineRepository(settings)
        try:
            latest = bundled_version(7)
            accepted = await repository.import_bundled_version(latest)
            with pytest.raises(GuidelineConflictError) as error:
                await repository.import_bundled_version(bundled_version(1))
            assert error.value.code == "guideline_bundle_older_version"
            assert (await repository.read_document(latest.identity.guideline_id)).pending_version_id == latest.version_id
            assert await repository.import_bundled_version(latest) == accepted
            assert await repository.read_version(latest.identity.guideline_id, bundled_version(1).version_id) is None
        finally:
            await repository.aclose()
    asyncio.run(exercise())


def test_restore_does_not_select_a_still_pending_bundle_with_only_a_vector(settings):
    async def exercise():
        repository = PostgresGuidelineRepository(settings)
        try:
            generation = await active_generation(repository)
            first = bundled_version()
            await import_ready(repository, first, generation)
            second = bundled_version(2, selected_content=content(title="Pending fixture"))
            await repository.import_bundled_version(second)
            claim = await repository.claim_operation("worker")
            await write(repository, claim, generation, second)
            root = await repository.read_document(first.identity.guideline_id)
            with pytest.raises(GuidelineConflictError) as error:
                await repository.restore_bundled(root.identity.guideline_id, second.version_id, expected_revision=root.revision,
                    mutation=mutation(Kind.RESTORE_BUNDLED, action_body(root.identity.guideline_id, root.revision, version_id=str(second.version_id))))
            assert error.value.code == "guideline_bundle_target_changed"
            assert await repository.read_effective_versions(await repository.read_catalog(), (PH,)) == (first,)
        finally:
            await repository.aclose()
    asyncio.run(exercise())

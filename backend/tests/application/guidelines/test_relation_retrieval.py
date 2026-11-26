import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.application.analysis_runs.workflow import CancellationToken
from app.application.guidelines.embedding import validated_guideline_texts
from app.application.guidelines.retrieval import GuidelineRunContext
from app.domain.guidelines import canonical_guideline_document, GuidelineCatalogSnapshot, GuidelinePolicy
from app.domain.memory_rag.embedding_vector import EmbeddingVector
from app.infrastructure.config import load_parameter_capability_registry
from app.infrastructure.persistence.guideline_repository import PostgresGuidelineRepository
from app.ports.errors import GuidelineConflictError
from scripts.test_database import temporary_migrated_schema
from tests.guideline_support import PH, DO, profile, content, version
from tests.persistence.test_guidelines import active_generation, create, publish
from tests.application.guidelines.test_worker import Embedding, worker
from tests.application.guidelines.test_retrieval import task

REGISTRY = load_parameter_capability_registry()


def relation_profile(**changes):
    return profile(text_contract_version=3, document_template_version=3, **changes)


async def install(repository, generation, selected_content, values):
    identity, receipt = await create(repository, selected_content=selected_content)
    approved = version(selected_content, identity)
    await publish(repository, approved, receipt.accepted_revision)
    claim = await repository.claim_operation("synthetic-relation-worker")
    document = canonical_guideline_document(approved.content, generation.profile, REGISTRY)
    vectors = tuple(EmbeddingVector(values=values[f.relation]) for f in document.fragments)
    await repository.complete_operation(claim, generation.generation_id, document, vectors)
    return approved, document


def test_source_paragraphs_keep_qualifiers_share_identity_and_have_version_locators():
    selected = relation_profile()
    paragraph = "If buffering is known, compare acid response; otherwise retain uncertainty and alternatives."
    first = canonical_guideline_document(content(body_markdown=f"## Mechanism\n{paragraph}\n\n## Limit\nNever infer a measurement from this relation."), selected, REGISTRY)
    second = canonical_guideline_document(content(title="Another article", body_markdown="## Mechanism\n"+paragraph), selected, REGISTRY)
    shared = next(f for f in first.fragments if f.relation == "Mechanism\n"+paragraph)
    assert shared == second.fragments[0]
    assert shared.canonical_digest in {key for key, _ in first.links}
    assert first.links != second.links or first.body_digest != second.body_digest
    assert first.summary == content().summary
    assert all("##" not in f.relation for f in first.fragments)
    assert all(len(s.encode()) <= selected.max_input_tokens for s in validated_guideline_texts(selected, first.fragments))
    nested = canonical_guideline_document(content(body_markdown="# Invalid example\n\n## Mechanism\nNever infer a measurement from this relation."), selected, REGISTRY)
    assert nested.fragments[0].relation == "Invalid example / Mechanism\nNever infer a measurement from this relation."
    adjacent = canonical_guideline_document(content(body_markdown="# Conditions\nRetain this condition.\n# Invalid example\nDo not infer from this example."), selected, REGISTRY)
    assert {f.relation for f in adjacent.fragments} == {"Conditions\nRetain this condition.", "Invalid example\nDo not infer from this example."}


def test_postgres_coverage_beats_synonym_volume_and_scope_is_applied_before_ranking():
    async def exercise(settings):
        repository = PostgresGuidelineRepository(settings)
        try:
            generation = await active_generation(repository, relation_profile())
            duplicates = [f"Photosynthesis relationship wording {i}; preserve the same biological condition." for i in range(12)]
            values = {p: (.99, .1, 0) for p in duplicates}
            noisy, _ = await install(repository, generation, content(title="Synthetic repeated topic", body_markdown="\n\n".join(duplicates)), values)
            first, second = "Synthetic biological direction under its stated conditions.", "Synthetic buffering amplitude under its stated conditions."
            comprehensive, document = await install(repository, generation,
                content(title="Synthetic two independent aspects", body_markdown=first+"\n\n"+second), {first: (1, 0, 0), second: (0, 1, 0)})
            weak, _ = await install(repository, generation, content(title="Synthetic weak relation", body_markdown="Weak matching relation."), {"Weak matching relation.": (.4, .4, .825)})
            await install(repository, generation, content(title="Synthetic unauthorized parameter", parameter_keys=(DO,), body_markdown=first), {first: (1, 0, 0)})
            snapshot = await repository.read_catalog()
            vectors = (EmbeddingVector(values=(1, 0, 0)), EmbeddingVector(values=(0, 1, 0)))
            matches = await repository.rank_guidelines(snapshot, (PH,), vectors, cutoff=.35, limit=3)
            assert [m.version.version_id for m in matches] == [comprehensive.version_id, noisy.version_id, weak.version_id]
            assert matches[0].similarity == pytest.approx(1)
            assert matches[1].similarity < .85
            assert matches[0].version.model_view.body == first+"\n\n"+second
            async with repository._engine.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM guideline_relation_embeddings WHERE relation = :text"), {"text": first}) == 1
                assert await connection.scalar(text("SELECT count(DISTINCT version_id) FROM guideline_relation_links l JOIN guideline_relation_embeddings r USING (generation_id, fragment_digest) WHERE r.relation = :text"), {"text": first}) == 2
        finally:
            await repository.aclose()
    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_distinct_aspects_use_one_batch_and_nearly_identical_queries_do_not_add_votes():
    from tests.application.guidelines.test_retrieval import Repository, pin
    from app.domain.guidelines import GuidelineEmbeddingBatch

    async def exercise():
        selected = relation_profile(max_input_tokens=8192)
        repository = Repository()
        repository.catalog = repository.catalog.model_copy(update={"profile": selected})

        async def vectors(*_):
            return GuidelineEmbeddingBatch(profile=selected, vectors=(EmbeddingVector(values=(1, 0, 0)),
                EmbeddingVector(values=(1, .001, 0)), EmbeddingVector(values=(0, 1, 0))))

        embedding = Embedding(vectors)
        run = await pin(repository, embedding, selected=selected)
        session = run.session(task(run.pin.run_id, CancellationToken(asyncio.Event())), deadline=run._clock.now()+timedelta(seconds=60))
        try:
            result = await session.search("Original professional question", aspects=("Biological direction", "Synonym of biological direction", "Buffering amplitude"))
            assert result.status == "retrieved"
            assert len(embedding.calls) == len(repository.ranks) == 1
            assert [q.question for q in embedding.calls[0][1]] == ["Biological direction", "Synonym of biological direction", "Buffering amplitude"]
            assert len(repository.ranks[0][2]) == 2
            assert await session.search("Rephrased question", aspects=("A new direction",)) is result
            assert len(embedding.calls) == 1
        finally:
            await session.aclose()
    asyncio.run(exercise())


def test_relation_completeness_and_immutable_source_links_are_database_guarded():
    from sqlalchemy.exc import DBAPIError

    async def exercise(settings):
        repository = PostgresGuidelineRepository(settings)
        try:
            generation = await active_generation(repository, relation_profile())
            body = content(body_markdown="First synthetic relationship.\n\nSecond synthetic relationship with a limitation.")
            identity, receipt = await create(repository, selected_content=body)
            approved = version(body, identity)
            await publish(repository, approved, receipt.accepted_revision)
            claim = await repository.claim_operation("synthetic-complete-worker")
            document = canonical_guideline_document(body, generation.profile, REGISTRY)
            with pytest.raises(ValueError):
                await repository.complete_operation(claim, generation.generation_id, document, (EmbeddingVector(values=(1, 0, 0)),))
            async with repository._engine.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM guideline_relation_links")) == 0
            with pytest.raises(DBAPIError):
                async with repository._engine.begin() as connection:
                    await connection.execute(text("INSERT INTO guideline_embeddings (generation_id, version_id, profile_id, dimensions, canonical_document_digest, embedding, relation_count) VALUES (:g, :v, :p, 3, :d, '[1,0,0]'::public.vector, 2)"),
                        {"g": generation.generation_id, "v": approved.version_id, "p": generation.profile.profile_id, "d": document.canonical_digest})
            await repository.complete_operation(claim, generation.generation_id, document,
                (EmbeddingVector(values=(1, 0, 0)), EmbeddingVector(values=(0, 1, 0))))
            for statement in ("DELETE FROM guideline_relation_links", "UPDATE guideline_relation_embeddings SET relation = 'changed'",
                    "INSERT INTO guideline_relation_links SELECT generation_id, version_id, fragment_digest, 'paragraph:99' FROM guideline_relation_links LIMIT 1"):
                with pytest.raises(DBAPIError):
                    async with repository._engine.begin() as connection:
                        await connection.execute(text(statement))
            async with repository._engine.connect() as connection:
                assert await connection.scalar(text("SELECT relation_count FROM guideline_embeddings")) == 2
            assert (await repository.read_version(identity.guideline_id, approved.version_id)).content == body
        finally:
            await repository.aclose()
    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_worker_reuses_shared_relations_and_building_generation_keeps_old_pin():
    from tests.application.guidelines.test_worker import system, custom, publish as publish_service, finish_generation

    async def exercise(settings):
        async with system(settings) as (repository, service, clock):
            embedding = Embedding()
            selected = worker(repository, embedding, relation_profile(max_input_tokens=8192))
            first = await custom(service)
            await selected.run_once()
            assert len(embedding.calls) == 1
            old = await repository.read_catalog()
            second = await custom(service, title="Same relation in another synthetic article")
            await selected.run_once()
            assert len(embedding.calls) == 1
            assert (await service.get(second.document_id)).state == "available"
            next_worker = worker(repository, embedding, relation_profile(model="next-synthetic-model", max_input_tokens=8192))
            await finish_generation(next_worker, repository)
            assert len(embedding.calls) == 2
            assert (await repository.read_catalog()).profile != old.profile
            match = await repository.rank_guidelines(old, (PH,), EmbeddingVector(values=(1, 0, 0)), cutoff=.35, limit=3)
            assert len(match) == 1 and match[0].version.identity.guideline_id == first.document_id
    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_late_relation_preparation_cannot_publish_after_disable():
    from tests.application.guidelines.test_worker import system, custom

    async def exercise(settings):
        async with system(settings) as (repository, service, _):
            entered, release = asyncio.Event(), asyncio.Event()

            async def block(*_):
                entered.set()
                await release.wait()

            embedding = Embedding(block)
            selected = worker(repository, embedding, relation_profile(max_input_tokens=8192))
            receipt = await custom(service)
            pending = asyncio.create_task(selected.run_once())
            await asyncio.wait_for(entered.wait(), 2)
            current = await service.get(receipt.document_id)
            await service.set_enabled(receipt.document_id, False, current.revision, uuid4())
            release.set()
            await asyncio.wait_for(pending, 2)
            assert (await service.get_operation(receipt.operation_id)).state.value == "superseded"
            assert (await service.get(receipt.document_id)).active_version_id is None
            async with repository._engine.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM guideline_relation_links")) == 0
                assert await connection.scalar(text("SELECT count(*) FROM guideline_relation_embeddings")) == 0
                assert await connection.scalar(text("SELECT count(*) FROM guideline_embeddings")) == 0
    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_new_scope_does_not_inherit_a_shared_relation_after_disable():
    from app.domain.guidelines.operation_kind import GuidelineOperationKind
    from app.domain.guidelines.mutation_bodies import action_body
    from tests.persistence.test_guidelines import mutation

    async def exercise(settings):
        repository = PostgresGuidelineRepository(settings)
        try:
            generation = await active_generation(repository, relation_profile())
            relation = "Synthetic acid input and buffering response with stated limitations."
            first, _ = await install(repository, generation, content(body_markdown=relation), {relation: (1, 0, 0)})
            second, _ = await install(repository, generation, content(title="Synthetic shared topic", body_markdown=relation), {relation: (1, 0, 0)})
            old = await repository.read_catalog()
            document = await repository.read_document(first.identity.guideline_id)
            await repository.set_enabled(first.identity.guideline_id, False, expected_revision=document.revision,
                mutation=mutation(GuidelineOperationKind.SET_ENABLED, action_body(first.identity.guideline_id, document.revision, enabled=False)))
            now = await repository.read_catalog()
            vector = EmbeddingVector(values=(1, 0, 0))
            assert len(await repository.rank_guidelines(old, (PH,), vector, cutoff=.35, limit=3)) == 2
            matches = await repository.rank_guidelines(now, (PH,), vector, cutoff=.35, limit=3)
            assert [m.version.version_id for m in matches] == [second.version_id]
            async with repository._engine.connect() as connection:
                assert await connection.scalar(text("SELECT count(*) FROM guideline_relation_embeddings")) == 1
        finally:
            await repository.aclose()
    with temporary_migrated_schema() as settings:
        asyncio.run(exercise(settings))


def test_synthetic_corpus_comparison_uses_only_isolated_fixture_versions():
    from tests.guideline_relation_benchmark import evaluate_synthetic_relations
    from tests.application.guidelines.test_embedding import catalog
    from app.infrastructure.guidelines.bundled_source import PackageBundledGuidelineSource

    production_before = PackageBundledGuidelineSource().read_versions()
    with temporary_migrated_schema() as settings:
        settings = settings.model_copy(update={"model_catalog": catalog().capabilities,
            "embedding_api_key": None, "embedding_base_url": None, "openai_api_key": None})
        report = asyncio.run(evaluate_synthetic_relations(settings, Embedding()))
    assert report["status"] == "completed" and report["synthetic"] is True
    assert report["documents"] == 12 and report["queries"] == 10
    assert report["relation_version_links"] > report["unique_relation_vectors"]
    assert len(report["arms"]) == 3
    assert all(len(arm["cases"]) == 10 for arm in report["arms"].values())
    assert PackageBundledGuidelineSource().read_versions() == production_before


@pytest.mark.skipif(os.environ.get("WATERAGENTS_RELATION_LIVE_E2E") != "1", reason="Opt-in synthetic embedding comparison")
def test_real_embedding_synthetic_relation_comparison(tmp_path):
    from app.infrastructure.adapters.openai_guideline_embeddings import OpenAIGuidelineEmbeddingAdapter
    from app.infrastructure.credentials.embedding import embedding_api_key, embedding_base_url
    from app.infrastructure.http import ProviderHttpClient
    from tests.guideline_relation_benchmark import evaluate_synthetic_relations

    async def exercise(settings):
        async with ProviderHttpClient(timeout_seconds=20, max_response_bytes=4_000_000, retry_delay_seconds=0, attempts=1) as http:
            key = embedding_api_key(settings).get_secret_value()
            base_url = embedding_base_url(settings)
            preparation = OpenAIGuidelineEmbeddingAdapter(http, api_key=key, base_url=base_url, timeout_seconds=20)
            online = OpenAIGuidelineEmbeddingAdapter(http, api_key=key, base_url=base_url, timeout_seconds=3)
            return await evaluate_synthetic_relations(settings, preparation, online)

    with temporary_migrated_schema() as settings:
        report = asyncio.run(exercise(settings))
    output = Path(os.environ.get("WATERAGENTS_RELATION_REPORT_PATH", str(tmp_path / "synthetic-comparison.json")))
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    assert report["status"] == "completed"
    assert report["unique_relation_vectors"] < report["relation_version_links"]

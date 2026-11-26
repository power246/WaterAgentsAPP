import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.application.guidelines.authoring import GuidelineAuthoringService
from app.application.guidelines.errors import GuidelineServiceError
from app.domain.guidelines import GuidelineDraftV1
from app.infrastructure.config import load_parameter_capability_registry
from app.infrastructure.persistence.guideline_repository import PostgresGuidelineRepository
from scripts.test_database import temporary_migrated_schema
from tests.guideline_support import content
from tests.persistence.test_guidelines import active_generation, race, write


class Clock:
    def now(self):
        return datetime.now(timezone.utc)


@pytest.fixture
def settings():
    with temporary_migrated_schema() as selected:
        yield selected


def review(document, **overrides):
    return {"expected_revision": document.revision, "content_digest": document.draft.content_digest,
            "review_confirmed": True, "reviewer": "fixture-reviewer",
            "review_note": "Synthetic fixture approval; not an actual content review.", **overrides}


def test_service_publication_uses_saved_digest_and_replays_before_reading_new_draft(settings):
    async def exercise():
        repo = PostgresGuidelineRepository(settings)
        service = GuidelineAuthoringService(repo, load_parameter_capability_registry(), Clock())
        try:
            created = await service.create(GuidelineDraftV1.model_validate(content().model_dump()), uuid4())
            document = await service.get(created.document_id)
            key = uuid4()
            request = review(document)
            accepted = await service.publish(created.document_id, **request, key=key)
            edited = GuidelineDraftV1.model_validate(content(title="Later draft").model_dump())
            await service.save(created.document_id, edited, 2, uuid4())
            await repo.aclose()
            repo = PostgresGuidelineRepository(settings)
            service = GuidelineAuthoringService(repo, load_parameter_capability_registry(), Clock())
            assert await service.publish(created.document_id, **request, key=key) == accepted
            approved = await service.get_version(created.document_id, accepted.version_id)
            assert approved.content.title == document.draft.title
            generation = await active_generation(repo)
            claim = await repo.claim_operation("fixture-worker")
            await write(repo, claim, generation, approved)
            await repo.activate_version(claim)
            result = await service.get(created.document_id)
            assert result.draft == edited and result.active_version == approved and result.state == "available"
            assert result.draft_digest == edited.content_digest and result.has_unpublished_changes
        finally:
            await repo.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("bad", ["review", "digest", "source", "incomplete"])
def test_publication_rejects_unreviewed_or_invalid_saved_content(settings, bad):
    async def exercise():
        repo = PostgresGuidelineRepository(settings)
        service = GuidelineAuthoringService(repo, load_parameter_capability_registry(), Clock())
        try:
            draft = GuidelineDraftV1.model_validate(content().model_dump())
            if bad == "source":
                draft = GuidelineDraftV1.model_validate({**draft.model_dump(), "source_kind": "reference_derived", "sources": ()})
            elif bad == "incomplete":
                draft = GuidelineDraftV1(title="Unfinished")
            created = await service.create(draft, uuid4())
            document = await service.get(created.document_id)
            payload = review(document)
            if bad == "review":
                payload["review_confirmed"] = False
            if bad == "digest":
                payload["content_digest"] = "sha256:" + "0" * 64
            with pytest.raises(GuidelineServiceError) as error:
                await service.publish(created.document_id, **payload, key=uuid4())
            assert error.value.status == 422
            assert (await service.get(created.document_id)).latest_version_number == 0
        finally:
            await repo.aclose()
    asyncio.run(exercise())


def test_concurrent_publications_replay_same_key_but_conflict_on_distinct_keys(settings):
    async def exercise():
        repo = PostgresGuidelineRepository(settings)
        service = GuidelineAuthoringService(repo, load_parameter_capability_registry(), Clock())
        try:
            for same_key in (True, False):
                created = await service.create(GuidelineDraftV1.model_validate(content().model_dump()), uuid4())
                payload = review(await service.get(created.document_id))
                first_key, second_key = uuid4(), uuid4()
                results = await race(repo._engine,
                    lambda: service.publish(created.document_id, **payload, key=first_key),
                    lambda: service.publish(created.document_id, **payload, key=first_key if same_key else second_key))
                if same_key:
                    assert results[0] == results[1]
                else:
                    assert sum(isinstance(result, GuidelineServiceError) for result in results) == 1
                assert (await service.get(created.document_id)).latest_version_number == 1
        finally:
            await repo.aclose()
    asyncio.run(exercise())


def test_list_filters_and_cursor_are_local_bounded_and_bind_filters(settings):
    async def exercise():
        repo = PostgresGuidelineRepository(settings)
        service = GuidelineAuthoringService(repo, load_parameter_capability_registry(), Clock())
        try:
            for title in ("Alpha", "Beta", "Gamma"):
                await service.create(GuidelineDraftV1(title=title, keywords=("marker",)), uuid4())
            first, cursor = await service.list(source="mine", state="draft", q="marker", limit=1)
            assert len(first) == 1 and cursor
            second, _ = await service.list(source="mine", state="draft", q="marker", limit=2, cursor=cursor)
            assert len(second) == 2 and first[0].identity.guideline_id not in {item.identity.guideline_id for item in second}
            with pytest.raises(GuidelineServiceError) as error:
                await service.list(q="different", cursor=cursor)
            assert error.value.code == "guideline_cursor_invalid"
            assert (await service.list(q="alpHa"))[0][0].title == "Alpha"
            assert not (await service.list(q="%"))[0]
        finally:
            await repo.aclose()
    asyncio.run(exercise())

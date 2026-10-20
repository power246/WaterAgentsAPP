from uuid import UUID

from app.domain.guidelines.draft import GuidelineDraftV1
from app.domain.guidelines.identity import GuidelineIdentity
from app.domain.guidelines.version import GuidelineVersionV1


def create_body(identity: GuidelineIdentity, draft: GuidelineDraftV1, expected_base_revision: int | None) -> dict:
    stable_identity = identity.model_dump(mode="json", exclude={"guideline_id", "family_root_id"})
    return {"identity": stable_identity, "draft": draft.model_dump(mode="json"),
            "expected_base_revision": expected_base_revision}


def save_body(document_id: UUID, draft: GuidelineDraftV1, expected_revision: int) -> dict:
    return {"document_id": str(document_id), "draft": draft.model_dump(mode="json"),
            "expected_revision": expected_revision}


def publish_body(version: GuidelineVersionV1, expected_revision: int) -> dict:
    return publication_body(version.identity.guideline_id, expected_revision, version.content.content_digest,
                            version.review.review_confirmed, version.review.reviewer, version.review.review_note)


def publication_body(document_id: UUID, expected_revision: int, content_digest: str,
                     review_confirmed: bool, reviewer: str, review_note: str) -> dict:
    return {"document_id": str(document_id), "expected_revision": expected_revision,
            "content_digest": content_digest, "review_confirmed": review_confirmed,
            "reviewer": reviewer, "review_note": review_note}


def action_body(document_id: UUID, expected_revision: int, **fields) -> dict:
    return {"document_id": str(document_id), "expected_revision": expected_revision, **fields}

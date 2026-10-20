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
    return {"identity": version.identity.model_dump(mode="json"),
            "content": version.content.model_dump(mode="json"),
            "review": version.review.model_dump(mode="json", exclude={"reviewed_at"}),
            "expected_revision": expected_revision}


def action_body(document_id: UUID, expected_revision: int, **fields) -> dict:
    return {"document_id": str(document_id), "expected_revision": expected_revision, **fields}

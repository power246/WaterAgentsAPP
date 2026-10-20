from datetime import datetime, timezone
from uuid import uuid4

from app.domain.guidelines import (
    CanonicalGuidelineQueryV1, GuidelineContentV1, GuidelineEmbeddingProfileV1,
    GuidelineIdentity, GuidelineReview, GuidelineSourceRef, GuidelineVersionV1,
    project_model_view,
)
from app.domain.hydrology.parameter_key import ParameterKey

PH = ParameterKey(code_system="usgs", code="00400")
DO = ParameterKey(code_system="usgs", code="00300")
FIXTURE_TIME = datetime(2026, 10, 20, tzinfo=timezone.utc)


def source(**overrides) -> GuidelineSourceRef:
    return GuidelineSourceRef(**{
        "source_id": "S1", "title": "Synthetic test source",
        "url": "https://source.invalid/mechanism", **overrides,
    })


def content(**overrides) -> GuidelineContentV1:
    return GuidelineContentV1(**{
        "title": "Fixture mechanism", "summary": "Synthetic advisory content",
        "parameter_keys": (PH,), "language": "en", "source_kind": "user_authored",
        "body_markdown": "If condition A holds, consider B; otherwise retain alternative C.",
        **overrides,
    })


def profile(**overrides) -> GuidelineEmbeddingProfileV1:
    return GuidelineEmbeddingProfileV1(**{
        "provider": "openai", "model": "fixture-embedding", "output_dimensions": 3,
        "max_input_tokens": 2048, "document_instructions": "Represent the complete advisory topic.",
        "query_instructions": "Retrieve relevant advisory mechanisms.", **overrides,
    })


def query(selected_profile=None, **overrides) -> CanonicalGuidelineQueryV1:
    selected_profile = selected_profile or profile()
    return CanonicalGuidelineQueryV1(**{
        "profile_id": selected_profile.profile_id, "instructions": selected_profile.query_instructions,
        "parameter_keys": (PH,), "mode": "prediction", "question": "解释上下游酸碱变化",
        **overrides,
    })


def identity() -> GuidelineIdentity:
    guideline_id = uuid4()
    return GuidelineIdentity(
        guideline_id=guideline_id, family_root_id=guideline_id, origin="local_custom",
    )


def version(selected_content=None, selected_identity=None) -> GuidelineVersionV1:
    selected_content = selected_content or content()
    selected_identity = selected_identity or identity()
    return GuidelineVersionV1(
        version_id=uuid4(), version_number=1, identity=selected_identity, content=selected_content,
        model_view=project_model_view(selected_content), review=GuidelineReview(
            guideline_id=selected_identity.guideline_id, content_digest=selected_content.content_digest,
            review_confirmed=True, reviewer="fixture-reviewer",
            review_note="Synthetic fixture approval; not an actual content review.",
            reviewed_at=FIXTURE_TIME,
        ),
    )

from typing import Literal
from uuid import UUID

from app.domain.domain_model import DomainModel
from app.domain.guidelines.delivery_ref import GuidelineDeliveryRef
from app.domain.guidelines.origin import GuidelineOrigin
from app.domain.guidelines.source_kind import GuidelineSourceKind


class GuidelineCitationV1(DomainModel):
    schema_version: Literal[1] = 1
    delivery_ref: GuidelineDeliveryRef
    guideline_id: UUID
    family_root_id: UUID
    version_id: UUID
    origin: GuidelineOrigin
    source_kind: GuidelineSourceKind
    operation_id: UUID
    phase: Literal[
        "evidence_planning", "numeric_estimation", "reasoning_batch",
        "overall_synthesis", "schema_repair", "run_summary",
    ]

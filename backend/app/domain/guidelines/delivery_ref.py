from uuid import UUID

from app.domain.domain_model import DomainModel


class GuidelineDeliveryRef(DomainModel):
    run_id: UUID
    session_id: UUID
    item_id: UUID

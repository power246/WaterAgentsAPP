from uuid import UUID

from app.domain.common import DomainModel, UtcDatetime


class RAGSourceCutoff(DomainModel):
    """Stable inclusive transcript frontier used by a building generation."""

    created_at: UtcDatetime
    item_id: UUID

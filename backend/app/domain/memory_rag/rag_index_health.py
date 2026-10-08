from app.domain.common import DomainModel, NonEmptyString
from app.domain.memory_rag.rag_generation_health import RAGGenerationHealth


class RAGIndexHealth(DomainModel):
    active_generation_id: NonEmptyString | None = None
    generations: tuple[RAGGenerationHealth, ...]

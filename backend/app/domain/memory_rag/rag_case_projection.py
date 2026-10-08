from app.domain.memory_rag.embedding_vector import EmbeddingVector
from app.domain.memory_rag.prepared_rag_case_projection import (
    PreparedRAGCaseProjectionV1,
)


class RAGCaseProjectionV1(PreparedRAGCaseProjectionV1):
    embedding: EmbeddingVector

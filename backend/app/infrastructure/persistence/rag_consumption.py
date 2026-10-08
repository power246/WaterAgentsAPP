from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncConnection

from app.domain.memory_rag.historical_case_identity import (
    derive_historical_case_id,
)
from app.infrastructure.persistence.schema import transcript_items


async def has_valid_consumption_source(
    connection: AsyncConnection,
    *,
    source_generation_id: UUID,
    source_case_id: UUID,
    source_run_id: UUID,
    source_session_id: UUID,
    source_result_item_id: UUID,
) -> bool:
    if source_case_id != derive_historical_case_id(
        source_generation_id,
        source_run_id,
        source_session_id,
        source_result_item_id,
    ):
        return False
    source = (
        await connection.execute(
            select(transcript_items.c.item_id)
            .where(
                transcript_items.c.run_id == source_run_id,
                transcript_items.c.session_id == source_session_id,
                transcript_items.c.item_id == source_result_item_id,
                transcript_items.c.content_type == "inference_result",
                transcript_items.c.content_schema_version == 1,
                transcript_items.c.rag_eligible.is_(True),
            )
            .with_for_update(key_share=True)
        )
    ).one_or_none()
    return source is not None

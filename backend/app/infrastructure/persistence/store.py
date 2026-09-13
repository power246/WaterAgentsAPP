import asyncio

from sqlalchemy.ext.asyncio import AsyncEngine

from app.infrastructure.persistence.records import PersistenceBatch
from app.infrastructure.persistence.schema import (
    analysis_runs,
    analysis_sessions,
    transcript_contents,
    transcript_items,
)


class PostgresTranscriptStore:
    """Append a pre-validated group of immutable persistence rows atomically."""

    def __init__(self, engine: AsyncEngine, *, operation_timeout_seconds: float) -> None:
        if operation_timeout_seconds <= 0:
            raise ValueError("operation_timeout_seconds must be positive")
        self._engine = engine
        self._operation_timeout_seconds = operation_timeout_seconds

    async def commit_batch(self, batch: PersistenceBatch) -> None:
        # Re-validating here keeps callers from bypassing cross-table invariants via
        # model_construct. PostgreSQL CHECKs cannot assert existence in another table.
        validated = PersistenceBatch.model_validate(batch.model_dump(mode="python"))
        async with asyncio.timeout(self._operation_timeout_seconds):
            async with self._engine.begin() as connection:
                if validated.runs:
                    await connection.execute(
                        analysis_runs.insert(),
                        [record.model_dump(mode="python") for record in validated.runs],
                    )
                if validated.sessions:
                    await connection.execute(
                        analysis_sessions.insert(),
                        [
                            record.model_dump(mode="python")
                            for record in validated.sessions
                        ],
                    )
                if validated.items:
                    await connection.execute(
                        transcript_items.insert(),
                        [record.model_dump(mode="python") for record in validated.items],
                    )
                if validated.contents:
                    await connection.execute(
                        transcript_contents.insert(),
                        [
                            record.model_dump(mode="python")
                            for record in validated.contents
                        ],
                    )

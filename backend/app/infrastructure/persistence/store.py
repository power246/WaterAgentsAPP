import asyncio
import hashlib
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.application.analysis_runs.creation import (
    CreationVerificationUnavailableError,
    IdempotencyKeyConflictError,
    RunCreationCandidate,
    RunCreationDisposition,
    RunCreationResult,
    StoredRunCreation,
)

from app.infrastructure.persistence.records import PersistenceBatch, PersistedRunRecord
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

    async def get_creation(self, idempotency_key: UUID) -> RunCreationResult | None:
        async with asyncio.timeout(self._operation_timeout_seconds):
            async with self._engine.connect() as connection:
                row = (
                    await connection.execute(
                        select(
                            analysis_runs.c.run_id,
                            analysis_runs.c.idempotency_key,
                            analysis_runs.c.request_fingerprint,
                            analysis_runs.c.creation_receipt,
                        ).where(analysis_runs.c.idempotency_key == idempotency_key)
                    )
                ).mappings().one_or_none()
        if row is None:
            return None
        return RunCreationResult(
            disposition=RunCreationDisposition.EXISTING,
            creation=StoredRunCreation.model_validate(dict(row)),
        )

    async def create_run(self, candidate: RunCreationCandidate) -> RunCreationResult:
        batch = self._creation_batch(candidate)
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                async with self._engine.begin() as connection:
                    await self._lock_run(connection, candidate.snapshot.run_id)
                    await self._execute_batch(connection, batch)
        except IntegrityError:
            if candidate.idempotency_key is None:
                raise
            existing = await self.get_creation(candidate.idempotency_key)
            if existing is None:
                raise
            if existing.creation.request_fingerprint != candidate.request_fingerprint:
                raise IdempotencyKeyConflictError() from None
            return existing
        return RunCreationResult(
            disposition=RunCreationDisposition.CREATED,
            creation=self._stored_creation(candidate),
        )

    async def verify_creation(
        self,
        candidate: RunCreationCandidate,
    ) -> RunCreationResult | None:
        expected = self._creation_batch(candidate)
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                async with self._engine.begin() as connection:
                    await self._lock_run(connection, candidate.snapshot.run_id)
                    run_row = (
                        await connection.execute(
                            select(analysis_runs).where(
                                analysis_runs.c.run_id == candidate.snapshot.run_id
                            )
                        )
                    ).mappings().one_or_none()
                    if run_row is None:
                        return None
                    item_rows = tuple(
                        (
                            await connection.execute(
                                select(transcript_items)
                                .where(
                                    transcript_items.c.item_id.in_(
                                        item.item_id for item in expected.items
                                    )
                                )
                                .order_by(transcript_items.c.transcript_sequence)
                            )
                        ).mappings()
                    )
                    content_rows = tuple(
                        (
                            await connection.execute(
                                select(transcript_contents)
                                .where(
                                    transcript_contents.c.item_id.in_(
                                        content.item_id for content in expected.contents
                                    )
                                )
                                .order_by(transcript_contents.c.item_id)
                            )
                        ).mappings()
                    )
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise CreationVerificationUnavailableError() from error
        actual = PersistenceBatch(
            runs=(type(expected.runs[0]).model_validate(dict(run_row)),),
            items=tuple(
                type(expected.items[0]).model_validate(dict(row)) for row in item_rows
            ),
            contents=tuple(
                type(expected.contents[0]).model_validate(dict(row))
                for row in content_rows
            ) if expected.contents else (),
        )
        if actual != expected:
            raise ValueError("stored creation differs from the submitted candidate")
        return RunCreationResult(
            disposition=RunCreationDisposition.CREATED,
            creation=self._stored_creation(candidate),
        )

    async def commit_batch(self, batch: PersistenceBatch) -> None:
        # Re-validating here keeps callers from bypassing cross-table invariants via
        # model_construct. PostgreSQL CHECKs cannot assert existence in another table.
        validated = PersistenceBatch.model_validate(batch.model_dump(mode="python"))
        async with asyncio.timeout(self._operation_timeout_seconds):
            async with self._engine.begin() as connection:
                await self._execute_batch(connection, validated)

    @staticmethod
    async def _execute_batch(
        connection: AsyncConnection,
        batch: PersistenceBatch,
    ) -> None:
        if batch.runs:
            await connection.execute(
                analysis_runs.insert(),
                [record.model_dump(mode="python") for record in batch.runs],
            )
        if batch.sessions:
            await connection.execute(
                analysis_sessions.insert(),
                [record.model_dump(mode="python") for record in batch.sessions],
            )
        if batch.items:
            await connection.execute(
                transcript_items.insert(),
                [record.model_dump(mode="python") for record in batch.items],
            )
        if batch.contents:
            await connection.execute(
                transcript_contents.insert(),
                [record.model_dump(mode="python") for record in batch.contents],
            )

    @staticmethod
    async def _lock_run(connection: AsyncConnection, run_id: UUID) -> None:
        await connection.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": advisory_lock_key(run_id)},
        )

    @staticmethod
    def _stored_creation(candidate: RunCreationCandidate) -> StoredRunCreation:
        return StoredRunCreation(
            run_id=candidate.snapshot.run_id,
            idempotency_key=candidate.idempotency_key,
            request_fingerprint=candidate.request_fingerprint,
            creation_receipt=candidate.creation_receipt,
        )

    @staticmethod
    def _creation_batch(candidate: RunCreationCandidate) -> PersistenceBatch:
        run = PersistedRunRecord(
            run_id=candidate.snapshot.run_id,
            status=candidate.snapshot.status.value,
            created_at=candidate.snapshot.created_at,
            updated_at=candidate.snapshot.updated_at,
            query=candidate.query,
            idempotency_key=candidate.idempotency_key,
            request_fingerprint=candidate.request_fingerprint,
            creation_receipt=candidate.creation_receipt,
            request_item_id=candidate.request_item_id,
            created_item_id=candidate.created_item_id,
        )
        return PersistenceBatch(
            runs=(run,),
            items=candidate.transcript.items,
            contents=candidate.transcript.contents,
        )


def advisory_lock_key(run_id: UUID) -> int:
    digest = hashlib.blake2b(str(run_id).encode("ascii"), digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)

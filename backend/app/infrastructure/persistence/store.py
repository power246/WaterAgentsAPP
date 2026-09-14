import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select, text, update
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
from app.application.analysis_runs.models import (
    AgentRole,
    AgentSessionStatus,
    RunStatus,
    SessionStateMutation,
    SessionSummary,
)
from app.application.analysis_runs.persistence import (
    BatchVerification,
    PersistenceWriteBatch,
    PersistenceWriteConflictError,
)
from app.application.transcripts.models import (
    StoredTranscriptContent,
    StoredTranscriptItem,
)
from app.application.transcripts.read_models import (
    StoredRunHeader,
    StoredRunHistoryPage,
    StoredRunRead,
)

from app.infrastructure.persistence.records import PersistenceBatch, PersistedRunRecord
from app.infrastructure.persistence.schema import (
    analysis_runs,
    analysis_sessions,
    transcript_contents,
    transcript_items,
)
from app.ports.errors import PortUnavailableError


def _session_terminal_item_id(mutation: SessionStateMutation) -> UUID | None:
    if mutation.summary.status not in {
        AgentSessionStatus.COMPLETED,
        AgentSessionStatus.FAILED,
        AgentSessionStatus.CANCELLED,
    }:
        return None
    return (
        mutation.transcript_item.item_id
        if mutation.transcript_item is not None
        else None
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
            async with self._run_transaction(candidate.snapshot.run_id) as connection:
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
            async with self._run_transaction(candidate.snapshot.run_id) as connection:
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
                        )
                    ).mappings()
                )
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise CreationVerificationUnavailableError() from error
        item_rows_by_id = {row["item_id"]: row for row in item_rows}
        content_rows_by_id = {row["item_id"]: row for row in content_rows}
        expected_item_ids = {item.item_id for item in expected.items}
        expected_content_ids = {content.item_id for content in expected.contents}
        if (
            set(item_rows_by_id) != expected_item_ids
            or set(content_rows_by_id) != expected_content_ids
        ):
            raise ValueError("stored creation differs from the submitted candidate")
        actual = PersistenceBatch(
            runs=(type(expected.runs[0]).model_validate(dict(run_row)),),
            items=tuple(
                type(expected.items[0]).model_validate(
                    dict(item_rows_by_id[item.item_id])
                )
                for item in expected.items
            ),
            contents=tuple(
                type(expected.contents[0]).model_validate(
                    dict(content_rows_by_id[content.item_id])
                )
                for content in expected.contents
            ) if expected.contents else (),
        )
        if actual != expected:
            raise ValueError("stored creation differs from the submitted candidate")
        return RunCreationResult(
            disposition=RunCreationDisposition.CREATED,
            creation=self._stored_creation(candidate),
        )

    async def write_batch(self, batch: PersistenceWriteBatch) -> None:
        try:
            async with self._run_transaction(batch.run_id) as connection:
                await self._apply_state_mutations(connection, batch)
                await self._execute_transcript_batch(connection, batch)
        except IntegrityError as error:
            raise PersistenceWriteConflictError(
                "persistence batch violates an immutable database constraint"
            ) from error

    async def verify_batch(
        self,
        batch: PersistenceWriteBatch,
    ) -> BatchVerification:
        async with self._run_transaction(batch.run_id) as connection:
            evidence = await self._verification_evidence(connection, batch)
        if evidence and all(evidence):
            return BatchVerification.COMMITTED
        if evidence and not any(evidence):
            return BatchVerification.NOT_COMMITTED
        raise PersistenceWriteConflictError(
            "stored persistence batch is incomplete or conflicting"
        )

    async def list_runs(
        self,
        limit: int,
        after_created_at: datetime | None,
        after_run_id: UUID | None,
    ) -> StoredRunHistoryPage:
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive integer")
        if (after_created_at is None) != (after_run_id is None):
            raise ValueError("history cursor position must be complete")
        statement = select(analysis_runs)
        if after_created_at is not None and after_run_id is not None:
            statement = statement.where(or_(
                analysis_runs.c.created_at < after_created_at,
                and_(
                    analysis_runs.c.created_at == after_created_at,
                    analysis_runs.c.run_id < after_run_id,
                ),
            ))
        statement = statement.order_by(
            analysis_runs.c.created_at.desc(),
            analysis_runs.c.run_id.desc(),
        ).limit(limit)
        try:
            async with self._read_transaction() as connection:
                run_rows = tuple(
                    (await connection.execute(statement)).mappings()
                )
                created_ids = tuple(row["created_item_id"] for row in run_rows)
                item_rows = ()
                content_rows = ()
                if created_ids:
                    item_rows = tuple(
                        (
                            await connection.execute(
                                select(transcript_items).where(
                                    transcript_items.c.item_id.in_(created_ids)
                                )
                            )
                        ).mappings()
                    )
                    external_ids = tuple(
                        row["item_id"]
                        for row in item_rows
                        if row["inline_body"] is None
                    )
                    if external_ids:
                        content_rows = tuple(
                            (
                                await connection.execute(
                                    select(transcript_contents).where(
                                        transcript_contents.c.item_id.in_(external_ids)
                                    )
                                )
                            ).mappings()
                        )
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise PortUnavailableError("transcript history is unavailable") from error
        items = {
            row["item_id"]: StoredTranscriptItem.model_validate(dict(row))
            for row in item_rows
        }
        contents = {
            row["item_id"]: StoredTranscriptContent.model_validate(dict(row))
            for row in content_rows
        }
        return StoredRunHistoryPage(runs=tuple(
            StoredRunRead(
                run=self._stored_run_header(row),
                items=(items[row["created_item_id"]],)
                if row["created_item_id"] in items else (),
                contents=(contents[row["created_item_id"]],)
                if row["created_item_id"] in contents else (),
            )
            for row in run_rows
        ))

    async def read_run(self, run_id: UUID) -> StoredRunRead | None:
        try:
            async with self._read_transaction() as connection:
                run_row = (
                    await connection.execute(
                        select(analysis_runs).where(analysis_runs.c.run_id == run_id)
                    )
                ).mappings().one_or_none()
                if run_row is None:
                    return None
                session_rows = tuple(
                    (
                        await connection.execute(
                            select(analysis_sessions)
                            .where(analysis_sessions.c.run_id == run_id)
                            .order_by(
                                analysis_sessions.c.created_at,
                                analysis_sessions.c.session_id,
                            )
                        )
                    ).mappings()
                )
                item_rows = tuple(
                    (
                        await connection.execute(
                            select(transcript_items)
                            .where(transcript_items.c.run_id == run_id)
                            .order_by(transcript_items.c.transcript_sequence)
                        )
                    ).mappings()
                )
                external_ids = tuple(
                    row["item_id"] for row in item_rows if row["inline_body"] is None
                )
                content_rows = ()
                if external_ids:
                    content_rows = tuple(
                        (
                            await connection.execute(
                                select(transcript_contents).where(
                                    transcript_contents.c.item_id.in_(external_ids)
                                )
                            )
                        ).mappings()
                    )
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise PortUnavailableError("transcript history is unavailable") from error
        return StoredRunRead(
            run=self._stored_run_header(run_row),
            sessions=tuple(
                SessionSummary(
                    session_id=row["session_id"],
                    agent_role=AgentRole(row["agent_role"]),
                    target_comid=row["target_comid"],
                    status=AgentSessionStatus(row["status"]),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                for row in session_rows
            ),
            items=tuple(
                StoredTranscriptItem.model_validate(dict(row)) for row in item_rows
            ),
            contents=tuple(
                StoredTranscriptContent.model_validate(dict(row))
                for row in content_rows
            ),
        )

    async def list_interrupted_run_ids(self) -> tuple[UUID, ...]:
        active_session_runs = select(analysis_sessions.c.run_id).where(
            analysis_sessions.c.status.in_(("pending", "running"))
        )
        statement = (
            select(analysis_runs.c.run_id)
            .where(or_(
                analysis_runs.c.status.in_(("pending", "running")),
                analysis_runs.c.run_id.in_(active_session_runs),
            ))
            .order_by(analysis_runs.c.run_id)
        )
        try:
            async with self._read_transaction() as connection:
                return tuple((await connection.execute(statement)).scalars())
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise PortUnavailableError(
                "interrupted analysis scan is unavailable"
            ) from error

    async def commit_batch(self, batch: PersistenceBatch) -> None:
        # Re-validating here keeps callers from bypassing cross-table invariants via
        # model_construct. PostgreSQL CHECKs cannot assert existence in another table.
        validated = PersistenceBatch.model_validate(batch.model_dump(mode="python"))
        async with asyncio.timeout(self._operation_timeout_seconds):
            async with self._engine.begin() as connection:
                await self._execute_batch(connection, validated)

    async def _apply_state_mutations(
        self,
        connection: AsyncConnection,
        batch: PersistenceWriteBatch,
    ) -> None:
        for mutation in batch.run_mutations:
            if mutation.previous_status is None:
                raise PersistenceWriteConflictError(
                    "incremental run mutations require an existing run"
                )
            terminal_item_id = (
                mutation.transcript_item.item_id
                if mutation.snapshot.status
                in {
                    RunStatus.COMPLETED,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                    RunStatus.COMPLETED_WITH_PERSISTENCE_ERROR,
                }
                and mutation.transcript_item is not None
                else None
            )
            result = await connection.execute(
                update(analysis_runs)
                .where(
                    analysis_runs.c.run_id == batch.run_id,
                    analysis_runs.c.status == mutation.previous_status.value,
                )
                .values(
                    status=mutation.snapshot.status.value,
                    updated_at=mutation.snapshot.updated_at,
                    terminal_item_id=terminal_item_id,
                )
            )
            if result.rowcount != 1:
                raise PersistenceWriteConflictError(
                    "persisted run state changed unexpectedly"
                )
        for mutation in batch.session_mutations:
            terminal_item_id = _session_terminal_item_id(mutation)
            if mutation.previous_status is None:
                await connection.execute(
                    analysis_sessions.insert().values(
                        session_id=mutation.summary.session_id,
                        run_id=mutation.run_id,
                        agent_role=mutation.summary.agent_role.value,
                        target_comid=mutation.summary.target_comid,
                        status=mutation.summary.status.value,
                        created_at=mutation.summary.created_at,
                        updated_at=mutation.summary.updated_at,
                        created_item_id=mutation.transcript_item.item_id,
                        terminal_item_id=terminal_item_id,
                    )
                )
                continue
            result = await connection.execute(
                update(analysis_sessions)
                .where(
                    analysis_sessions.c.run_id == batch.run_id,
                    analysis_sessions.c.session_id == mutation.summary.session_id,
                    analysis_sessions.c.status == mutation.previous_status.value,
                )
                .values(
                    status=mutation.summary.status.value,
                    updated_at=mutation.summary.updated_at,
                    terminal_item_id=terminal_item_id,
                )
            )
            if result.rowcount != 1:
                raise PersistenceWriteConflictError(
                    "persisted Agent session state changed unexpectedly"
                )

    @staticmethod
    async def _execute_transcript_batch(
        connection: AsyncConnection,
        batch: PersistenceWriteBatch,
    ) -> None:
        if batch.transcript.items:
            await connection.execute(
                transcript_items.insert(),
                [
                    record.model_dump(mode="python")
                    for record in batch.transcript.items
                ],
            )
        if batch.transcript.contents:
            await connection.execute(
                transcript_contents.insert(),
                [
                    record.model_dump(mode="python")
                    for record in batch.transcript.contents
                ],
            )

    async def _verification_evidence(
        self,
        connection: AsyncConnection,
        batch: PersistenceWriteBatch,
    ) -> list[bool]:
        evidence: list[bool] = []
        contents = {content.item_id: content for content in batch.transcript.contents}
        for expected in batch.transcript.items:
            row = (
                await connection.execute(
                    select(transcript_items).where(
                        transcript_items.c.item_id == expected.item_id
                    )
                )
            ).mappings().one_or_none()
            if row is None:
                evidence.append(False)
                continue
            exact = type(expected).model_validate(dict(row)) == expected
            if expected.inline_body is None:
                content_row = (
                    await connection.execute(
                        select(transcript_contents).where(
                            transcript_contents.c.item_id == expected.item_id
                        )
                    )
                ).mappings().one_or_none()
                exact = exact and content_row is not None and (
                    type(contents[expected.item_id]).model_validate(dict(content_row))
                    == contents[expected.item_id]
                )
            evidence.append(exact)
        if batch.run_mutations:
            row = (
                await connection.execute(
                    select(
                        analysis_runs.c.status,
                        analysis_runs.c.updated_at,
                        analysis_runs.c.terminal_item_id,
                    ).where(analysis_runs.c.run_id == batch.run_id)
                )
            ).mappings().one()
            final = batch.run_mutations[-1]
            evidence.append(
                row["status"] == final.snapshot.status.value
                and row["updated_at"] == final.snapshot.updated_at
                and row["terminal_item_id"]
                == (
                    final.transcript_item.item_id
                    if final.transcript_item is not None
                    else None
                )
            )
        by_session = {}
        for mutation in batch.session_mutations:
            by_session.setdefault(mutation.summary.session_id, []).append(mutation)
        for session_id, mutations in by_session.items():
            row = (
                await connection.execute(
                    select(
                        analysis_sessions.c.agent_role,
                        analysis_sessions.c.target_comid,
                        analysis_sessions.c.status,
                        analysis_sessions.c.created_at,
                        analysis_sessions.c.updated_at,
                        analysis_sessions.c.created_item_id,
                        analysis_sessions.c.terminal_item_id,
                    ).where(
                        analysis_sessions.c.run_id == batch.run_id,
                        analysis_sessions.c.session_id == session_id,
                    )
                )
            ).mappings().one_or_none()
            final = mutations[-1]
            if row is None:
                evidence.append(False)
                continue
            created_item = next(
                mutation.transcript_item.item_id
                for mutation in mutations
                if mutation.previous_status is None
            ) if mutations[0].previous_status is None else row["created_item_id"]
            evidence.append(
                row["agent_role"] == final.summary.agent_role.value
                and row["target_comid"] == final.summary.target_comid
                and row["status"] == final.summary.status.value
                and row["created_at"] == final.summary.created_at
                and row["updated_at"] == final.summary.updated_at
                and row["created_item_id"] == created_item
                and row["terminal_item_id"]
                == _session_terminal_item_id(final)
            )
        return evidence

    @asynccontextmanager
    async def _run_transaction(
        self,
        run_id: UUID,
    ) -> AsyncIterator[AsyncConnection]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._operation_timeout_seconds
        connection: AsyncConnection | None = None
        try:
            async with asyncio.timeout_at(deadline):
                connection = await self._engine.connect()
                transaction = await connection.begin()
                await self._configure_statement_timeout(connection, deadline)
                await self._lock_run(connection, run_id)
                yield connection
                await transaction.commit()
        except BaseException:
            if connection is not None:
                await self._discard_connection(connection)
            raise
        else:
            if connection is not None:
                await self._close_connection(connection)

    @asynccontextmanager
    async def _read_transaction(self) -> AsyncIterator[AsyncConnection]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._operation_timeout_seconds
        connection: AsyncConnection | None = None
        try:
            async with asyncio.timeout_at(deadline):
                connection = await self._engine.connect()
                transaction = await connection.begin()
                await connection.execute(text(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                ))
                await self._configure_statement_timeout(connection, deadline)
                yield connection
                await transaction.commit()
        except BaseException:
            if connection is not None:
                await self._discard_connection(connection)
            raise
        else:
            if connection is not None:
                await self._close_connection(connection)

    @staticmethod
    async def _close_connection(connection: AsyncConnection) -> None:
        try:
            async with asyncio.timeout(1):
                await connection.close()
        except asyncio.CancelledError:
            await PostgresTranscriptStore._discard_connection(connection)
            raise
        except (SQLAlchemyError, OSError, TimeoutError):
            await PostgresTranscriptStore._discard_connection(connection)

    @staticmethod
    async def _discard_connection(connection: AsyncConnection) -> None:
        async def discard() -> None:
            try:
                await connection.invalidate()
            finally:
                await connection.close()

        task = asyncio.create_task(discard())
        try:
            async with asyncio.timeout(1):
                await asyncio.shield(task)
        except (SQLAlchemyError, OSError, TimeoutError, asyncio.CancelledError):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _configure_statement_timeout(
        self,
        connection: AsyncConnection,
        deadline: float | None = None,
    ) -> None:
        remaining = (
            self._operation_timeout_seconds
            if deadline is None
            else deadline - asyncio.get_running_loop().time()
        )
        milliseconds = max(1, int(remaining * 1000))
        await connection.scalar(
            text("SELECT set_config('statement_timeout', :timeout, true)"),
            {"timeout": f"{milliseconds}ms"},
        )

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
        await connection.scalar(
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
    def _stored_run_header(row) -> StoredRunHeader:
        return StoredRunHeader(
            run_id=row["run_id"],
            status=RunStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            query=row["query"],
            request_item_id=row["request_item_id"],
            created_item_id=row["created_item_id"],
            terminal_item_id=row["terminal_item_id"],
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

"""Durable, database-serialized Admin operation lifecycle."""

import asyncio
import base64
import binascii
import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.application.settings_admin.errors import (
    AdminOperationConflictError, UnitPolicyIdempotencyConflictError,
)
from app.domain.settings_admin import AdminCommand
from app.domain.settings_admin import AdminOperation, AdminOperationState
from app.infrastructure.persistence.schema import admin_operations
from app.ports.errors import PortUnavailableError


def _project(row) -> AdminOperation:
    return AdminOperation.model_validate({
        "operation_id": row["operation_id"],
        "kind": row["kind"], "state": row["state"],
        "resource_identity": row["resource_identity"],
        "progress": {
            "processed_items": row["processed_items"],
            "total_items": row["total_items"],
            "succeeded_items": row["succeeded_items"],
            "failed_items": row["failed_items"],
        },
        "safe_result": row["safe_result"], "safe_error": row["safe_error"],
        "created_at": row["created_at"], "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    })


def _command(row) -> AdminCommand | None:
    value = row["safe_request_summary"].get("command")
    return AdminCommand.model_validate(value) if value is not None else None


def _encode_cursor(operation: AdminOperation) -> str:
    value = json.dumps([operation.created_at.isoformat(), str(operation.operation_id)])
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    if len(cursor) > 256:
        raise ValueError("invalid Admin operation cursor")
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError("invalid cursor")
        timestamp = datetime.fromisoformat(value[0])
        if timestamp.tzinfo is None:
            raise ValueError("cursor timezone missing")
        return timestamp.astimezone(UTC), UUID(value[1])
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error) as error:
        raise ValueError("invalid Admin operation cursor") from error


class PostgresAdminOperationRepository:
    def __init__(self, engine: AsyncEngine, *, operation_timeout_seconds: float) -> None:
        if operation_timeout_seconds <= 0:
            raise ValueError("operation_timeout_seconds must be positive")
        self._engine = engine
        self._timeout = operation_timeout_seconds

    async def create(
        self, operation: AdminOperation, *, idempotency_key: UUID,
        request_fingerprint: bytes, confirmation_token_digest: bytes,
        command: AdminCommand | None = None,
    ) -> AdminOperation:
        if operation.state is not AdminOperationState.PENDING:
            raise ValueError("new Admin operation must be pending")
        if idempotency_key.version != 4 or any(
            len(value) != 32 for value in (request_fingerprint, confirmation_token_digest)
        ):
            raise ValueError("Admin operation identities must be UUID v4 and SHA-256")
        if command is not None and (
            command.kind is not operation.kind
            or command.resource_identity != operation.resource_identity
        ):
            raise ValueError("Admin command and operation identity disagree")
        try:
            async with asyncio.timeout(self._timeout):
                async with self._engine.begin() as connection:
                    await connection.execute(insert(admin_operations).values(
                        operation_id=operation.operation_id, kind=operation.kind.value,
                        state=operation.state.value,
                        resource_identity=operation.resource_identity,
                        request_fingerprint=request_fingerprint,
                        idempotency_key=idempotency_key,
                        confirmation_token_digest=confirmation_token_digest,
                        safe_request_summary={
                            "action": operation.kind.value,
                            "resource_identity": operation.resource_identity,
                            **({"command": command.model_dump(mode="json")}
                               if command is not None else {}),
                        },
                        processed_items=operation.progress.processed_items,
                        total_items=operation.progress.total_items,
                        succeeded_items=operation.progress.succeeded_items,
                        failed_items=operation.progress.failed_items,
                        safe_result=None, safe_error=None,
                        created_at=operation.created_at,
                    ))
            return operation
        except IntegrityError as error:
            # The unique constraints serialize concurrent creates. Re-read only
            # after the failed transaction rolls back.
            try:
                async with asyncio.timeout(self._timeout):
                    async with self._engine.connect() as connection:
                        row = (await connection.execute(
                            select(admin_operations).where(
                                admin_operations.c.idempotency_key == idempotency_key
                            )
                        )).mappings().one_or_none()
            except (SQLAlchemyError, OSError, TimeoutError) as read_error:
                raise PortUnavailableError("admin operation store unavailable") from read_error
            if row is not None and row["request_fingerprint"] == request_fingerprint:
                return _project(row)
            if row is not None:
                raise UnitPolicyIdempotencyConflictError() from error
            raise AdminOperationConflictError() from error
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise PortUnavailableError("admin operation store unavailable") from error
    async def read_receipt(
        self, idempotency_key: UUID,
    ) -> tuple[AdminOperation, bytes, AdminCommand | None] | None:
        try:
            async with asyncio.timeout(self._timeout):
                async with self._engine.connect() as connection:
                    row = (await connection.execute(select(admin_operations).where(
                        admin_operations.c.idempotency_key == idempotency_key
                    ))).mappings().one_or_none()
            return None if row is None else (
                _project(row), row["request_fingerprint"], _command(row),
            )
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise PortUnavailableError("admin operation store unavailable") from error

    async def list_pending(self) -> tuple[tuple[AdminOperation, AdminCommand | None], ...]:
        try:
            async with asyncio.timeout(self._timeout):
                async with self._engine.connect() as connection:
                    rows = tuple((await connection.execute(
                        select(admin_operations).where(
                            admin_operations.c.state == "pending"
                        ).order_by(admin_operations.c.created_at,
                                   admin_operations.c.operation_id)
                    )).mappings())
            return tuple((_project(row), _command(row)) for row in rows)
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise PortUnavailableError("admin operation store unavailable") from error

    async def reconcile_running(self, *, now: datetime) -> int:
        if now.tzinfo is None:
            raise ValueError("now must be timezone aware")
        try:
            async with asyncio.timeout(self._timeout):
                async with self._engine.begin() as connection:
                    result = await connection.execute(update(admin_operations).where(
                        admin_operations.c.state == "running"
                    ).values(
                        state="interrupted", finished_at=now,
                        safe_error={"code": "operation_interrupted",
                                    "message": "The operation was interrupted.",
                                    "retryable": True},
                    ))
                    return result.rowcount
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise PortUnavailableError("admin operation store unavailable") from error

    async def get(self, operation_id: UUID) -> AdminOperation | None:
        try:
            async with asyncio.timeout(self._timeout):
                async with self._engine.connect() as connection:
                    row = (await connection.execute(select(admin_operations).where(
                        admin_operations.c.operation_id == operation_id
                    ))).mappings().one_or_none()
                    return None if row is None else _project(row)
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise PortUnavailableError("admin operation store unavailable") from error

    async def list_recent(
        self, *, limit: int, cursor: str | None,
    ) -> tuple[tuple[AdminOperation, ...], str | None]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        boundary = _decode_cursor(cursor) if cursor is not None else None
        query = select(admin_operations)
        if boundary is not None:
            query = query.where(or_(
                admin_operations.c.created_at < boundary[0],
                and_(admin_operations.c.created_at == boundary[0],
                     admin_operations.c.operation_id < boundary[1]),
            ))
        query = query.order_by(
            admin_operations.c.created_at.desc(), admin_operations.c.operation_id.desc()
        ).limit(limit + 1)
        try:
            async with asyncio.timeout(self._timeout):
                async with self._engine.connect() as connection:
                    rows = tuple((await connection.execute(query)).mappings())
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise PortUnavailableError("admin operation store unavailable") from error
        page = tuple(_project(row) for row in rows[:limit])
        return page, _encode_cursor(page[-1]) if len(rows) > limit else None

    async def compare_and_swap_state(
        self, operation: AdminOperation, *, expected: AdminOperationState,
    ) -> AdminOperation:
        try:
            async with asyncio.timeout(self._timeout):
                async with self._engine.begin() as connection:
                    row = (await connection.execute(select(admin_operations).where(
                        admin_operations.c.operation_id == operation.operation_id
                    ).with_for_update())).mappings().one_or_none()
                    if row is None or row["state"] != expected.value:
                        raise AdminOperationConflictError()
                    current = _project(row)
                    try:
                        current.transition(operation)
                    except ValueError as error:
                        raise AdminOperationConflictError() from error
                    await connection.execute(update(admin_operations).where(
                        admin_operations.c.operation_id == operation.operation_id
                    ).values(
                        state=operation.state.value,
                        processed_items=operation.progress.processed_items,
                        total_items=operation.progress.total_items,
                        succeeded_items=operation.progress.succeeded_items,
                        failed_items=operation.progress.failed_items,
                        safe_result=(operation.safe_result.model_dump(mode="json")
                                     if operation.safe_result is not None else None),
                        safe_error=(operation.safe_error.model_dump(mode="json")
                                    if operation.safe_error is not None else None),
                        started_at=operation.started_at, finished_at=operation.finished_at,
                    ))
                    return operation
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise PortUnavailableError("admin operation store unavailable") from error

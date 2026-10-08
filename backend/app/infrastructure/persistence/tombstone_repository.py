"""Read deleted idempotency identities without retaining request content."""

import asyncio
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.settings_admin import IdempotencyTombstone
from app.infrastructure.persistence.schema import analysis_run_idempotency_tombstones
from app.ports.errors import PortUnavailableError


class PostgresTombstoneRepository:
    def __init__(self, engine: AsyncEngine, *, operation_timeout_seconds: float) -> None:
        if operation_timeout_seconds <= 0:
            raise ValueError("operation_timeout_seconds must be positive")
        self._engine = engine
        self._timeout = operation_timeout_seconds

    async def get(self, idempotency_key: UUID) -> IdempotencyTombstone | None:
        if idempotency_key.version != 4:
            raise ValueError("idempotency key must be UUID v4")
        try:
            async with asyncio.timeout(self._timeout):
                async with self._engine.connect() as connection:
                    row = (await connection.execute(select(
                        analysis_run_idempotency_tombstones
                    ).where(
                        analysis_run_idempotency_tombstones.c.idempotency_key
                        == idempotency_key
                    ))).mappings().one_or_none()
                    return None if row is None else IdempotencyTombstone(**row)
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise PortUnavailableError("tombstone store unavailable") from error

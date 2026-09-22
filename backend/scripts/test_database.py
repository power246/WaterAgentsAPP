import asyncio
import os
from contextlib import contextmanager
from collections.abc import Iterator
from uuid import uuid4

from sqlalchemy import text

from app.infrastructure.config import AppSettings
from app.infrastructure.persistence.database import (
    check_database_schema,
    create_database_engine,
    upgrade_database,
)


def require_test_database_settings() -> AppSettings:
    business = AppSettings()
    test_name = os.environ.get("WATERAGENTS_TEST_DB_NAME", "").strip()
    if not test_name:
        raise RuntimeError(
            "WATERAGENTS_TEST_DB_NAME is required for PostgreSQL smoke tests"
        )
    if test_name == business.db_name:
        raise RuntimeError(
            "WATERAGENTS_TEST_DB_NAME must differ from WATERAGENTS_DB_NAME"
        )
    return business.model_copy(update={"db_name": test_name})


async def _create_schema(settings: AppSettings, schema: str) -> None:
    engine = create_database_engine(settings)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    finally:
        await engine.dispose()


async def _drop_schema(settings: AppSettings, schema: str) -> None:
    engine = create_database_engine(settings)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        await engine.dispose()


@contextmanager
def temporary_migrated_schema() -> Iterator[AppSettings]:
    settings = require_test_database_settings()
    schema = f"wateragents_m3_{uuid4().hex}"
    previous_options = os.environ.get("PGOPTIONS")
    created = False
    try:
        asyncio.run(_create_schema(settings, schema))
        created = True
        os.environ["PGOPTIONS"] = f"-csearch_path={schema}"
        upgrade_database(settings)
        asyncio.run(check_database_schema(settings))
    except Exception as error:
        if previous_options is None:
            os.environ.pop("PGOPTIONS", None)
        else:
            os.environ["PGOPTIONS"] = previous_options
        if created:
            asyncio.run(_drop_schema(settings, schema))
        raise RuntimeError(
            "the isolated PostgreSQL smoke schema could not be prepared"
        ) from error
    try:
        yield settings
    finally:
        if previous_options is None:
            os.environ.pop("PGOPTIONS", None)
        else:
            os.environ["PGOPTIONS"] = previous_options
        asyncio.run(_drop_schema(settings, schema))

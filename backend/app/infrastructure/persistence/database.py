import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import URL, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.infrastructure.config.settings import AppSettings

_REVISION_PATTERN = re.compile(r"^(?P<date>\d{8})_(?P<sequence>\d+)$")


class DatabaseConfigurationError(RuntimeError):
    pass


class DatabaseConnectionError(RuntimeError):
    pass


class SchemaVersionError(RuntimeError):
    pass


class MissingSchemaError(SchemaVersionError):
    pass


class OutdatedSchemaError(SchemaVersionError):
    pass


class NewerSchemaError(SchemaVersionError):
    pass


class IncompatibleSchemaError(SchemaVersionError):
    pass


class MultipleSchemaHeadsError(SchemaVersionError):
    pass


def require_database_password(settings: AppSettings) -> str:
    secret = settings.db_password
    if secret is None or secret.get_secret_value() == "":
        raise DatabaseConfigurationError(
            "WATERAGENTS_DB_PASSWORD is required for database commands and startup"
        )
    return secret.get_secret_value()


def database_url(settings: AppSettings) -> URL:
    password = require_database_password(settings)
    query = {"sslmode": settings.db_sslmode}
    if settings.db_sslrootcert is not None:
        query["sslrootcert"] = settings.db_sslrootcert
    return URL.create(
        "postgresql+psycopg",
        username=settings.db_user,
        password=password,
        host=settings.db_host,
        port=settings.db_port,
        database=settings.db_name,
        query=query,
    )


def create_database_engine(settings: AppSettings) -> AsyncEngine:
    return create_async_engine(
        database_url(settings),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
    )


def migration_path() -> Path:
    return Path(__file__).resolve().with_name("migrations")


def alembic_config(settings: AppSettings) -> Config:
    config = Config()
    config.set_main_option("script_location", str(migration_path()))
    config.set_main_option(
        "sqlalchemy.url",
        database_url(settings).render_as_string(hide_password=False).replace("%", "%%"),
    )
    config.attributes["operation_timeout_seconds"] = (
        settings.db_operation_timeout_seconds
    )
    return config


def expected_schema_head(config: Config) -> str:
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise MultipleSchemaHeadsError(
            f"application migration history must have one head; found {len(heads)}"
        )
    return heads[0]


def upgrade_database(settings: AppSettings) -> str:
    config = alembic_config(settings)
    expected = expected_schema_head(config)
    try:
        command.upgrade(config, "head")
    except (SQLAlchemyError, OSError) as error:
        raise DatabaseConnectionError("database upgrade failed") from error
    return expected


async def check_database_schema(
    settings: AppSettings,
    *,
    engine: AsyncEngine | None = None,
) -> str:
    config = alembic_config(settings)
    expected = expected_schema_head(config)
    script = ScriptDirectory.from_config(config)
    owned_engine = engine is None
    active_engine = engine or create_database_engine(settings)
    try:
        async with asyncio.timeout(settings.db_operation_timeout_seconds):
            async with active_engine.connect() as connection:
                table_name = await connection.scalar(
                    text("SELECT to_regclass('public.alembic_version')")
                )
                if table_name is None:
                    raise MissingSchemaError(
                        "database schema is missing; run wateragents-db upgrade"
                    )
                revisions = tuple(
                    (
                        await connection.execute(
                            text("SELECT version_num FROM alembic_version")
                        )
                    ).scalars()
                )
    except SchemaVersionError:
        raise
    except (SQLAlchemyError, OSError, TimeoutError) as error:
        raise DatabaseConnectionError("database schema check failed") from error
    finally:
        if owned_engine:
            await active_engine.dispose()

    _validate_database_revisions(revisions, expected, script)
    return expected


def _validate_database_revisions(
    revisions: tuple[str, ...],
    expected: str,
    script: ScriptDirectory,
) -> None:
    if not revisions:
        raise MissingSchemaError(
            "database schema version is empty; run wateragents-db upgrade"
        )
    if len(revisions) != 1:
        raise MultipleSchemaHeadsError(
            f"database must have one schema head; found {len(revisions)}"
        )
    current = revisions[0]
    if current == expected:
        return

    try:
        script.get_revision(current)
    except Exception as error:
        expected_order = _revision_order(expected)
        current_order = _revision_order(current)
        if expected_order is not None and current_order is not None:
            if current_order > expected_order:
                raise NewerSchemaError(
                    f"database schema {current} is newer than application schema {expected}"
                ) from error
        raise IncompatibleSchemaError(
            f"database schema revision {current} is unknown to this application"
        ) from error

    script.get_revision(expected)
    if _is_ancestor(script, current, expected):
        raise OutdatedSchemaError(
            f"database schema {current} is older than application schema {expected}; "
            "run wateragents-db upgrade"
        )
    if _is_ancestor(script, expected, current):
        raise NewerSchemaError(
            f"database schema {current} is newer than application schema {expected}"
        )
    raise IncompatibleSchemaError(
        f"database schema {current} is not compatible with application schema {expected}"
    )


def _revision_order(revision: str) -> tuple[int, int] | None:
    match = _REVISION_PATTERN.fullmatch(revision)
    if match is None:
        return None
    return int(match.group("date")), int(match.group("sequence"))


def _is_ancestor(script: ScriptDirectory, ancestor: str, descendant: str) -> bool:
    pending = [descendant]
    visited: set[str] = set()
    while pending:
        revision_id = pending.pop()
        if revision_id in visited:
            continue
        visited.add(revision_id)
        revision = script.get_revision(revision_id)
        parents = revision.down_revision
        if parents is None:
            continue
        for parent in (parents,) if isinstance(parents, str) else parents:
            if parent == ancestor:
                return True
            pending.append(parent)
    return False


@asynccontextmanager
async def database_engine(settings: AppSettings) -> AsyncIterator[AsyncEngine]:
    engine = create_database_engine(settings)
    try:
        yield engine
    finally:
        await engine.dispose()

"""Verify and import this archived run into a compatible WaterAgents database.

Run ``python import_run.py --check-only`` before configuring a database.  The
default import target is the database configured by the project's backend/.env.
An explicit ``--database-url`` can select an isolated database instead.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    LargeBinary,
    Text,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.application.transcripts.models import StoredTranscriptItem
from app.application.transcripts.registry import create_production_registry
from app.infrastructure.config.settings import (
    SettingsConfigurationError,
    load_app_settings,
)
from app.infrastructure.persistence.database import (
    DatabaseConfigurationError,
    DatabaseConnectionError,
    SchemaVersionError,
    check_database_schema,
    create_database_engine,
)
from app.infrastructure.persistence.schema import (
    analysis_runs,
    analysis_sessions,
    transcript_contents,
    transcript_items,
)
from app.infrastructure.runtime.event_loop import configure_asyncio_event_loop

PACKAGE_DIR = Path(__file__).resolve().parent
TABLES = {
    "analysis_runs": analysis_runs,
    "analysis_sessions": analysis_sessions,
    "transcript_items": transcript_items,
    "transcript_contents": transcript_contents,
}
HEX_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class PackageError(ValueError):
    """The export is incomplete, damaged, or incompatible with this code."""


def _read_json(path: Path) -> object:
    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise PackageError(f"Duplicate JSON key in {path.name}: {key}")
            result[key] = value
        return result

    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=unique_pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(
                PackageError(f"Non-finite JSON number in {path.name}: {value}")
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise PackageError(f"Invalid JSON file: {path.name}") from error


def _parse_uuid(value: object, label: str) -> UUID:
    if not isinstance(value, str):
        raise PackageError(f"Invalid UUID at {label}")
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise PackageError(f"Invalid UUID at {label}") from error
    if str(parsed) != value:
        raise PackageError(f"Noncanonical UUID at {label}")
    return parsed


def _decode_value(column: object, value: object, label: str) -> object:
    if value is None:
        if not column.nullable:
            raise PackageError(f"Missing required value at {label}")
        return None
    kind = column.type
    if isinstance(kind, PGUUID):
        return _parse_uuid(value, label)
    if isinstance(kind, DateTime):
        if not isinstance(value, str):
            raise PackageError(f"Invalid timestamp at {label}")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise PackageError(f"Invalid timestamp at {label}") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise PackageError(f"Timestamp requires an offset at {label}")
        return parsed
    if isinstance(kind, LargeBinary):
        if not isinstance(value, dict) or set(value) != {"base64"}:
            raise PackageError(f"Invalid encoded bytes at {label}")
        encoded = value["base64"]
        if not isinstance(encoded, str):
            raise PackageError(f"Invalid encoded bytes at {label}")
        try:
            return base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as error:
            raise PackageError(f"Invalid encoded bytes at {label}") from error
    if isinstance(kind, JSONB):
        if not isinstance(value, dict):
            raise PackageError(f"Expected JSON object at {label}")
        return value
    if isinstance(kind, ARRAY):
        if not isinstance(value, list) or any(
            not isinstance(part, str) for part in value
        ):
            raise PackageError(f"Expected string array at {label}")
        return value
    if isinstance(kind, Boolean):
        if type(value) is not bool:
            raise PackageError(f"Expected Boolean at {label}")
        return value
    if isinstance(kind, (BigInteger, Integer)):
        if type(value) is not int:
            raise PackageError(f"Expected integer at {label}")
        return value
    if isinstance(kind, Text):
        if not isinstance(value, str):
            raise PackageError(f"Expected string at {label}")
        return value
    raise PackageError(f"Unsupported database column type at {label}")


def _decode_tables(raw: object) -> dict[str, list[dict[str, object]]]:
    if not isinstance(raw, dict) or set(raw) != set(TABLES):
        raise PackageError("Export must contain exactly the four transcript tables")
    decoded: dict[str, list[dict[str, object]]] = {}
    for table_name, table in TABLES.items():
        rows = raw[table_name]
        if not isinstance(rows, list):
            raise PackageError(f"Invalid {table_name} rows")
        columns = {column.name: column for column in table.columns}
        decoded_rows: list[dict[str, object]] = []
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) != set(columns):
                raise PackageError(
                    f"{table_name}[{index}] fields differ from this project's schema"
                )
            decoded_rows.append(
                {
                    name: _decode_value(
                        column, row[name], f"{table_name}[{index}].{name}"
                    )
                    for name, column in columns.items()
                }
            )
        decoded[table_name] = decoded_rows
    return decoded


def _validate_snapshot(
    manifest: dict[str, object],
    payload: dict[str, object],
    tables: dict[str, list[dict[str, object]]],
) -> tuple[UUID, dict[str, int]]:
    if payload.get("format") != "wateragents-run-database-records-v1":
        raise PackageError("Unsupported database-record format")
    run_id = _parse_uuid(payload.get("run_id"), "payload.run_id")
    if manifest.get("run_id") != str(run_id):
        raise PackageError("Manifest and records name different runs")
    runs = tables["analysis_runs"]
    sessions = tables["analysis_sessions"]
    items = tables["transcript_items"]
    contents = tables["transcript_contents"]
    if len(runs) != 1 or runs[0]["run_id"] != run_id:
        raise PackageError("Export must contain one matching run")
    if runs[0]["status"] != "completed" or manifest.get("run_status") != "completed":
        raise PackageError("Only a completed run can be imported")
    if manifest.get("query") != runs[0]["query"]:
        raise PackageError("Manifest query differs from the run")
    if any(row["run_id"] != run_id for row in sessions + items):
        raise PackageError("A session or transcript item belongs to another run")
    sessions_by_id = {row["session_id"]: row for row in sessions}
    items_by_id = {row["item_id"]: row for row in items}
    contents_by_id = {row["item_id"]: row for row in contents}
    if len(sessions_by_id) != len(sessions) or len(items_by_id) != len(items):
        raise PackageError("Duplicate session or transcript item ID")
    if len(contents_by_id) != len(contents):
        raise PackageError("Duplicate external content ID")
    if len({row["transcript_sequence"] for row in items}) != len(items):
        raise PackageError("Duplicate transcript sequence")
    if any(
        row["session_id"] not in sessions_by_id for row in items if row["session_id"]
    ):
        raise PackageError("Transcript item references a missing session")
    if set(contents_by_id) - set(items_by_id):
        raise PackageError("External content references a missing item")

    for key in ("request_item_id", "created_item_id", "terminal_item_id"):
        pointer = runs[0][key]
        if pointer not in items_by_id or items_by_id[pointer]["session_id"] is not None:
            raise PackageError(f"Run {key} does not point to a run-level item")
    for session in sessions:
        for key in ("created_item_id", "terminal_item_id"):
            pointer = session[key]
            if (
                pointer not in items_by_id
                or items_by_id[pointer]["session_id"] != session["session_id"]
            ):
                raise PackageError(f"Session {key} points outside its session")

    registry = create_production_registry()
    referenced: set[UUID] = set()
    body_hashes: dict[str, str] = {}
    for row in items:
        item_id = row["item_id"]
        content = contents_by_id.get(item_id)
        if row["inline_body"] is None:
            if content is None or content["created_at"] != row["created_at"]:
                raise PackageError(f"Missing or mismatched external body: {item_id}")
            body = content["body"]
        else:
            if content is not None:
                raise PackageError(
                    f"Inline item has duplicate external body: {item_id}"
                )
            body = row["inline_body"]
        if len(body.encode("utf-8")) != row["content_bytes"]:
            raise PackageError(f"Body length mismatch: {item_id}")
        body_hashes[str(item_id)] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        parsed = registry.parse(StoredTranscriptItem.model_validate(row), body)
        if parsed.parse_status.value != "ok":
            raise PackageError(f"Transcript body cannot be parsed: {item_id}")
        referenced.update(registry.references_from_read(parsed))
    if referenced - set(items_by_id):
        raise PackageError("Transcript references missing items")

    validation = manifest.get("validation")
    if not isinstance(validation, dict):
        raise PackageError("Manifest has no validation summary")
    expected_counts = {
        "session_count": len(sessions),
        "item_count": len(items),
        "external_content_count": len(contents),
        "typed_reference_count": len(referenced),
    }
    for key, count in expected_counts.items():
        if validation.get(key) != count:
            raise PackageError(f"Manifest {key} does not match the records")
    if validation.get("parse_statuses") != {"ok": len(items)}:
        raise PackageError("Manifest transcript parse status differs from the records")
    if validation.get("body_sha256_by_item_id") != body_hashes:
        raise PackageError("Manifest transcript body hashes differ from the records")
    if (
        validation.get("all_body_lengths_match") is not True
        or validation.get("all_references_present") is not True
    ):
        raise PackageError("Manifest validation did not pass")
    return run_id, expected_counts


def verify_package(
    package_dir: Path,
) -> tuple[UUID, dict[str, list[dict[str, object]]], dict[str, int]]:
    manifest = _read_json(package_dir / "manifest.json")
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != "wateragents-runtime-test-export-v1"
    ):
        raise PackageError("Unsupported or missing manifest format")
    hashes = manifest.get("files_sha256")
    if not isinstance(hashes, dict) or "run-database-records.json" not in hashes:
        raise PackageError("Manifest omits the database records checksum")
    for filename, expected in hashes.items():
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or filename in {"", ".", "..", "manifest.json"}
            or not isinstance(expected, str)
            or HEX_SHA256.fullmatch(expected) is None
        ):
            raise PackageError("Manifest contains an invalid file checksum entry")
        actual = hashlib.sha256((package_dir / filename).read_bytes()).hexdigest()
        if actual != expected:
            raise PackageError(f"SHA-256 mismatch: {filename}")
    payload = _read_json(package_dir / "run-database-records.json")
    if not isinstance(payload, dict):
        raise PackageError("Invalid database records file")
    tables = _decode_tables(payload.get("tables"))
    run_id, counts = _validate_snapshot(manifest, payload, tables)
    return run_id, tables, counts


async def import_run(
    run_id: UUID,
    tables: dict[str, list[dict[str, object]]],
    *,
    database_url_override: str | None,
) -> None:
    settings = load_app_settings()
    if database_url_override is None:
        engine = create_database_engine(settings)
    else:
        url = make_url(database_url_override)
        if url.drivername != "postgresql+psycopg" or not url.database:
            raise PackageError(
                "--database-url requires a postgresql+psycopg URL with a database"
            )
        engine = create_async_engine(url, pool_pre_ping=True)
    try:
        await check_database_schema(settings, engine=engine)
        async with engine.begin() as connection:
            existing = await connection.scalar(
                select(analysis_runs.c.run_id).where(analysis_runs.c.run_id == run_id)
            )
            if existing is not None:
                raise PackageError(
                    "This run ID already exists in the destination database"
                )
            for name, table in TABLES.items():
                rows = tables[name]
                if rows:
                    await connection.execute(table.insert(), rows)
            # Check circular lifecycle foreign keys before committing.
            await connection.exec_driver_sql("SET CONSTRAINTS ALL IMMEDIATE")
            for name, table in TABLES.items():
                if name == "transcript_contents":
                    statement = (
                        select(func.count())
                        .select_from(
                            table.join(
                                transcript_items,
                                table.c.item_id == transcript_items.c.item_id,
                            )
                        )
                        .where(transcript_items.c.run_id == run_id)
                    )
                else:
                    statement = (
                        select(func.count())
                        .select_from(table)
                        .where(table.c.run_id == run_id)
                    )
                if await connection.scalar(statement) != len(tables[name]):
                    raise PackageError(f"Database readback count differs for {name}")
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--package",
        type=Path,
        default=PACKAGE_DIR,
        help="directory holding manifest.json",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="validate files without connecting to a database",
    )
    parser.add_argument(
        "--database-url", help="optional isolated postgresql+psycopg target URL"
    )
    args = parser.parse_args()
    try:
        run_id, tables, counts = verify_package(args.package)
        print(
            f"Verified {run_id}: {counts['session_count']} sessions, "
            f"{counts['item_count']} transcript items, "
            f"{counts['external_content_count']} external bodies, "
            f"{counts['typed_reference_count']} typed references"
        )
        if args.check_only:
            return 0
        configure_asyncio_event_loop()
        asyncio.run(import_run(run_id, tables, database_url_override=args.database_url))
        print(f"Imported {run_id} into the configured database")
        return 0
    except (
        PackageError,
        SettingsConfigurationError,
        DatabaseConfigurationError,
        DatabaseConnectionError,
        SchemaVersionError,
        OSError,
    ) as error:
        print(f"Import failed: {error}", file=sys.stderr)
        return 1
    except SQLAlchemyError:
        print(
            "Import failed: database rejected the records; no rows were committed",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

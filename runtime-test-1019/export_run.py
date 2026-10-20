"""Archive one completed runtime run from the production transcript database."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.application.transcripts.models import StoredTranscriptItem
from app.application.transcripts.registry import create_production_registry
from app.infrastructure.config.settings import load_app_settings
from app.infrastructure.persistence.database import create_database_engine
from app.infrastructure.persistence.schema import (
    analysis_runs,
    analysis_sessions,
    transcript_contents,
    transcript_items,
)
from app.infrastructure.runtime.event_loop import windows_selector_loop_factory

RUN_ID = UUID("12ccd705-d5be-4de9-8f53-0e04a358cb11")
OUTPUT_DIR = Path(__file__).resolve().parent


def _json_value(value: object) -> object:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, bytes | memoryview):
        return {"base64": base64.b64encode(bytes(value)).decode("ascii")}
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _write_json(path: Path, value: object) -> str:
    encoded = (
        json.dumps(value, default=_json_value, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(encoded)
    os.replace(temporary, path)
    return hashlib.sha256(encoded).hexdigest()


async def _snapshot() -> dict[str, list[dict[str, object]]]:
    settings = load_app_settings()
    engine = create_database_engine(settings)
    try:
        async with engine.connect() as connection, connection.begin():
            await connection.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            run_rows = (
                (
                    await connection.execute(
                        select(analysis_runs).where(analysis_runs.c.run_id == RUN_ID)
                    )
                )
                .mappings()
                .all()
            )
            session_rows = (
                (
                    await connection.execute(
                        select(analysis_sessions)
                        .where(analysis_sessions.c.run_id == RUN_ID)
                        .order_by(
                            analysis_sessions.c.created_at,
                            analysis_sessions.c.session_id,
                        )
                    )
                )
                .mappings()
                .all()
            )
            item_rows = (
                (
                    await connection.execute(
                        select(transcript_items)
                        .where(transcript_items.c.run_id == RUN_ID)
                        .order_by(transcript_items.c.transcript_sequence)
                    )
                )
                .mappings()
                .all()
            )
            content_rows = (
                (
                    await connection.execute(
                        select(transcript_contents)
                        .join(
                            transcript_items,
                            transcript_contents.c.item_id == transcript_items.c.item_id,
                        )
                        .where(transcript_items.c.run_id == RUN_ID)
                        .order_by(transcript_items.c.transcript_sequence)
                    )
                )
                .mappings()
                .all()
            )
        return {
            "analysis_runs": [dict(row) for row in run_rows],
            "analysis_sessions": [dict(row) for row in session_rows],
            "transcript_items": [dict(row) for row in item_rows],
            "transcript_contents": [dict(row) for row in content_rows],
        }
    finally:
        await engine.dispose()


def _validate(snapshot: dict[str, list[dict[str, object]]]) -> dict[str, object]:
    runs = snapshot["analysis_runs"]
    sessions = snapshot["analysis_sessions"]
    items = snapshot["transcript_items"]
    contents = snapshot["transcript_contents"]
    if len(runs) != 1 or runs[0]["status"] != "completed":
        raise RuntimeError("The requested run is absent or not completed")
    item_by_id = {row["item_id"]: row for row in items}
    content_by_id = {row["item_id"]: row["body"] for row in contents}
    if len(item_by_id) != len(items) or len(content_by_id) != len(contents):
        raise RuntimeError("Duplicate transcript item or content IDs")
    if len({row["transcript_sequence"] for row in items}) != len(items):
        raise RuntimeError("Duplicate transcript sequence")
    registry = create_production_registry()
    parse_statuses: Counter[str] = Counter()
    referenced_ids: set[UUID] = set()
    body_hashes: dict[str, str] = {}
    for row in items:
        if row["run_id"] != RUN_ID:
            raise RuntimeError("Transcript item belongs to another run")
        item_id = row["item_id"]
        body = (
            row["inline_body"]
            if row["inline_body"] is not None
            else content_by_id.get(item_id)
        )
        if body is None or len(body.encode("utf-8")) != row["content_bytes"]:
            raise RuntimeError(f"Missing or truncated transcript content: {item_id}")
        if (row["inline_body"] is None) != (item_id in content_by_id):
            raise RuntimeError(f"Invalid inline/external content placement: {item_id}")
        body_hashes[str(item_id)] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        parsed = registry.parse(StoredTranscriptItem.model_validate(row), body)
        parse_statuses[parsed.parse_status.value] += 1
        if parsed.parse_status.value == "ok":
            referenced_ids.update(registry.references_from_read(parsed))
    missing = sorted(str(item_id) for item_id in referenced_ids - item_by_id.keys())
    root_ids = [
        runs[0][key]
        for key in ("request_item_id", "created_item_id", "terminal_item_id")
    ]
    root_ids.extend(
        row[key] for row in sessions for key in ("created_item_id", "terminal_item_id")
    )
    missing.extend(
        str(item_id)
        for item_id in root_ids
        if item_id is not None and item_id not in item_by_id
    )
    if missing or parse_statuses.get("corrupt") or parse_statuses.get("unsupported"):
        raise RuntimeError(
            f"Transcript validation failed: missing={missing}, parse_statuses={dict(parse_statuses)}"
        )
    return {
        "session_count": len(sessions),
        "item_count": len(items),
        "external_content_count": len(contents),
        "typed_reference_count": len(referenced_ids),
        "parse_statuses": dict(parse_statuses),
        "all_body_lengths_match": True,
        "all_references_present": True,
        "body_sha256_by_item_id": body_hashes,
    }


async def main() -> None:
    snapshot = await _snapshot()
    validation = _validate(snapshot)
    run = snapshot["analysis_runs"][0]
    result_types = {"inference_result", "topology_run_result", "run_summary_result"}
    contents = {row["item_id"]: row["body"] for row in snapshot["transcript_contents"]}
    results = [
        {
            "item_id": str(item["item_id"]),
            "transcript_sequence": item["transcript_sequence"],
            "content_type": item["content_type"],
            "content": json.loads(
                item["inline_body"]
                if item["inline_body"] is not None
                else contents[item["item_id"]]
            ),
        }
        for item in snapshot["transcript_items"]
        if item["content_type"] in result_types
    ]
    raw_sha = _write_json(
        OUTPUT_DIR / "run-database-records.json",
        {
            "format": "wateragents-run-database-records-v1",
            "run_id": str(RUN_ID),
            "tables": snapshot,
        },
    )
    result_sha = _write_json(OUTPUT_DIR / "results.json", results)
    _write_json(
        OUTPUT_DIR / "manifest.json",
        {
            "format": "wateragents-runtime-test-export-v1",
            "run_id": str(RUN_ID),
            "exported_at": datetime.now(UTC).isoformat(),
            "run_status": run["status"],
            "query": run["query"],
            "source": "production PostgreSQL read-only snapshot",
            "files_sha256": {
                "run-database-records.json": raw_sha,
                "results.json": result_sha,
            },
            "validation": validation,
        },
    )
    print(
        f"Exported {RUN_ID}: {validation['session_count']} sessions, "
        f"{validation['item_count']} items, "
        f"{validation['external_content_count']} external contents, "
        f"{len(results)} result items"
    )


if __name__ == "__main__":
    asyncio.run(
        main(),
        loop_factory=windows_selector_loop_factory if sys.platform == "win32" else None,
    )

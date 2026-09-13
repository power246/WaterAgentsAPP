from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.infrastructure.persistence.records import (
    PersistenceBatch,
    PersistedRunRecord,
    PersistedSessionRecord,
    PersistedTranscriptContentRecord,
    PersistedTranscriptItemRecord,
)

RUN_ID = UUID("10000000-0000-0000-0000-000000000001")
SESSION_ID = UUID("20000000-0000-0000-0000-000000000001")
REQUEST_ITEM_ID = UUID("30000000-0000-0000-0000-000000000001")
CREATED_ITEM_ID = UUID("30000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 9, 13, tzinfo=UTC)


def _item(
    item_id: UUID,
    sequence: int,
    body: str | None = "{}",
    *,
    session_id: UUID | None = None,
    content_bytes: int | None = None,
    content_type: str = "lifecycle_event",
) -> PersistedTranscriptItemRecord:
    return PersistedTranscriptItemRecord(
        item_id=item_id,
        run_id=RUN_ID,
        session_id=session_id,
        transcript_sequence=sequence,
        created_at=NOW,
        content_type=content_type,
        content_schema_version=2,
        inline_body=body,
        preview_head=None if body is not None else "head",
        preview_tail=None if body is not None else "tail",
        content_bytes=(
            content_bytes
            if content_bytes is not None
            else len(body.encode("utf-8")) if body is not None else 0
        ),
    )


def _run() -> PersistedRunRecord:
    return PersistedRunRecord(
        run_id=RUN_ID,
        status="pending",
        created_at=NOW,
        updated_at=NOW,
        query="lake level",
        creation_receipt={"run_id": str(RUN_ID)},
        request_item_id=REQUEST_ITEM_ID,
        created_item_id=CREATED_ITEM_ID,
    )


def test_valid_inline_creation_batch() -> None:
    batch = PersistenceBatch(
        runs=(_run(),),
        items=(
            _item(REQUEST_ITEM_ID, 1, content_type="analysis_request"),
            _item(CREATED_ITEM_ID, 2),
        ),
    )
    assert len(batch.items) == 2


def test_inline_body_byte_count_uses_utf8() -> None:
    with pytest.raises(ValidationError, match="UTF-8 body size"):
        _item(REQUEST_ITEM_ID, 1, "湖", content_bytes=1)


def test_external_content_requires_one_matching_row() -> None:
    external = _item(REQUEST_ITEM_ID, 1, None, content_bytes=3)
    with pytest.raises(ValidationError, match="requires transcript_contents"):
        PersistenceBatch(items=(external,))

    content = PersistedTranscriptContentRecord(
        item_id=REQUEST_ITEM_ID,
        body="lake",
        created_at=NOW,
    )
    with pytest.raises(ValidationError, match="UTF-8 body size"):
        PersistenceBatch(items=(external,), contents=(content,))


def test_inline_content_forbids_external_row() -> None:
    inline = _item(REQUEST_ITEM_ID, 1)
    content = PersistedTranscriptContentRecord(
        item_id=REQUEST_ITEM_ID,
        body="{}",
        created_at=NOW,
    )
    with pytest.raises(ValidationError, match="must not have"):
        PersistenceBatch(items=(inline,), contents=(content,))


def test_external_content_time_and_duplicates_are_rejected() -> None:
    external = _item(REQUEST_ITEM_ID, 1, None, content_bytes=2)
    mismatched = PersistedTranscriptContentRecord(
        item_id=REQUEST_ITEM_ID,
        body="{}",
        created_at=datetime(2026, 9, 14, tzinfo=UTC),
    )
    with pytest.raises(ValidationError, match="created_at"):
        PersistenceBatch(items=(external,), contents=(mismatched,))

    matching = mismatched.model_copy(update={"created_at": NOW})
    with pytest.raises(ValidationError, match="duplicate item_id"):
        PersistenceBatch(items=(external,), contents=(matching, matching))


def test_root_and_session_lifecycle_references_have_exact_scope() -> None:
    wrong_scope = _item(REQUEST_ITEM_ID, 1, session_id=SESSION_ID)
    with pytest.raises(ValidationError, match="expected run/session scope"):
        PersistenceBatch(
            runs=(_run(),),
            items=(
                wrong_scope.model_copy(update={"content_type": "analysis_request"}),
                _item(CREATED_ITEM_ID, 2),
            ),
        )

    session = PersistedSessionRecord(
        session_id=SESSION_ID,
        run_id=RUN_ID,
        agent_role="query_parser",
        status="pending",
        created_at=NOW,
        updated_at=NOW,
        created_item_id=CREATED_ITEM_ID,
    )
    with pytest.raises(ValidationError, match="expected run/session scope"):
        PersistenceBatch(
            sessions=(session,),
            items=(_item(CREATED_ITEM_ID, 1),),
        )


def test_terminal_reference_and_role_rules_fail_early() -> None:
    with pytest.raises(ValidationError, match="terminal_item_id"):
        PersistedRunRecord.model_validate(
            {**_run().model_dump(mode="python"), "status": "completed"}
        )
    with pytest.raises(ValidationError, match="target_comid"):
        PersistedSessionRecord(
            session_id=SESSION_ID,
            run_id=RUN_ID,
            agent_role="waterbody",
            status="pending",
            created_at=NOW,
            updated_at=NOW,
            created_item_id=CREATED_ITEM_ID,
        )


def test_duplicate_row_identity_is_rejected() -> None:
    item = _item(REQUEST_ITEM_ID, 1)
    with pytest.raises(ValidationError, match="duplicate item_id"):
        PersistenceBatch(items=(item, item))

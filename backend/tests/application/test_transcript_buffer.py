from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.application.analysis_runs.models import RunFailedEvent, TranscriptItemIdentity
from app.application.transcripts.buffer import (
    PendingTranscriptReferencesError,
    TranscriptBuffer,
    locate_content,
)
from app.application.transcripts.models import TranscriptContentRef
from app.application.transcripts.registry import (
    create_production_registry,
    fixture_message_registration,
)

RUN_ID = UUID("10000000-0000-0000-0000-000000000001")
ITEM_ID = UUID("30000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 9, 13, tzinfo=UTC)


def _identity(
    item_id: UUID,
    sequence: int,
    *,
    session_id: UUID | None = None,
) -> TranscriptItemIdentity:
    return TranscriptItemIdentity(
        item_id=item_id,
        run_id=RUN_ID,
        session_id=session_id,
        transcript_sequence=sequence,
        created_at=NOW,
    )


@pytest.mark.parametrize(
    ("size", "inline"),
    [(65535, True), (65536, True), (65537, False)],
)
def test_default_inline_boundary_is_exact_utf8_bytes(size: int, inline: bool) -> None:
    location = locate_content("x" * size)

    assert (location.inline_body is not None) is inline


def test_preview_limits_are_customizable_and_unicode_safe() -> None:
    body = "水" * 20
    location = locate_content(body, inline_content_bytes=10, preview_bytes=10)

    assert location.inline_body is None
    assert len(location.preview_head.encode("utf-8")) <= 10
    assert len(location.preview_tail.encode("utf-8")) <= 10
    assert location.preview_head == "水" * 3
    assert location.preview_tail == "水" * 3
    assert "�" not in location.preview_head + location.preview_tail

    default = locate_content("水" * 30000, inline_content_bytes=1)
    assert len(default.preview_head.encode("utf-8")) <= 1024
    assert len(default.preview_tail.encode("utf-8")) <= 1024
    assert len((default.preview_head + default.preview_tail).encode("utf-8")) <= 2048


def test_identical_text_remains_two_immutable_facts_with_distinct_ids() -> None:
    registry = create_production_registry()
    buffer = TranscriptBuffer(registry)
    first = buffer.append(_identity(ITEM_ID, 1), "analysis_request", 1, {"query": "lake"})
    second = buffer.append(
        _identity(UUID(int=22), 2), "analysis_request", 1, {"query": "lake"}
    )

    assert first.item_id != second.item_id
    assert first.content.safe_body == second.content.safe_body
    assert len(buffer.items) == 2
    with pytest.raises(ValueError, match="identity"):
        buffer.append(_identity(ITEM_ID, 3), "analysis_request", 1, {"query": "lake"})


def test_external_body_is_stored_once_and_full_content_ref_uses_run_and_item() -> None:
    registry = create_production_registry()
    buffer = TranscriptBuffer(registry)
    item = buffer.append(
        _identity(ITEM_ID, 1),
        "analysis_request",
        1,
        {"query": "水" * 20},
    )
    batch = buffer.to_storage_batch(inline_content_bytes=10, preview_bytes=7)

    assert item.content_ref.run_id == RUN_ID
    assert item.content_ref.item_id == ITEM_ID
    assert batch.items[0].inline_body is None
    assert len(batch.contents) == 1
    assert batch.contents[0].body == item.content.safe_body
    assert len(batch.items[0].preview_head.encode("utf-8")) <= 7
    assert len(batch.items[0].preview_tail.encode("utf-8")) <= 7


def test_redaction_happens_before_live_and_is_rechecked_for_storage() -> None:
    secret = "synthetic-secret-value"
    registry = create_production_registry()
    buffer = TranscriptBuffer(registry)
    item = buffer.append(
        _identity(ITEM_ID, 1),
        "analysis_request",
        1,
        {"query": f"inspect {secret}"},
        private_values=(secret,),
    )

    assert secret not in item.content.safe_body
    assert "[REDACTED]" in item.content.safe_body
    batch = buffer.to_storage_batch(private_values=(secret,))
    assert batch.items[0].inline_body == item.content.safe_body
    with pytest.raises(ValueError, match="differs from the readable live body"):
        buffer.to_storage_batch(private_values=("inspect",))


def test_reference_targets_must_be_persisted_same_batch_or_waited_for() -> None:
    registry = create_production_registry()
    buffer = TranscriptBuffer(registry)
    buffer.append(
        _identity(ITEM_ID, 1),
        "runtime_fact",
        1,
        {
            "fact": "late_message_output",
            "original_item_id": str(UUID(int=99)),
            "text": "late",
            "end_reason": "cancelled",
        },
    )
    target = TranscriptContentRef(run_id=RUN_ID, item_id=UUID(int=99))

    with pytest.raises(ValueError, match="does not exist"):
        buffer.to_storage_batch()
    with pytest.raises(PendingTranscriptReferencesError) as pending:
        buffer.to_storage_batch(generating=(target,))
    assert pending.value.resolution.waiting_for == (target,)
    assert buffer.to_storage_batch(persisted=(target,)).items


def test_same_batch_reference_is_valid_and_target_body_is_not_copied() -> None:
    registry = create_production_registry()
    buffer = TranscriptBuffer(registry)
    target_id = UUID(int=101)
    buffer.append(
        _identity(target_id, 1, session_id=UUID(int=201)),
        "analysis_request",
        1,
        {"query": "水" * 20},
    )
    buffer.append(
        _identity(ITEM_ID, 2, session_id=UUID(int=202)),
        "runtime_fact",
        1,
        {
            "fact": "late_message_output",
            "original_item_id": str(target_id),
            "text": "tail",
            "end_reason": "cancelled",
        },
    )

    batch = buffer.to_storage_batch(inline_content_bytes=10)
    assert len(batch.items) == 2
    assert [content.item_id for content in batch.contents].count(target_id) == 1


def test_generating_fixture_message_is_live_only() -> None:
    registry = create_production_registry().copy_with(fixture_message_registration())
    buffer = TranscriptBuffer(registry)
    buffer.append(
        _identity(ITEM_ID, 1),
        "fixture_message",
        1,
        {"text": "partial", "generation_status": "generating"},
    )

    with pytest.raises(ValueError, match="live-only"):
        buffer.to_storage_batch()


def test_error_already_embedded_in_lifecycle_is_not_copied() -> None:
    registry = create_production_registry()
    buffer = TranscriptBuffer(registry)
    error = {
        "error_id": str(UUID(int=77)),
        "code": "workflow_failed",
        "message": "The workflow failed.",
        "retryable": False,
    }
    event = RunFailedEvent(
        run_id=RUN_ID,
        sequence=2,
        occurred_at=NOW,
        payload={"failed_at": NOW, "error": error},
    )
    buffer.append(_identity(ITEM_ID, 1), "lifecycle_event", 2, event)

    with pytest.raises(ValueError, match="must not be copied"):
        buffer.append(_identity(UUID(int=78), 2), "error", 1, error)
    assert len(buffer.items) == 1

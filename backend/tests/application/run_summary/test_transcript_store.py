import asyncio
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid5
from types import SimpleNamespace

from app.application.analysis_runs.models import (
    SessionResultReference,
    TranscriptItemIdentity,
)
from app.application.run_summary.runner import RunSummaryRunStatus, RunSummaryRunner
from app.application.run_summary.transcript_store import RunSummaryTranscriptStore
from tests.application.run_summary.support import (
    MODEL,
    NOW,
    RUN_ID,
    SUMMARY_SESSION_ID,
    TARGET_SOURCE,
    FakeSummaryModelPort,
    draft_payload,
    summary_input,
)


class RecordingOperations:
    def __init__(self) -> None:
        self.next_sequence = 1
        self.items = []
        self.failed = []
        self.cancelled = []

    def _identity(self, *, operation_id=None):
        sequence = self.next_sequence
        self.next_sequence += 1
        return TranscriptItemIdentity(
            item_id=uuid5(NAMESPACE_URL, f"summary-item:{sequence}"),
            run_id=RUN_ID,
            session_id=SUMMARY_SESSION_ID,
            transcript_sequence=sequence,
            created_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
            operation_id=operation_id,
        )

    async def reserve_transcript_item(
        self,
        run_id,
        *,
        session_id=None,
        request_id=None,
        operation_id=None,
    ):
        assert run_id == RUN_ID
        assert session_id == SUMMARY_SESSION_ID
        return self._identity(operation_id=operation_id)

    async def finalize_transcript_item(
        self,
        run_id,
        item_id,
        content_type,
        content_schema_version,
        body,
        *,
        private_values=(),
    ):
        self.items.append((content_type, body, item_id))

    async def fail_agent_session(self, run_id, session_id, error):
        self.failed.append(error)
        return error

    async def cancel_agent_session(self, run_id, session_id):
        self.cancelled.append(session_id)
        return session_id

    async def complete_run_summary_agent_session(
        self,
        run_id,
        session_id,
        result,
        *,
        message,
    ):
        message_identity = self._identity(operation_id=message.operation_id)
        self.items.append(
            ("run_summary_agent_message", message, message_identity.item_id)
        )
        identity = self._identity()
        self.items.append(("run_summary_result", result, identity.item_id))
        return SimpleNamespace(result_ref=SessionResultReference(
            run_id=run_id,
            item_id=identity.item_id,
        ))


def execute(outputs):
    operations = RecordingOperations()
    store = RunSummaryTranscriptStore(
        operations=operations,
        run_id=RUN_ID,
        session_id=SUMMARY_SESSION_ID,
    )
    runner = RunSummaryRunner(
        model_port=FakeSummaryModelPort(outputs),
        transcript_store=store,
        operation_id_factory=lambda: UUID(
            "95a64a52-c1e2-4725-88ee-6c83f105e632"
        ),
    )
    outcome = asyncio.run(
        runner.run(summary_input(), model=MODEL, deadline=NOW)
    )
    return outcome, operations


def test_transcript_adapter_publishes_only_the_validated_summary_message() -> None:
    outcome, operations = execute([draft_payload()])

    assert outcome.status is RunSummaryRunStatus.COMPLETED
    assert [item[0] for item in operations.items] == [
        "run_summary_input",
        "run_summary_model_request",
        "run_summary_model_response",
        "run_summary_agent_message",
        "run_summary_result",
    ]
    response = operations.items[2][1]
    message = operations.items[3][1]
    assert response.visible_text == "A plain-language summary."
    assert response.message_refs == ()
    assert message.text == outcome.result.plain_language_summary
    assert message.message_id == "output-1"
    assert operations.failed == []


def test_failed_summary_saves_safe_failure_and_no_result_or_fallback() -> None:
    bad = draft_payload(source_ref=TARGET_SOURCE.model_copy(
        update={"item_id": UUID("a5a64a52-c1e2-4725-88ee-6c83f105e633")}
    ))
    outcome, operations = execute([bad, bad])

    assert outcome.status is RunSummaryRunStatus.FAILED
    assert len(operations.failed) == 1
    assert operations.failed[0].code == "run_summary_invalid"
    assert "run_summary_result" not in [item[0] for item in operations.items]
    assert "run_summary_agent_message" not in [item[0] for item in operations.items]
    assert all(
        getattr(body, "plain_language_summary", None) is None
        for _, body, _ in operations.items
    )

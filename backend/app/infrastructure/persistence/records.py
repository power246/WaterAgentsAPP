from typing import Literal, TypeVar
from uuid import UUID

from pydantic import field_validator, model_validator

from app.domain.common import JsonValue, NonEmptyString, UtcDatetime
from app.domain.domain_model import DomainModel

RunStatusValue = Literal[
    "pending",
    "running",
    "completed",
    "failed",
    "cancelled",
    "completed_with_persistence_error",
]
SessionStatusValue = Literal["pending", "running", "completed", "failed", "cancelled"]
AgentRoleValue = Literal["query_parser", "waterbody"]

_RUN_TERMINAL = {
    "completed",
    "failed",
    "cancelled",
    "completed_with_persistence_error",
}
_SESSION_TERMINAL = {"completed", "failed", "cancelled"}
RecordT = TypeVar("RecordT")


def _unique_by(records: tuple[RecordT, ...], attribute: str) -> dict[UUID, RecordT]:
    values: dict[UUID, RecordT] = {}
    for record in records:
        value = getattr(record, attribute)
        if value in values:
            raise ValueError(f"duplicate {attribute} in persistence batch")
        values[value] = record
    return values


class PersistedRunRecord(DomainModel):
    run_id: UUID
    status: RunStatusValue
    created_at: UtcDatetime
    updated_at: UtcDatetime
    query: str
    idempotency_key: UUID | None = None
    request_fingerprint: bytes | None = None
    creation_receipt: dict[str, JsonValue]
    request_item_id: UUID
    created_item_id: UUID
    terminal_item_id: UUID | None = None

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
        return value

    @model_validator(mode="after")
    def validate_run_invariants(self) -> "PersistedRunRecord":
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if (self.idempotency_key is None) != (self.request_fingerprint is None):
            raise ValueError("idempotency key and fingerprint must be provided together")
        if (
            self.request_fingerprint is not None
            and len(self.request_fingerprint) != 32
        ):
            raise ValueError("request_fingerprint must contain a SHA-256 digest")
        lifecycle_ids = {self.request_item_id, self.created_item_id}
        if self.terminal_item_id is not None:
            lifecycle_ids.add(self.terminal_item_id)
        if len(lifecycle_ids) != (3 if self.terminal_item_id is not None else 2):
            raise ValueError("run lifecycle item references must be distinct")
        if (self.status in _RUN_TERMINAL) != (self.terminal_item_id is not None):
            raise ValueError("terminal_item_id must match terminal run status")
        return self


class PersistedSessionRecord(DomainModel):
    session_id: UUID
    run_id: UUID
    agent_role: AgentRoleValue
    target_comid: NonEmptyString | None = None
    status: SessionStatusValue
    created_at: UtcDatetime
    updated_at: UtcDatetime
    created_item_id: UUID
    terminal_item_id: UUID | None = None

    @model_validator(mode="after")
    def validate_session_invariants(self) -> "PersistedSessionRecord":
        if self.agent_role == "query_parser" and self.target_comid is not None:
            raise ValueError("query_parser sessions must not have target_comid")
        if self.agent_role == "waterbody" and (
            self.target_comid is None or not self.target_comid.strip()
        ):
            raise ValueError("waterbody sessions require target_comid")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must not precede created_at")
        if self.terminal_item_id == self.created_item_id:
            raise ValueError("session lifecycle item references must be distinct")
        if (self.status in _SESSION_TERMINAL) != (
            self.terminal_item_id is not None
        ):
            raise ValueError("terminal_item_id must match terminal session status")
        return self


class PersistedTranscriptItemRecord(DomainModel):
    item_id: UUID
    run_id: UUID
    session_id: UUID | None = None
    transcript_sequence: int
    created_at: UtcDatetime
    content_type: str
    content_schema_version: int
    request_id: UUID | None = None
    operation_id: UUID | None = None
    tags: tuple[str, ...] = ()
    rag_eligible: bool = False
    inline_body: str | None = None
    preview_head: str | None = None
    preview_tail: str | None = None
    content_bytes: int

    @model_validator(mode="after")
    def validate_item_invariants(self) -> "PersistedTranscriptItemRecord":
        if self.transcript_sequence <= 0:
            raise ValueError("transcript_sequence must be positive")
        if self.content_schema_version <= 0:
            raise ValueError("content_schema_version must be positive")
        if not self.content_type.strip():
            raise ValueError("content_type must not be blank")
        if self.content_bytes < 0:
            raise ValueError("content_bytes must be non-negative")
        is_inline = self.inline_body is not None
        has_both_previews = self.preview_head is not None and self.preview_tail is not None
        if is_inline == has_both_previews:
            raise ValueError("item must contain either inline body or both previews")
        if not is_inline and (self.preview_head is None or self.preview_tail is None):
            raise ValueError("external item requires both previews")
        if is_inline and len(self.inline_body.encode("utf-8")) != self.content_bytes:
            raise ValueError("inline content_bytes must match UTF-8 body size")
        return self


class PersistedTranscriptContentRecord(DomainModel):
    item_id: UUID
    body: str
    created_at: UtcDatetime


class PersistenceBatch(DomainModel):
    runs: tuple[PersistedRunRecord, ...] = ()
    sessions: tuple[PersistedSessionRecord, ...] = ()
    items: tuple[PersistedTranscriptItemRecord, ...] = ()
    contents: tuple[PersistedTranscriptContentRecord, ...] = ()

    @model_validator(mode="after")
    def validate_atomic_batch(self) -> "PersistenceBatch":
        _unique_by(self.runs, "run_id")
        session_ids = _unique_by(self.sessions, "session_id")
        item_ids = _unique_by(self.items, "item_id")
        content_ids = _unique_by(self.contents, "item_id")

        for run in self.runs:
            self._validate_item_reference(
                run.request_item_id,
                run.run_id,
                None,
                "analysis_request",
                item_ids,
            )
            self._validate_item_reference(
                run.created_item_id,
                run.run_id,
                None,
                "lifecycle_event",
                item_ids,
            )
            if run.terminal_item_id is not None:
                self._validate_item_reference(
                    run.terminal_item_id,
                    run.run_id,
                    None,
                    "lifecycle_event",
                    item_ids,
                )

        for session in self.sessions:
            self._validate_item_reference(
                session.created_item_id,
                session.run_id,
                session.session_id,
                "lifecycle_event",
                item_ids,
            )
            if session.terminal_item_id is not None:
                self._validate_item_reference(
                    session.terminal_item_id,
                    session.run_id,
                    session.session_id,
                    "lifecycle_event",
                    item_ids,
                )

        for item in self.items:
            if item.session_id is not None and item.session_id in session_ids:
                session = session_ids[item.session_id]
                if session.run_id != item.run_id:
                    raise ValueError("item and session must belong to the same run")
            content = content_ids.get(item.item_id)
            if item.inline_body is not None:
                if content is not None:
                    raise ValueError("inline item must not have transcript_contents")
                continue
            if content is None:
                raise ValueError(
                    "external item requires transcript_contents in the same batch"
                )
            if content.created_at != item.created_at:
                raise ValueError("external content created_at must match its item")
            if len(content.body.encode("utf-8")) != item.content_bytes:
                raise ValueError("external content_bytes must match UTF-8 body size")

        unknown_content_ids = set(content_ids) - set(item_ids)
        if unknown_content_ids:
            raise ValueError("transcript_contents requires its item in the same batch")
        return self

    @staticmethod
    def _validate_item_reference(
        item_id: UUID,
        run_id: UUID,
        session_id: UUID | None,
        content_type: str,
        item_ids: dict[UUID, PersistedTranscriptItemRecord],
    ) -> None:
        item = item_ids.get(item_id)
        if item is None:
            raise ValueError("root lifecycle references must be present in the same batch")
        if item.run_id != run_id or item.session_id != session_id:
            raise ValueError("lifecycle reference must have the expected run/session scope")
        if item.content_type != content_type:
            raise ValueError(f"referenced item must have content_type {content_type}")

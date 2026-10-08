import base64
import binascii
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from pydantic import ValidationError

from app.application.analysis_runs.models import (
    AgentRole,
    RunCompletedEvent,
    RunCreatedEvent,
    SessionSummary,
    TopologyRunResultSummary,
)
from app.application.analysis_runs.topology_result_models import TopologyRunResult
from app.application.analysis_runs.persistence import PersistenceInfo
from app.application.query_understanding.context_models import AnalysisContext
from app.application.transcripts.buffer import locate_content
from app.application.transcripts.models import (
    ContentVisibility,
    ParseStatus,
    ReadTranscriptItem,
    StoredTranscriptContent,
    StoredTranscriptItem,
    TranscriptItem,
    TranscriptContentRef,
)
from app.application.transcripts.read_models import (
    AnalysisContextSummary,
    ContextSummaryDateRange,
    ContextSummaryLocation,
    ContextSummaryParameter,
    ContextSummaryTargetParameter,
    HistoryCursorPosition,
    InlineTranscriptContent,
    LiveRunRead,
    ReadIssue,
    ReadSession,
    ReadSource,
    ReadView,
    RegisteredRunRead,
    ReferencedTranscriptContent,
    RunDetailResponse,
    RunHistoryItem,
    RunHistoryResponse,
    RunStatusResponse,
    RunSummary,
    SessionDetailResponse,
    SessionTranscriptResponse,
    StoredRunHeader,
    StoredRunHistoryPage,
    StoredRunRead,
    TranscriptContentResponse,
    TranscriptCorrelation,
    TranscriptItemView,
    TranscriptScope,
    UnparsedTranscriptContent,
)
from app.application.transcripts.registry import TranscriptRegistry
from app.domain.common import normalize_utc_datetime
from app.ports.errors import PortError
from app.ports.transcript_store import TranscriptReadStorePort

_CURSOR_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_CURSOR_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)


class LiveTranscriptProvider(Protocol):
    async def get_live_read(self, run_id: UUID | str) -> LiveRunRead | None: ...


class InvalidHistoryCursorError(ValueError):
    pass


class TranscriptDatabaseUnavailableError(RuntimeError):
    pass


class TranscriptRunNotFoundError(LookupError):
    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id
        super().__init__("The run was not found.")


class TranscriptSessionNotFoundError(LookupError):
    def __init__(self, run_id: UUID, session_id: UUID) -> None:
        self.run_id = run_id
        self.session_id = session_id
        super().__init__("The Agent session was not found in the run.")


class TranscriptItemNotFoundError(LookupError):
    def __init__(self, run_id: UUID, item_id: UUID) -> None:
        self.run_id = run_id
        self.item_id = item_id
        super().__init__("The transcript item was not found in the run.")


class TranscriptContentUnreadableError(RuntimeError):
    def __init__(self, run_id: UUID, item_id: UUID) -> None:
        self.run_id = run_id
        self.item_id = item_id
        super().__init__("The transcript item's full content could not be read.")


class RunAlreadyTerminalError(RuntimeError):
    def __init__(self, run: RunStatusResponse) -> None:
        self.run = run
        super().__init__("The run is already terminal.")


class RunNotLiveError(RuntimeError):
    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id
        super().__init__("The run no longer has a live owner.")


class PersistenceRetryUnavailableError(RuntimeError):
    def __init__(self, run_id: UUID) -> None:
        self.run_id = run_id
        super().__init__("Persistence retry requires a live run owner.")


@dataclass(frozen=True, slots=True)
class _SelectedRun:
    stored: StoredRunRead
    source: ReadSource
    persistence: PersistenceInfo | None


class TranscriptReader:
    def __init__(
        self,
        live: LiveTranscriptProvider,
        store: TranscriptReadStorePort[StoredRunRead, StoredRunHistoryPage],
        registry: TranscriptRegistry,
        *,
        error_id_factory: Callable[[], UUID],
        inline_content_bytes: int,
        preview_bytes: int,
    ) -> None:
        self._live = live
        self._store = store
        self._registry = registry
        self._error_id_factory = error_id_factory
        self._inline_content_bytes = inline_content_bytes
        self._preview_bytes = preview_bytes

    async def list_runs(
        self,
        *,
        limit: int,
        cursor: str | None,
    ) -> RunHistoryResponse:
        position = decode_history_cursor(cursor) if cursor is not None else None
        try:
            page = await self._store.list_runs(
                limit + 1,
                position.created_at if position is not None else None,
                position.run_id if position is not None else None,
            )
        except (PortError, OSError, TimeoutError) as error:
            raise TranscriptDatabaseUnavailableError() from error
        selected = page.runs[:limit]
        items = tuple(self._history_item(run) for run in selected)
        next_cursor = None
        if len(page.runs) > limit and selected:
            last = selected[-1].run
            next_cursor = encode_history_cursor(
                HistoryCursorPosition(
                    created_at=last.created_at,
                    run_id=last.run_id,
                )
            )
        return RunHistoryResponse(items=items, next_cursor=next_cursor)

    async def get_run(self, run_id: UUID, view: ReadView) -> RunDetailResponse:
        selected = await self._select(run_id, view)
        parsed = self._parse_items(selected.stored)
        visible = self._visible_items(parsed, view)
        run_items = tuple(
            item for item in visible if item.item.session_id is None
        )
        referenced, reference_issues = self._reference_closure(
            run_items,
            parsed,
            view,
        )
        context_items = tuple(
            item
            for item in visible
            if item.item.content_type == "query_understood"
            and item.item.item_id not in {
                candidate.item.item_id for candidate in (*run_items, *referenced)
            }
        )
        referenced = tuple(sorted(
            (*referenced, *context_items),
            key=lambda item: item.item.transcript_sequence,
        ))
        summary, summary_issues = self._run_summary(selected.stored, parsed)
        returned = (*run_items, *referenced)
        issues = self._issues(
            returned,
            (*summary_issues, *reference_issues, *selected.stored.session_issues),
        )
        return RunDetailResponse(
            run=summary,
            sessions=selected.stored.sessions,
            items=tuple(
                self._item_view(item, selected.stored.sessions) for item in run_items
            ),
            referenced_items=tuple(
                self._item_view(item, selected.stored.sessions)
                for item in referenced
            ),
            source=selected.source,
            persistence=selected.persistence,
            issues=issues,
        )

    async def get_session(
        self,
        run_id: UUID,
        session_id: UUID,
        view: ReadView,
    ) -> SessionDetailResponse:
        selected = await self._select(run_id, view)
        session = self._session(selected.stored, session_id)
        return SessionDetailResponse(
            run_id=run_id,
            session=session,
            transcript_url=(
                f"/api/analysis/runs/{run_id}/sessions/{session_id}/transcript"
                f"?view={view.value}"
            ),
            source=selected.source,
            persistence=selected.persistence,
            issues=self._session_issues(selected.stored, session_id),
        )

    async def get_session_transcript(
        self,
        run_id: UUID,
        session_id: UUID,
        view: ReadView,
    ) -> SessionTranscriptResponse:
        selected = await self._select(run_id, view)
        self._session(selected.stored, session_id)
        parsed = self._parse_items(selected.stored)
        visible = self._visible_items(parsed, view)
        session_items = tuple(
            item for item in visible if item.item.session_id == session_id
        )
        referenced, reference_issues = self._reference_closure(
            session_items,
            parsed,
            view,
        )
        returned = (*session_items, *referenced)
        return SessionTranscriptResponse(
            run_id=run_id,
            session_id=session_id,
            items=tuple(
                self._item_view(item, selected.stored.sessions)
                for item in session_items
            ),
            referenced_items=tuple(
                self._item_view(item, selected.stored.sessions)
                for item in referenced
            ),
            source=selected.source,
            persistence=selected.persistence,
            issues=self._issues(
                returned,
                (*reference_issues, *self._session_issues(selected.stored, session_id)),
            ),
        )

    async def get_content(
        self,
        run_id: UUID,
        item_id: UUID,
        view: ReadView,
    ) -> TranscriptContentResponse:
        selected = await self._select(run_id, view)
        parsed = self._parse_items(selected.stored)
        item = next(
            (candidate for candidate in parsed if candidate.item.item_id == item_id),
            None,
        )
        if item is None or not self._is_visible(item, view):
            raise TranscriptItemNotFoundError(run_id, item_id)
        stored_item = next(
            candidate
            for candidate in selected.stored.items
            if candidate.item_id == item_id
        )
        checked = self._checked_body(selected.stored, stored_item)
        if checked is None:
            raise TranscriptContentUnreadableError(run_id, item_id)
        safe_item, text, _ = checked
        try:
            public_text = self._registry.public_content_body(
                safe_item.content_type,
                safe_item.content_schema_version,
                text,
            )
        except (ValidationError, TypeError, ValueError) as error:
            raise TranscriptContentUnreadableError(run_id, item_id) from error
        return TranscriptContentResponse(
            run_id=run_id,
            item_id=item_id,
            content_type=safe_item.content_type,
            content_schema_version=safe_item.content_schema_version,
            content_bytes=len(public_text.encode("utf-8")),
            text=public_text,
            source=selected.source,
            persistence=selected.persistence,
            issues=self._issues((item,), ()),
        )

    async def read_registered_item(
        self,
        run_id: UUID,
        item_id: UUID,
        view: ReadView,
        *,
        session_id: UUID | None = None,
    ) -> ReadTranscriptItem:
        selected = await self._select(run_id, view)
        if session_id is not None:
            self._session(selected.stored, session_id)
        stored_item = next(
            (
                candidate
                for candidate in sorted(
                    selected.stored.items,
                    key=lambda candidate: candidate.transcript_sequence,
                )
                if candidate.item_id == item_id
            ),
            None,
        )
        item = (
            self._parse_item(selected.stored, stored_item)
            if stored_item is not None
            else None
        )
        if (
            item is None
            or not self._is_visible(item, view)
            or (session_id is not None and item.item.session_id != session_id)
        ):
            raise TranscriptItemNotFoundError(run_id, item_id)
        if item.body is None:
            raise TranscriptContentUnreadableError(run_id, item_id)
        return item

    async def read_registered_run(
        self,
        run_id: UUID,
        view: ReadView,
    ) -> RegisteredRunRead:
        """Decode a persisted run once for bounded internal source hydration."""

        selected = await self._select(run_id, view)
        return RegisteredRunRead(
            run=selected.stored.run,
            sessions=selected.stored.sessions,
            items=self._visible_items(self._parse_items(selected.stored), view),
        )

    async def read_registered_session_items(
        self,
        run_id: UUID,
        session_id: UUID,
        view: ReadView,
    ) -> tuple[ReadTranscriptItem, ...]:
        selected = await self._select(run_id, view)
        self._session(selected.stored, session_id)
        return tuple(
            item
            for item in self._visible_items(
                self._parse_items(selected.stored),
                view,
            )
            if item.item.session_id == session_id and item.body is not None
        )

    async def get_historical_status(self, run_id: UUID) -> RunStatusResponse:
        selected = await self._select(run_id, ReadView.HISTORY)
        run = selected.stored.run
        return RunStatusResponse(
            run_id=run.run_id,
            status=run.status,
            created_at=run.created_at,
            updated_at=run.updated_at,
            persistence=None,
        )

    def present_live_item(
        self,
        item: TranscriptItem,
        session: SessionSummary | None,
    ) -> TranscriptItemView:
        location = locate_content(
            item.content.safe_body,
            preview_body=self._registry.public_safe_body(
                item.content.content_type,
                item.content.content_schema_version,
                item.content.safe_body,
            ),
            inline_content_bytes=self._inline_content_bytes,
            preview_bytes=self._preview_bytes,
        )
        stored = StoredTranscriptItem(
            item_id=item.item_id,
            run_id=item.run_id,
            session_id=item.session_id,
            transcript_sequence=item.transcript_sequence,
            created_at=item.created_at,
            request_id=item.request_id,
            operation_id=item.operation_id,
            content_type=item.content.content_type,
            content_schema_version=item.content.content_schema_version,
            tags=item.content.metadata.tags,
            rag_eligible=item.content.metadata.rag_eligible,
            inline_body=location.inline_body,
            preview_head=location.preview_head,
            preview_tail=location.preview_tail,
            content_bytes=item.content.content_bytes,
        )
        read = self._registry.parse(
            stored,
            item.content.safe_body,
            error_id_factory=self._error_id_factory,
        )
        return self._item_view(read, (session,) if session is not None else ())

    async def _select(self, run_id: UUID, view: ReadView) -> _SelectedRun:
        if view is ReadView.AUTO:
            live = await self._live.get_live_read(run_id)
            if live is not None:
                return _SelectedRun(
                    stored=self._stored_live(live),
                    source=ReadSource.LIVE,
                    persistence=live.persistence,
                )
        try:
            stored = await self._store.read_run(run_id)
        except (PortError, OSError, TimeoutError) as error:
            raise TranscriptDatabaseUnavailableError() from error
        if stored is None:
            raise TranscriptRunNotFoundError(run_id)
        return _SelectedRun(
            stored=stored,
            source=ReadSource.POSTGRESQL,
            persistence=None,
        )

    def _stored_live(self, live: LiveRunRead) -> StoredRunRead:
        stored_items: list[StoredTranscriptItem] = []
        contents: list[StoredTranscriptContent] = []
        for item in live.items:
            preview_body = (
                self._registry.public_safe_body(
                    item.content.content_type,
                    item.content.content_schema_version,
                    item.content.safe_body,
                )
                if item.content.content_bytes > self._inline_content_bytes
                else None
            )
            location = locate_content(
                item.content.safe_body,
                preview_body=preview_body,
                inline_content_bytes=self._inline_content_bytes,
                preview_bytes=self._preview_bytes,
            )
            stored_items.append(StoredTranscriptItem(
                item_id=item.item_id,
                run_id=item.run_id,
                session_id=item.session_id,
                transcript_sequence=item.transcript_sequence,
                created_at=item.created_at,
                request_id=item.request_id,
                operation_id=item.operation_id,
                content_type=item.content.content_type,
                content_schema_version=item.content.content_schema_version,
                tags=item.content.metadata.tags,
                rag_eligible=item.content.metadata.rag_eligible,
                inline_body=location.inline_body,
                preview_head=location.preview_head,
                preview_tail=location.preview_tail,
                content_bytes=item.content.content_bytes,
            ))
            if location.inline_body is None:
                contents.append(StoredTranscriptContent(
                    item_id=item.item_id,
                    body=item.content.safe_body,
                    created_at=item.created_at,
                ))
        request_item = next(
            item
            for item in stored_items
            if item.session_id is None and item.transcript_sequence == 1
        )
        created_item = next(
            item
            for item in stored_items
            if item.session_id is None and item.transcript_sequence == 2
        )
        terminal_items = tuple(
            item
            for item in stored_items
            if item.session_id is None
            and item.content_type == "lifecycle_event"
            and item.transcript_sequence > 2
        )
        return StoredRunRead(
            run=StoredRunHeader(
                run_id=live.run.run_id,
                status=live.run.status,
                created_at=live.run.created_at,
                updated_at=live.run.updated_at,
                query=live.query,
                request_item_id=request_item.item_id,
                created_item_id=created_item.item_id,
                terminal_item_id=(
                    terminal_items[-1].item_id if terminal_items else None
                ),
            ),
            sessions=live.sessions,
            items=tuple(stored_items),
            contents=tuple(contents),
        )

    def _history_item(self, stored: StoredRunRead) -> RunHistoryItem:
        parsed = self._parse_items(stored)
        summary, issues = self._run_summary(stored, parsed)
        return RunHistoryItem(
            **summary.model_dump(mode="python"),
            issues=self._issues((), (*issues, *stored.session_issues)),
        )

    def _run_summary(
        self,
        stored: StoredRunRead,
        parsed: tuple[ReadTranscriptItem, ...],
    ) -> tuple[RunSummary, tuple[ReadIssue, ...]]:
        created = next(
            (
                item
                for item in parsed
                if item.item.item_id == stored.run.created_item_id
            ),
            None,
        )
        configuration = None
        issues: list[ReadIssue] = []
        if created is not None and isinstance(created.body, RunCreatedEvent):
            configuration = created.body.payload.configuration
        elif created is not None and created.parse_error is not None:
            issues.append(ReadIssue(
                item_id=created.item.item_id,
                error=created.parse_error,
            ))
        else:
            item_id = (
                created.item.item_id
                if created is not None
                else stored.run.created_item_id
            )
            issues.append(ReadIssue(
                item_id=item_id,
                error=self._read_error(
                    "transcript_content_corrupt",
                    "The run configuration record is invalid.",
                ),
            ))
        context_summary, context_issues = self._context_summary(stored, parsed)
        issues.extend(context_issues)
        result_ref, result_summary, result_issues = self._result_summary(
            stored,
            parsed,
        )
        issues.extend(result_issues)
        return RunSummary(
            run_id=stored.run.run_id,
            status=stored.run.status,
            created_at=stored.run.created_at,
            updated_at=stored.run.updated_at,
            query=stored.run.query,
            configuration=configuration,
            context_summary=context_summary,
            result_ref=result_ref,
            result_summary=result_summary,
        ), tuple(issues)

    def _result_summary(
        self,
        stored: StoredRunRead,
        parsed: tuple[ReadTranscriptItem, ...],
    ) -> tuple[
        TranscriptContentRef | None,
        TopologyRunResultSummary | None,
        tuple[ReadIssue, ...],
    ]:
        terminal_id = stored.run.terminal_item_id
        if terminal_id is None:
            return None, None, ()
        terminal = next(
            (item for item in parsed if item.item.item_id == terminal_id),
            None,
        )
        if terminal is None or terminal.parse_error is not None:
            return None, None, (ReadIssue(
                item_id=terminal_id,
                error=(
                    terminal.parse_error
                    if terminal is not None and terminal.parse_error is not None
                    else self._read_error(
                        "transcript_content_corrupt",
                        "The run terminal record is unavailable.",
                    )
                ),
            ),)
        if not isinstance(terminal.body, RunCompletedEvent):
            return None, None, ()
        payload = terminal.body.payload
        if payload.result_ref is None or payload.result_summary is None:
            return None, None, ()
        result = next(
            (
                item
                for item in parsed
                if item.item.item_id == payload.result_ref.item_id
            ),
            None,
        )
        invalid = (
            result is None
            or result.parse_error is not None
            or result.item.session_id is not None
            or not isinstance(result.body, TopologyRunResult)
            or result.body.run_id != stored.run.run_id
        )
        if invalid:
            return None, None, (ReadIssue(
                item_id=payload.result_ref.item_id,
                error=(
                    result.parse_error
                    if result is not None and result.parse_error is not None
                    else self._read_error(
                        "transcript_content_corrupt",
                        "The topology run result is invalid.",
                    )
                ),
            ),)
        expected = TopologyRunResultSummary(
            target_comid=result.body.target_comid,
            target_result_summary=result.body.target_result_summary,
            graph_coverage=result.body.graph_coverage,
            outcome_counts=result.body.outcome_counts,
            summary_status=result.body.summary.status,
            summary_result_ref=result.body.summary.result_ref,
            summary_error=result.body.summary.public_error,
        )
        if expected != payload.result_summary:
            return None, None, (ReadIssue(
                item_id=terminal_id,
                error=self._read_error(
                    "transcript_content_corrupt",
                    "The run result projection does not match its stored result.",
                ),
            ),)
        return (
            TranscriptContentRef(
                run_id=payload.result_ref.run_id,
                item_id=payload.result_ref.item_id,
            ),
            expected,
            (),
        )

    def _context_summary(
        self,
        stored: StoredRunRead,
        parsed: tuple[ReadTranscriptItem, ...],
    ) -> tuple[AnalysisContextSummary | None, tuple[ReadIssue, ...]]:
        candidates = tuple(
            item
            for item in parsed
            if item.item.content_type == "query_understood"
        )
        if not candidates:
            return None, ()
        if len(candidates) != 1:
            return None, tuple(
                ReadIssue(
                    item_id=item.item.item_id,
                    error=self._read_error(
                        "transcript_content_corrupt",
                        "The run contains conflicting query-understanding records.",
                    ),
                )
                for item in candidates
            )

        candidate = candidates[0]
        session = next(
            (
                item
                for item in stored.sessions
                if item.session_id == candidate.item.session_id
            ),
            None,
        )
        if (
            candidate.item.session_id is None
            or session is None
            or session.agent_role is not AgentRole.QUERY_PARSER
        ):
            return None, (ReadIssue(
                item_id=candidate.item.item_id,
                error=self._read_error(
                    "transcript_content_corrupt",
                    "The query-understanding record has invalid ownership.",
                ),
            ),)
        if candidate.parse_error is not None:
            return None, (ReadIssue(
                item_id=candidate.item.item_id,
                error=candidate.parse_error,
            ),)
        if not isinstance(candidate.body, AnalysisContext):
            return None, (ReadIssue(
                item_id=candidate.item.item_id,
                error=self._read_error(
                    "transcript_content_corrupt",
                    "The query-understanding record is invalid.",
                ),
            ),)

        context = candidate.body
        return AnalysisContextSummary(
            target_comid=context.target.comid,
            location=ContextSummaryLocation(
                latitude=context.target.point.latitude,
                longitude=context.target.point.longitude,
                display_name=context.target.display_name,
            ),
            date_range=ContextSummaryDateRange(
                start_date=context.requested_date_range.start_date,
                end_date=context.requested_date_range.end_date,
            ),
            mode=context.mode,
            time_resolution=context.time_resolution,
            params_used=tuple(
                ContextSummaryTargetParameter(
                    code_system=item.key.code_system,
                    code=item.key.code,
                    canonical_name=item.canonical_name,
                    source=item.source,
                    rationale=item.rationale,
                )
                for item in context.params_used
            ),
            supporting_params=tuple(
                ContextSummaryParameter(
                    code_system=item.key.code_system,
                    code=item.key.code,
                    canonical_name=item.canonical_name,
                )
                for item in context.supporting_params
            ),
        ), ()

    def _parse_items(self, stored: StoredRunRead) -> tuple[ReadTranscriptItem, ...]:
        return tuple(
            self._parse_item(stored, item)
            for item in sorted(
                stored.items,
                key=lambda candidate: candidate.transcript_sequence,
            )
        )

    def _parse_item(
        self,
        stored: StoredRunRead,
        item: StoredTranscriptItem,
    ) -> ReadTranscriptItem:
        checked = self._checked_body(stored, item)
        if checked is None:
            return ReadTranscriptItem(
                item=item,
                content_ref={"run_id": item.run_id, "item_id": item.item_id},
                parse_status=ParseStatus.CORRUPT,
                parse_error=self._read_error(
                    "transcript_content_corrupt",
                    "This transcript item's full content is unavailable.",
                ),
            )
        safe_item, safe_body, stored_bytes_match = checked
        if not stored_bytes_match:
            return ReadTranscriptItem(
                item=safe_item,
                content_ref={"run_id": item.run_id, "item_id": item.item_id},
                parse_status=ParseStatus.CORRUPT,
                parse_error=self._read_error(
                    "transcript_content_corrupt",
                    "This transcript item contains invalid content.",
                ),
            )
        return self._registry.parse(
            safe_item,
            safe_body,
            error_id_factory=self._error_id_factory,
        )

    def _checked_body(
        self,
        stored: StoredRunRead,
        item: StoredTranscriptItem,
    ) -> tuple[StoredTranscriptItem, str, bool] | None:
        body = self._body(stored, item)
        if body is None:
            return None
        try:
            safe_body = self._registry.safe_read_body(
                item.content_type,
                item.content_schema_version,
                body,
            )
        except (TypeError, ValueError):
            return None
        return (
            self._safe_item(item, body, safe_body),
            safe_body,
            len(body.encode("utf-8")) == item.content_bytes,
        )

    def _safe_item(
        self,
        item: StoredTranscriptItem,
        original_body: str,
        safe_body: str,
    ) -> StoredTranscriptItem:
        safe_content_bytes = len(safe_body.encode("utf-8"))
        if safe_body == original_body and safe_content_bytes == item.content_bytes:
            return item
        try:
            preview_body = self._registry.public_safe_body(
                item.content_type,
                item.content_schema_version,
                safe_body,
            )
        except (ValidationError, TypeError, ValueError):
            preview_body = ""
        location = locate_content(
            safe_body,
            preview_body=preview_body,
            inline_content_bytes=self._inline_content_bytes,
            preview_bytes=self._preview_bytes,
        )
        return StoredTranscriptItem(
            item_id=item.item_id,
            run_id=item.run_id,
            session_id=item.session_id,
            transcript_sequence=item.transcript_sequence,
            created_at=item.created_at,
            content_type=item.content_type,
            content_schema_version=item.content_schema_version,
            request_id=item.request_id,
            operation_id=item.operation_id,
            tags=item.tags,
            rag_eligible=item.rag_eligible,
            inline_body=location.inline_body,
            preview_head=location.preview_head,
            preview_tail=location.preview_tail,
            content_bytes=safe_content_bytes,
        )

    @staticmethod
    def _body(stored: StoredRunRead, item: StoredTranscriptItem) -> str | None:
        if item.inline_body is not None:
            return item.inline_body
        content = next(
            (candidate for candidate in stored.contents if candidate.item_id == item.item_id),
            None,
        )
        return content.body if content is not None else None

    def _visible_items(
        self,
        parsed: tuple[ReadTranscriptItem, ...],
        view: ReadView,
    ) -> tuple[ReadTranscriptItem, ...]:
        return tuple(item for item in parsed if self._is_visible(item, view))

    def _is_visible(self, item: ReadTranscriptItem, view: ReadView) -> bool:
        return (
            view is ReadView.HISTORY
            or self._registry.visibility_from_read(item) is ContentVisibility.AUTO
        )

    def _reference_closure(
        self,
        roots: tuple[ReadTranscriptItem, ...],
        parsed: tuple[ReadTranscriptItem, ...],
        view: ReadView,
    ) -> tuple[tuple[ReadTranscriptItem, ...], tuple[ReadIssue, ...]]:
        by_id = {item.item.item_id: item for item in parsed}
        visited = {item.item.item_id for item in roots}
        queue = list(roots)
        referenced: list[ReadTranscriptItem] = []
        issues: list[ReadIssue] = []
        while queue:
            source = queue.pop(0)
            for item_id in self._registry.references_from_read(source):
                if item_id in visited:
                    continue
                visited.add(item_id)
                target = by_id.get(item_id)
                if target is None:
                    issues.append(ReadIssue(
                        item_id=source.item.item_id,
                        error=self._read_error(
                            "transcript_reference_unreadable",
                            "A referenced transcript item is unavailable.",
                        ),
                    ))
                    continue
                if not self._is_visible(target, view):
                    continue
                referenced.append(target)
                queue.append(target)
        referenced.sort(key=lambda item: item.item.transcript_sequence)
        return tuple(referenced), tuple(issues)

    def _item_view(
        self,
        read: ReadTranscriptItem,
        sessions: tuple[ReadSession, ...],
    ) -> TranscriptItemView:
        session = (
            next(
                item for item in sessions if item.session_id == read.item.session_id
            )
            if read.item.session_id is not None
            else None
        )
        metadata = self._registry.metadata_from_read(read)
        errors = self._registry.error_ids_from_read(read)
        if read.parse_status is ParseStatus.OK:
            if read.item.inline_body is not None:
                content = InlineTranscriptContent(
                    body=self._registry.public_body(read)
                )
            else:
                content = ReferencedTranscriptContent(
                    preview_head=read.item.preview_head or "",
                    preview_tail=read.item.preview_tail or "",
                )
        else:
            content = UnparsedTranscriptContent()
        return TranscriptItemView(
            item_id=read.item.item_id,
            run_id=read.item.run_id,
            transcript_sequence=read.item.transcript_sequence,
            occurred_at=read.item.created_at,
            content_type=read.item.content_type,
            content_schema_version=read.item.content_schema_version,
            scope=TranscriptScope(
                session_id=read.item.session_id,
                agent_role=session.agent_role if session is not None else None,
                target_comid=session.target_comid if session is not None else None,
            ),
            correlation=TranscriptCorrelation(
                request_id=read.item.request_id,
                operation_id=read.item.operation_id,
                error_id=errors[0] if errors else None,
            ),
            tags=read.item.tags,
            rag_eligible=read.item.rag_eligible,
            confidence=metadata.confidence,
            provenance=metadata.provenance,
            content_bytes=read.item.content_bytes,
            content_ref=read.content_ref,
            content=content,
            parse_status=read.parse_status,
            parse_error=read.parse_error,
        )

    def _issues(
        self,
        items: tuple[ReadTranscriptItem, ...],
        additional: tuple[ReadIssue, ...],
    ) -> tuple[ReadIssue, ...]:
        issues = list(additional)
        issues.extend(
            ReadIssue(item_id=item.item.item_id, error=item.parse_error)
            for item in items
            if item.parse_error is not None
        )
        unique: dict[tuple[UUID, str], ReadIssue] = {}
        for issue in issues:
            unique.setdefault((issue.item_id, issue.error.code), issue)
        return tuple(unique.values())

    @staticmethod
    def _session_issues(
        stored: StoredRunRead, session_id: UUID
    ) -> tuple[ReadIssue, ...]:
        item_ids = {
            item.item_id for item in stored.items if item.session_id == session_id
        }
        return tuple(
            issue for issue in stored.session_issues
            if issue.item_id in item_ids or issue.item_id == session_id
        )

    @staticmethod
    def _session(stored: StoredRunRead, session_id: UUID) -> ReadSession:
        session = next(
            (item for item in stored.sessions if item.session_id == session_id),
            None,
        )
        if session is None:
            raise TranscriptSessionNotFoundError(stored.run.run_id, session_id)
        return session

    def _read_error(self, code: str, message: str):
        from app.application.analysis_runs.models import PublicError

        return PublicError(
            error_id=self._error_id_factory(),
            code=code,
            message=message,
            failed_stage="transcript_read",
            retryable=False,
        )


def encode_history_cursor(position: HistoryCursorPosition) -> str:
    timestamp = normalize_utc_datetime(position.created_at).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ"
    )
    payload = json.dumps(
        {"v": 1, "created_at": timestamp, "run_id": str(position.run_id)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_history_cursor(value: str) -> HistoryCursorPosition:
    if not value or len(value) > 1024 or _CURSOR_PATTERN.fullmatch(value) is None:
        raise InvalidHistoryCursorError()
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.b64decode(
            value + padding,
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(decoded.decode("utf-8"))
        if (
            not isinstance(payload, dict)
            or set(payload) != {"v", "created_at", "run_id"}
            or type(payload["v"]) is not int
            or payload["v"] != 1
            or not isinstance(payload["created_at"], str)
            or _CURSOR_TIMESTAMP_PATTERN.fullmatch(payload["created_at"]) is None
            or not isinstance(payload["run_id"], str)
        ):
            raise ValueError
        created_at = datetime.strptime(
            payload["created_at"],
            "%Y-%m-%dT%H:%M:%S.%fZ",
        ).replace(tzinfo=UTC)
        run_id = UUID(payload["run_id"])
        if str(run_id) != payload["run_id"]:
            raise ValueError
        position = HistoryCursorPosition(created_at=created_at, run_id=run_id)
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as error:
        raise InvalidHistoryCursorError() from error
    if encode_history_cursor(position) != value:
        raise InvalidHistoryCursorError()
    return position

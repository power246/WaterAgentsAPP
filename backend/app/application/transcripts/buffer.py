from uuid import UUID

from app.application.analysis_runs.models import TranscriptItemIdentity
from app.application.transcripts.models import (
    DEFAULT_INLINE_CONTENT_BYTES,
    DEFAULT_PREVIEW_BYTES,
    ContentLocation,
    ReferenceResolution,
    ReferenceResolutionStatus,
    StoredTranscriptContent,
    StoredTranscriptItem,
    TranscriptContentRef,
    TranscriptItem,
    TranscriptStorageBatch,
)
from app.application.transcripts.registry import (
    TranscriptRegistry,
    validate_references as validate_registered_references,
)


class PendingTranscriptReferencesError(ValueError):
    def __init__(self, resolution: ReferenceResolution) -> None:
        super().__init__("transcript references are still generating")
        self.resolution = resolution


def _utf8_head(value: str, maximum_bytes: int) -> str:
    return value.encode("utf-8")[:maximum_bytes].decode("utf-8", errors="ignore")


def _utf8_tail(value: str, maximum_bytes: int) -> str:
    return value.encode("utf-8")[-maximum_bytes:].decode("utf-8", errors="ignore")


def locate_content(
    safe_body: str,
    *,
    inline_content_bytes: int = DEFAULT_INLINE_CONTENT_BYTES,
    preview_bytes: int = DEFAULT_PREVIEW_BYTES,
) -> ContentLocation:
    if inline_content_bytes < 0:
        raise ValueError("inline_content_bytes must be non-negative")
    if preview_bytes <= 0:
        raise ValueError("preview_bytes must be positive")
    if len(safe_body.encode("utf-8")) <= inline_content_bytes:
        return ContentLocation(inline_body=safe_body)
    return ContentLocation(
        preview_head=_utf8_head(safe_body, preview_bytes),
        preview_tail=_utf8_tail(safe_body, preview_bytes),
    )


class TranscriptBuffer:
    def __init__(self, registry: TranscriptRegistry) -> None:
        self._registry = registry
        self._items: list[TranscriptItem] = []
        self._item_ids: set[UUID] = set()
        self._sequences: set[tuple[UUID, int]] = set()
        self._error_ids: set[UUID] = set()

    @property
    def items(self) -> tuple[TranscriptItem, ...]:
        return tuple(self._items)

    def append(
        self,
        identity: TranscriptItemIdentity,
        content_type: str,
        content_schema_version: int,
        body: object,
        *,
        private_values: tuple[str, ...] = (),
        reserved_references: tuple[TranscriptContentRef, ...] | None = None,
    ) -> TranscriptItem:
        if identity.item_id in self._item_ids:
            raise ValueError("transcript item identity already exists")
        sequence_key = (identity.run_id, identity.transcript_sequence)
        if sequence_key in self._sequences:
            raise ValueError("transcript sequence already exists in this run")
        content = self._registry.encode(
            content_type,
            content_schema_version,
            body,
            private_values=private_values,
        )
        if reserved_references is not None:
            validate_registered_references(
                identity.run_id,
                content.references,
                same_batch=reserved_references,
            )
        error_ids = self._registry.error_ids(content)
        if self._error_ids.intersection(error_ids):
            raise ValueError("an embedded error must not be copied to another item")
        item = TranscriptItem(
            **identity.model_dump(mode="python"),
            content=content,
        )
        self._items.append(item)
        self._item_ids.add(item.item_id)
        self._sequences.add(sequence_key)
        self._error_ids.update(error_ids)
        return item

    def to_storage_batch(
        self,
        *,
        private_values: tuple[str, ...] = (),
        inline_content_bytes: int = DEFAULT_INLINE_CONTENT_BYTES,
        preview_bytes: int = DEFAULT_PREVIEW_BYTES,
        persisted: tuple[TranscriptContentRef, ...] = (),
        generating: tuple[TranscriptContentRef, ...] = (),
        item_ids: tuple[UUID, ...] | None = None,
    ) -> TranscriptStorageBatch:
        selected = self._selected_items(item_ids)
        resolution = self.reference_resolution(
            persisted=persisted,
            generating=generating,
            item_ids=item_ids,
        )
        if resolution.status is ReferenceResolutionStatus.WAITING:
            raise PendingTranscriptReferencesError(resolution)
        stored_items: list[StoredTranscriptItem] = []
        contents: list[StoredTranscriptContent] = []
        for item in selected:
            content = self._registry.recheck(
                item.content,
                private_values=private_values,
            )
            if self._registry.is_message_type(
                content.content_type,
                content.content_schema_version,
            ):
                projection = self._registry.message_projection(
                    content.content_type,
                    content.content_schema_version,
                    content.safe_body,
                )
                if projection.generation_status.value == "generating":
                    raise ValueError(
                        "generating messages are live-only and cannot be stored"
                    )
            location = locate_content(
                content.safe_body,
                inline_content_bytes=inline_content_bytes,
                preview_bytes=preview_bytes,
            )
            stored_items.append(StoredTranscriptItem(
                item_id=item.item_id,
                run_id=item.run_id,
                session_id=item.session_id,
                transcript_sequence=item.transcript_sequence,
                created_at=item.created_at,
                request_id=item.request_id,
                operation_id=item.operation_id,
                content_type=content.content_type,
                content_schema_version=content.content_schema_version,
                tags=content.metadata.tags,
                rag_eligible=content.metadata.rag_eligible,
                inline_body=location.inline_body,
                preview_head=location.preview_head,
                preview_tail=location.preview_tail,
                content_bytes=content.content_bytes,
            ))
            if location.inline_body is None:
                contents.append(StoredTranscriptContent(
                    item_id=item.item_id,
                    body=content.safe_body,
                    created_at=item.created_at,
                ))
        return TranscriptStorageBatch(
            items=tuple(stored_items),
            contents=tuple(contents),
        )

    def reference_resolution(
        self,
        *,
        persisted: tuple[TranscriptContentRef, ...] = (),
        generating: tuple[TranscriptContentRef, ...] = (),
        item_ids: tuple[UUID, ...] | None = None,
    ) -> ReferenceResolution:
        selected = self._selected_items(item_ids)
        same_batch = tuple(item.content_ref for item in selected)
        waiting: list[TranscriptContentRef] = []
        for item in selected:
            resolution = validate_registered_references(
                item.run_id,
                item.content.references,
                persisted=persisted,
                same_batch=same_batch,
                generating=generating,
            )
            for target in resolution.waiting_for:
                if target not in waiting:
                    waiting.append(target)
        if waiting:
            return ReferenceResolution(
                status=ReferenceResolutionStatus.WAITING,
                waiting_for=tuple(waiting),
            )
        return ReferenceResolution(status=ReferenceResolutionStatus.READY)

    def _selected_items(
        self,
        item_ids: tuple[UUID, ...] | None,
    ) -> tuple[TranscriptItem, ...]:
        if item_ids is None:
            return tuple(self._items)
        requested = set(item_ids)
        if len(requested) != len(item_ids):
            raise ValueError("selected transcript item identities must be unique")
        selected = tuple(item for item in self._items if item.item_id in requested)
        if len(selected) != len(requested):
            raise ValueError("selected transcript item is not finalized")
        return selected

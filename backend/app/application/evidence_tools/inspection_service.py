from app.application.evidence_tools.errors import EvidenceScopeViolationError
from app.application.evidence_tools.models import (
    EvidenceInspectionPage,
    EvidenceToolReader,
)
from app.application.evidence_tools.scope import EvidenceSessionScope
from app.domain import SourceRecordReference


class EvidenceInspectionService:
    def __init__(
        self,
        reader: EvidenceToolReader,
        *,
        default_page_size: int = 50,
        max_page_size: int = 200,
    ) -> None:
        if not 1 <= default_page_size <= max_page_size:
            raise ValueError("invalid inspection page limits")
        self._reader = reader
        self._default_page_size = default_page_size
        self._max_page_size = max_page_size

    async def inspect(
        self,
        scope: EvidenceSessionScope,
        *,
        source_item_id: str,
        offset: int = 0,
        limit: int | None = None,
    ) -> EvidenceInspectionPage:
        scope.cancel_token.checkpoint()
        page_limit = self._default_page_size if limit is None else limit
        if not source_item_id.strip() or offset < 0 or not 1 <= page_limit <= self._max_page_size:
            raise EvidenceScopeViolationError()
        records = await self._reader.read_source_records(scope, source_item_id)
        scope.cancel_token.checkpoint()
        ordered = tuple(sorted(records, key=_record_order))
        page_records = ordered[offset : offset + page_limit]
        end = offset + len(page_records)
        return EvidenceInspectionPage(
            source_item_id=source_item_id,
            offset=offset,
            limit=page_limit,
            total_records=len(ordered),
            records=page_records,
            record_refs=tuple(
                SourceRecordReference(
                    item_id=source_item_id,
                    record_id=record.record_id,
                )
                for record in page_records
            ),
            next_offset=end if end < len(ordered) else None,
        )


def _record_order(record) -> tuple[object, str]:
    observed = record.observed_time
    if observed is None:
        observed = record.valid_time_range.start
    return observed, record.record_id

from typing import Protocol

from app.domain import HistoricalCaseRef, HistoricalCaseSourceV1


class HistoricalSourceReaderPort(Protocol):
    """Read one authoritative historical case by its complete cross-run identity."""

    async def read_case(
        self,
        case_ref: HistoricalCaseRef,
    ) -> HistoricalCaseSourceV1: ...

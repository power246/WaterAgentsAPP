from typing import Generic, Self, TypeVar

from pydantic import model_validator

from app.domain.common import DomainModel, NonEmptyString
from app.domain.evidence.collection_completion_proof import (
    CollectionCompletionProof,
)
from app.domain.evidence.collection_completion_contract import (
    CollectionCompletionContract,
)
from app.domain.evidence.collection_counts import CollectionCounts
from app.domain.evidence.collection_page_manifest import CollectionPageManifest
from app.domain.time.time_range import TimeRange


RecordT = TypeVar("RecordT")


class ProviderCollectionBatch(DomainModel, Generic[RecordT]):
    """Provider-neutral records plus technical collection-completeness facts."""

    operation_id: NonEmptyString
    records: tuple[RecordT, ...]
    page_or_chunk_manifests: tuple[CollectionPageManifest, ...]
    completed_ranges: tuple[TimeRange, ...]
    counts: CollectionCounts
    completion_proof: CollectionCompletionProof
    incomplete_reason: NonEmptyString | None = None

    @model_validator(mode="after")
    def validate_batch(self) -> Self:
        if self.counts.pages != len(self.page_or_chunk_manifests):
            raise ValueError("page count does not match page manifests")
        if self.counts.records != len(self.records):
            raise ValueError("record count does not match records")
        if self.counts.records != sum(
            page.record_count for page in self.page_or_chunk_manifests
        ):
            raise ValueError("record count does not match page manifests")
        if self.counts.response_bytes != sum(
            page.response_bytes for page in self.page_or_chunk_manifests
        ):
            raise ValueError("byte count does not match page manifests")
        page_ids = tuple(page.page_identity for page in self.page_or_chunk_manifests)
        if len(page_ids) != len(set(page_ids)):
            raise ValueError("page identities must be unique")
        for previous, current in zip(
            self.completed_ranges,
            self.completed_ranges[1:],
        ):
            if previous.end > current.start:
                raise ValueError("completed ranges must be ordered and non-overlapping")
        if self.completion_proof.complete:
            if self.incomplete_reason is not None:
                raise ValueError("complete collection must not have an incomplete reason")
            if not self.page_or_chunk_manifests:
                raise ValueError("complete collection requires a terminal page or chunk")
            if any(not page.page_complete for page in self.page_or_chunk_manifests):
                raise ValueError("complete collection requires every page to be complete")
            if self.completion_proof.terminal_identity is None:
                raise ValueError("complete collection requires terminal identity")
            final_page = self.page_or_chunk_manifests[-1]
            if (
                self.completion_proof.terminal_identity
                != final_page.page_identity
                or final_page.next_page_identity is not None
            ):
                raise ValueError("completion proof must identify a terminal page")
            if (
                self.completion_proof.contract
                is CollectionCompletionContract.SINGLE_RESPONSE
                and len(self.page_or_chunk_manifests) != 1
            ):
                raise ValueError("single-response completion requires exactly one page")
            if (
                self.completion_proof.contract
                is CollectionCompletionContract.OFFICIAL_NEXT_CHAIN
                and any(
                    current.next_page_identity != following.page_identity
                    for current, following in zip(
                        self.page_or_chunk_manifests,
                        self.page_or_chunk_manifests[1:],
                    )
                )
            ):
                raise ValueError("official next chain must link every page")
        elif self.incomplete_reason is None:
            raise ValueError("incomplete collection requires a safe reason")
        return self

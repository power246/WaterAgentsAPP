from typing import Literal, Self

from pydantic import field_validator, model_validator

from app.domain.common import DomainModel, NonEmptyString, ensure_unique_tuple
from app.domain.evidence.collection_completion_proof import (
    CollectionCompletionProof,
)
from app.domain.evidence.collection_counts import CollectionCounts
from app.domain.evidence.collection_page_manifest import CollectionPageManifest
from app.domain.evidence.collection_status import CollectionStatus
from app.domain.evidence.window_completeness import WindowCompleteness
from app.domain.time.time_range import TimeRange


class CollectionManifest(DomainModel):
    schema_version: Literal[1] = 1
    manifest_id: NonEmptyString
    operation_id: NonEmptyString
    provider: NonEmptyString
    channel: NonEmptyString
    collection_status: CollectionStatus
    request_range: TimeRange
    requested_series: tuple[NonEmptyString, ...]
    page_or_chunk_manifests: tuple[CollectionPageManifest, ...]
    completed_ranges: tuple[TimeRange, ...]
    affected_window_ids: tuple[NonEmptyString, ...]
    window_completeness: tuple[WindowCompleteness, ...]
    counts: CollectionCounts
    completion_proof: CollectionCompletionProof

    @field_validator("requested_series", "affected_window_ids")
    @classmethod
    def validate_nonempty_unique_strings(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not value:
            raise ValueError("manifest series and windows must not be empty")
        return ensure_unique_tuple(value, field_name="manifest series or windows")

    @model_validator(mode="after")
    def validate_completion(self) -> Self:
        page_ids = tuple(
            page.page_identity for page in self.page_or_chunk_manifests
        )
        ensure_unique_tuple(page_ids, field_name="collection page identities")
        window_ids = tuple(item.window_id for item in self.window_completeness)
        ensure_unique_tuple(window_ids, field_name="window completeness identities")
        if set(window_ids) != set(self.affected_window_ids):
            raise ValueError("window completeness must cover affected windows exactly")
        if self.counts.pages != len(self.page_or_chunk_manifests):
            raise ValueError("page count does not match page manifests")
        if self.counts.records != sum(
            page.record_count for page in self.page_or_chunk_manifests
        ):
            raise ValueError("record count does not match page manifests")
        if self.counts.response_bytes != sum(
            page.response_bytes for page in self.page_or_chunk_manifests
        ):
            raise ValueError("byte count does not match page manifests")
        for previous, current in zip(
            self.completed_ranges,
            self.completed_ranges[1:],
        ):
            if previous.end > current.start:
                raise ValueError("completed_ranges must be ordered and non-overlapping")
        if any(
            item.start < self.request_range.start or item.end > self.request_range.end
            for item in self.completed_ranges
        ):
            raise ValueError("completed_ranges must stay within request_range")
        windows_complete = all(item.complete for item in self.window_completeness)
        if self.completion_proof.complete != windows_complete:
            raise ValueError("completion proof must match window completeness")
        if self.collection_status is CollectionStatus.EMPTY:
            if not self.completion_proof.complete or self.counts.records != 0:
                raise ValueError("empty requires complete collection with zero records")
        if (
            self.collection_status is CollectionStatus.UNAVAILABLE
            and self.completion_proof.complete
        ):
            raise ValueError("unavailable collection cannot be complete")
        return self

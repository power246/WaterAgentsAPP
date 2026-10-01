import asyncio
from collections.abc import Callable
from datetime import datetime, timezone

from app.application.evidence_tools import (
    EvidenceCollectionArtifacts,
    EvidenceScopeViolationError,
    StoredPolicyObservations,
)
from app.domain import (
    CollectionCompletionProof,
    CollectionCounts,
    CollectionPageManifest,
    EvidenceFragment,
    EvidenceFragmentReference,
    ItemReference,
    ProviderCollectionBatch,
    SourceRecord,
    SourceRecordReference,
)


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 10, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value


class SequentialIds:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"operation-{self.value:03d}"


class MemoryEvidenceStore:
    def __init__(self) -> None:
        self.calls = []
        self.results = []
        self.fragments: dict[tuple[str, str], EvidenceFragment] = {}
        self.records: dict[tuple[str, str, str], tuple[SourceRecord, ...]] = {}
        self.observations: dict[
            tuple[SourceRecordReference, ...], StoredPolicyObservations
        ] = {}

    async def write_call(self, scope, request, *, operation_id):
        self.calls.append((scope.run_id, request, operation_id))
        return ItemReference(item_id=f"call-item-{len(self.calls):03d}")

    async def write_collection(self, scope, *, operation_id, batches):
        records = tuple(record for batch in batches for record in batch.records)
        item_id = f"source:{scope.run_id}:{scope.session_id}:{operation_id}"
        self.records[(scope.run_id, scope.session_id, item_id)] = records
        return EvidenceCollectionArtifacts(
            manifest_ref=ItemReference(item_id=f"manifest:{operation_id}"),
            source_record_refs=tuple(
                SourceRecordReference(item_id=item_id, record_id=record.record_id)
                for record in records
            ),
        )

    async def write_fragment(self, scope, *, operation_id, fragment):
        item_id = f"fragment:{scope.run_id}:{scope.session_id}:{operation_id}"
        self.fragments[(item_id, fragment.evidence_id)] = fragment
        return EvidenceFragmentReference(
            item_id=item_id,
            evidence_id=fragment.evidence_id,
        )

    async def write_result(self, scope, result):
        self.results.append((scope.run_id, result))
        return ItemReference(item_id=f"result-item-{len(self.results):03d}")

    async def read_fragment(self, scope, reference):
        fragment = self.fragments.get((reference.item_id, reference.evidence_id))
        if fragment is None or fragment.target_comid != scope.agent_comid:
            raise EvidenceScopeViolationError()
        return fragment

    async def read_policy_observations(self, scope, references):
        stored = self.observations.get(references)
        if stored is None:
            raise EvidenceScopeViolationError()
        return stored

    async def read_source_records(self, scope, source_item_id):
        records = self.records.get((scope.run_id, scope.session_id, source_item_id))
        if records is None:
            raise EvidenceScopeViolationError()
        return records

    def seed_records(self, scope, item_id, records):
        self.records[(scope.run_id, scope.session_id, item_id)] = tuple(records)

    def seed_fragment(self, reference, fragment):
        self.fragments[(reference.item_id, reference.evidence_id)] = fragment

    def seed_observations(self, references, observations, *, complete=True, cadence=3600):
        self.observations[tuple(references)] = StoredPolicyObservations(
            observations=tuple(observations),
            collection_complete=complete,
            expected_cadence_seconds=cadence,
        )


class FakeCollectionPort:
    def __init__(
        self,
        factory: Callable[[object], ProviderCollectionBatch],
        *,
        gated: bool = False,
    ) -> None:
        self.factory = factory
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not gated:
            self.release.set()

    async def collect(self, request, *, deadline=None):
        self.calls.append((request, deadline))
        self.started.set()
        await self.release.wait()
        return self.factory(request)


def complete_batch(request, records=(), *, complete=True, response_bytes=64):
    operation_id = request.operation_id
    request_range = getattr(request, "request_range", None)
    return ProviderCollectionBatch(
        operation_id=operation_id,
        records=tuple(records),
        page_or_chunk_manifests=(
            CollectionPageManifest(
                page_identity=f"page:{operation_id}",
                record_count=len(records),
                response_bytes=response_bytes,
                page_complete=complete,
            ),
        ),
        completed_ranges=(request_range,) if request_range is not None and complete else (),
        counts=CollectionCounts(
            requests=1,
            pages=1,
            records=len(records),
            response_bytes=response_bytes,
        ),
        completion_proof=CollectionCompletionProof(
            contract="single_response",
            complete=complete,
            terminal_identity=f"page:{operation_id}" if complete else None,
            digest=f"digest:{operation_id}",
        ),
        incomplete_reason=None if complete else "incomplete_source",
    )

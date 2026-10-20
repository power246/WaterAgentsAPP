import asyncio
from collections.abc import Callable
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from app.application.analysis_runs.models import SessionSummary
from app.application.evidence_tools import (
    EvidenceCollectionArtifacts,
    EvidenceScopeViolationError,
    StoredPolicyObservations,
)
from app.application.evidence_tools.collection_service import build_policy_observation
from app.domain import (
    CollectionCompletionProof,
    CollectionCounts,
    CollectionPageManifest,
    EvidenceFragment,
    EvidenceFragmentReference,
    ItemReference,
    NumericDependencyPlan,
    ProviderCollectionBatch,
    ReferenceScope,
    SourceRecord,
    SourceRecordReference,
)
from app.infrastructure.config import load_parameter_capability_registry


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
        self.manifests = {}
        self.bundles = []
        self.scopes: dict[str, ReferenceScope] = {}
        self.fail_bundle_write = False
        self.records: dict[tuple[str, str, str], tuple[SourceRecord, ...]] = {}
        self.observations: dict[
            tuple[SourceRecordReference, ...], StoredPolicyObservations
        ] = {}
        self.sessions: dict[str, SessionSummary] = {}
        self.numeric_plans: dict[str, NumericDependencyPlan] = {}

    async def read_agent_session(self, scope, session_id):
        try:
            return self.sessions[session_id]
        except KeyError as error:
            raise EvidenceScopeViolationError() from error

    async def write_call(self, scope, request, *, operation_id, policy):
        self.calls.append((scope.run_id, request, operation_id, policy))
        reference = ItemReference(item_id=f"call-item-{len(self.calls):03d}")
        self._remember_scope(scope, reference.item_id)
        return reference

    async def write_collection(self, scope, *, operation_id, manifests, records):
        item_id = str(
            uuid5(
                NAMESPACE_URL,
                f"source:{scope.run_id}:{scope.session_id}:{operation_id}:"
                f"{len(self.records) + 1}",
            )
        )
        self.records[(scope.run_id, scope.session_id, item_id)] = records
        self._remember_scope(scope, item_id)
        manifest_refs = []
        for index, manifest in enumerate(manifests, start=1):
            manifest_item_id = f"manifest:{operation_id}:{index:03d}"
            self.manifests[manifest_item_id] = manifest
            self._remember_scope(scope, manifest_item_id)
            manifest_refs.append(ItemReference(item_id=manifest_item_id))
        return EvidenceCollectionArtifacts(
            manifest_refs=tuple(manifest_refs),
            source_record_refs=tuple(
                SourceRecordReference(item_id=item_id, record_id=record.record_id)
                for record in records
            ),
        )

    async def write_fragment(self, scope, *, operation_id, fragment):
        item_id = str(
            uuid5(
                NAMESPACE_URL,
                f"fragment:{scope.run_id}:{scope.session_id}:{operation_id}:"
                f"{len(self.fragments) + 1}",
            )
        )
        self.fragments[(item_id, fragment.evidence_id)] = fragment
        self._remember_scope(scope, item_id)
        return EvidenceFragmentReference(
            item_id=item_id,
            evidence_id=fragment.evidence_id,
        )

    async def write_numeric_plan(self, scope, plan):
        item_id = str(uuid5(
            NAMESPACE_URL,
            f"numeric-plan:{scope.run_id}:{scope.session_id}:{plan.plan_digest}",
        ))
        self.numeric_plans[item_id] = plan
        self._remember_scope(scope, item_id)
        return ItemReference(item_id=item_id)

    async def read_numeric_plan(self, scope, reference):
        plan = self.numeric_plans.get(reference.item_id)
        if (
            plan is None
            or plan.run_id != scope.run_id
            or plan.session_id != scope.session_id
            or plan.target_comid != scope.agent_comid
        ):
            raise EvidenceScopeViolationError()
        return plan

    async def write_result(self, scope, result):
        self.results.append((scope.run_id, result))
        reference = ItemReference(item_id=f"result-item-{len(self.results):03d}")
        self._remember_scope(scope, reference.item_id)
        return reference

    async def write_bundle(self, scope, bundle):
        if self.fail_bundle_write:
            raise RuntimeError("bundle write failed")
        self.bundles.append((scope.run_id, bundle))
        reference = ItemReference(item_id=f"bundle-item-{len(self.bundles):03d}")
        self._remember_scope(scope, reference.item_id)
        return reference

    async def read_fragment(self, scope, reference):
        fragment = self.fragments.get((reference.item_id, reference.evidence_id))
        if fragment is None or fragment.target_comid != scope.agent_comid:
            raise EvidenceScopeViolationError()
        return fragment

    async def read_fragments(self, scope, references):
        return tuple([
            await self.read_fragment(scope, reference)
            for reference in references
        ])

    async def read_policy_observations(self, scope, references, window):
        stored = self.observations.get(references)
        if stored is not None:
            return stored
        records = {
            (item_id, record.record_id): record
            for (run_id, session_id, item_id), items in self.records.items()
            if run_id == scope.run_id and session_id == scope.session_id
            for record in items
        }
        try:
            selected = tuple(
                records[(reference.item_id, reference.record_id)]
                for reference in references
            )
            window_id = next(
                item.window_id for item in scope.time_windows
                if item.time_range == window
            )
            manifests = tuple(
                next(
                    manifest for manifest in self.manifests.values()
                    if record.provenance.page_identity in {
                        page.page_identity
                        for page in manifest.page_or_chunk_manifests
                    }
                )
                for record in selected
            )
        except (KeyError, StopIteration) as error:
            raise EvidenceScopeViolationError() from error
        registry = load_parameter_capability_registry()
        observations = tuple(
            build_policy_observation(registry, record, reference)
            for reference, record in zip(references, selected, strict=True)
        )
        if not observations or any(item is None for item in observations):
            raise EvidenceScopeViolationError()
        return StoredPolicyObservations(
            observations=observations,
            collection_complete=all(
                manifest.completion_proof.complete
                and any(
                    item.window_id == window_id and item.complete
                    for item in manifest.window_completeness
                )
                for manifest in manifests
            ),
            selection_complete=True,
            expected_cadence_seconds=None,
        )

    async def read_source_records(self, scope, source_item_id):
        records = self.records.get((scope.run_id, scope.session_id, source_item_id))
        if records is None:
            raise EvidenceScopeViolationError()
        return records

    async def resolve_reference_scopes(self, scope, item_ids):
        try:
            resolved = tuple(self.scopes[item_id] for item_id in item_ids)
        except KeyError as error:
            raise EvidenceScopeViolationError() from error
        if any(item.run_id != scope.run_id for item in resolved):
            raise EvidenceScopeViolationError()
        return resolved

    def seed_records(self, scope, item_id, records):
        self.records[(scope.run_id, scope.session_id, item_id)] = tuple(records)
        self._remember_scope(scope, item_id)

    def seed_fragment(self, reference, fragment):
        self.fragments[(reference.item_id, reference.evidence_id)] = fragment

    def seed_scope(self, item_id, *, run_id="run-1", session_id="session-1"):
        self.scopes[item_id] = ReferenceScope(
            item_id=item_id,
            run_id=run_id,
            session_id=session_id,
        )

    def seed_session(self, session):
        self.sessions[str(session.session_id)] = session

    def seed_observations(
        self, references, observations, *, complete=True, selection_complete=True,
        cadence=3600, station_point=None,
    ):
        self.observations[tuple(references)] = StoredPolicyObservations(
            observations=tuple(observations),
            collection_complete=complete,
            selection_complete=selection_complete,
            expected_cadence_seconds=cadence,
            station_point=station_point,
        )

    def _remember_scope(self, scope, item_id):
        self.scopes[item_id] = ReferenceScope(
            item_id=item_id,
            run_id=scope.run_id,
            session_id=scope.session_id,
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

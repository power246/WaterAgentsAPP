from collections import defaultdict
from uuid import UUID

from app.application.analysis_runs.models import SessionSummary
from app.application.analysis_runs.workflow import WorkflowOperations
from app.application.evidence_tools.collection_service import build_policy_observation
from app.application.evidence_tools.errors import EvidenceScopeViolationError
from app.application.evidence_tools.models import (
    EvidenceCollectionArtifacts,
    EvidencePolicyClass,
    StoredPolicyObservations,
)
from app.application.evidence_tools.scope import EvidenceSessionScope
from app.application.transcripts.read_models import ReadView
from app.application.transcripts.reader import (
    TranscriptContentUnreadableError,
    TranscriptItemNotFoundError,
    TranscriptReader,
    TranscriptRunNotFoundError,
    TranscriptSessionNotFoundError,
)
from app.domain import (
    CollectionManifest,
    EvidenceBundle,
    EvidenceFragment,
    EvidenceFragmentReference,
    EvidenceSourceRecords,
    EvidenceToolCall,
    EvidenceToolRequest,
    EvidenceToolResult,
    ItemReference,
    ParameterCapabilityRegistry,
    ReferenceScope,
    SourceRecord,
    SourceRecordReference,
)


class EvidenceTranscriptStore:
    """M5 evidence adapter over the existing transcript writer and reader."""

    def __init__(
        self,
        *,
        operations: WorkflowOperations,
        reader: TranscriptReader,
        capability_registry: ParameterCapabilityRegistry,
        read_view: ReadView = ReadView.AUTO,
    ) -> None:
        self._operations = operations
        self._reader = reader
        self._capabilities = capability_registry
        self._read_view = read_view

    async def read_agent_session(
        self,
        scope: EvidenceSessionScope,
        session_id: str,
    ) -> SessionSummary:
        try:
            detail = await self._reader.get_session(
                UUID(scope.run_id),
                UUID(session_id),
                self._read_view,
            )
        except (
            ValueError,
            TranscriptRunNotFoundError,
            TranscriptSessionNotFoundError,
        ) as error:
            raise EvidenceScopeViolationError() from error
        return detail.session

    async def write_call(
        self,
        scope: EvidenceSessionScope,
        request: EvidenceToolRequest,
        *,
        operation_id: str,
        policy: EvidencePolicyClass,
    ) -> ItemReference:
        if request.session_id != scope.session_id:
            raise EvidenceScopeViolationError()
        identity = await self._operations.reserve_transcript_item(
            scope.run_id,
            session_id=scope.session_id,
            operation_id=operation_id,
        )
        body = EvidenceToolCall(
            tool_call_id=request.tool_call_id,
            session_id=request.session_id,
            tool_name=request.tool_name,
            purpose=request.purpose,
            window_ids=request.window_ids,
            parameter_keys=request.parameter_keys,
            options=request.options,
            policy=policy.value,
            requested_at=identity.created_at,
        )
        await self._operations.finalize_transcript_item(
            scope.run_id,
            identity.item_id,
            "evidence_tool_call",
            1,
            body,
        )
        return ItemReference(item_id=str(identity.item_id))

    async def write_collection(
        self,
        scope: EvidenceSessionScope,
        *,
        operation_id: str,
        manifests: tuple[CollectionManifest, ...],
        records: tuple[SourceRecord, ...],
    ) -> EvidenceCollectionArtifacts:
        if not manifests:
            raise EvidenceScopeViolationError()
        record_batches = _assign_records_to_manifests(manifests, records)
        manifest_refs: list[ItemReference] = []
        record_refs: list[SourceRecordReference] = []
        for manifest, batch in zip(manifests, record_batches, strict=True):
            reference = await self._write_source_batch(
                scope,
                operation_id=operation_id,
                manifest=manifest,
                records=batch,
            )
            manifest_refs.append(reference)
            record_refs.extend(
                SourceRecordReference(
                    item_id=reference.item_id,
                    record_id=record.record_id,
                )
                for record in batch
            )
        return EvidenceCollectionArtifacts(
            manifest_refs=tuple(manifest_refs),
            source_record_refs=tuple(record_refs),
        )

    async def write_fragment(
        self,
        scope: EvidenceSessionScope,
        *,
        operation_id: str,
        fragment: EvidenceFragment,
    ) -> EvidenceFragmentReference:
        reference = await self._write(
            scope,
            content_type="evidence_fragment",
            body=fragment,
            operation_id=operation_id,
        )
        return EvidenceFragmentReference(
            item_id=reference.item_id,
            evidence_id=fragment.evidence_id,
        )

    async def write_result(
        self,
        scope: EvidenceSessionScope,
        result: EvidenceToolResult,
    ) -> ItemReference:
        return await self._write(
            scope,
            content_type="evidence_tool_result",
            body=result,
            operation_id=result.operation_id,
        )

    async def write_bundle(
        self,
        scope: EvidenceSessionScope,
        bundle: EvidenceBundle,
    ) -> ItemReference:
        return await self._write(
            scope,
            content_type="evidence_bundle",
            body=bundle,
            operation_id=None,
        )

    async def read_fragment(
        self,
        scope: EvidenceSessionScope,
        reference: EvidenceFragmentReference,
    ) -> EvidenceFragment:
        body = await self._read_session_item(scope, reference.item_id)
        if (
            not isinstance(body, EvidenceFragment)
            or body.evidence_id != reference.evidence_id
            or body.target_comid != scope.agent_comid
        ):
            raise EvidenceScopeViolationError()
        return body

    async def read_policy_observations(
        self,
        scope: EvidenceSessionScope,
        references: tuple[SourceRecordReference, ...],
    ) -> StoredPolicyObservations:
        if not references or len(references) != len(set(references)):
            raise EvidenceScopeViolationError()
        bodies: dict[str, EvidenceSourceRecords] = {}
        records: dict[SourceRecordReference, SourceRecord] = {}
        for item_id, grouped in _group_record_references(references).items():
            body = await self._read_session_item(scope, item_id)
            if not isinstance(body, EvidenceSourceRecords):
                raise EvidenceScopeViolationError()
            bodies[item_id] = body
            by_id = {record.record_id: record for record in body.records}
            for reference in grouped:
                record = by_id.get(reference.record_id)
                if record is None:
                    raise EvidenceScopeViolationError()
                records[reference] = record
        observations = tuple(
            build_policy_observation(
                self._capabilities,
                records[reference],
                reference,
                provider_name=bodies[reference.item_id].manifest.provider,
                channel=bodies[reference.item_id].manifest.channel,
            )
            for reference in references
        )
        if any(item is None for item in observations):
            raise EvidenceScopeViolationError()
        return StoredPolicyObservations(
            observations=tuple(item for item in observations if item is not None),
            collection_complete=all(
                body.manifest.completion_proof.complete for body in bodies.values()
            ),
        )

    async def read_source_records(
        self,
        scope: EvidenceSessionScope,
        source_item_id: str,
    ) -> tuple[SourceRecord, ...]:
        body = await self._read_session_item(scope, source_item_id)
        if not isinstance(body, EvidenceSourceRecords):
            raise EvidenceScopeViolationError()
        return body.records

    async def resolve_reference_scopes(
        self,
        scope: EvidenceSessionScope,
        item_ids: tuple[str, ...],
    ) -> tuple[ReferenceScope, ...]:
        if len(item_ids) != len(set(item_ids)):
            raise EvidenceScopeViolationError()
        resolved: list[ReferenceScope] = []
        try:
            run_id = UUID(scope.run_id)
            for item_id in item_ids:
                item = await self._reader.read_registered_item(
                    run_id,
                    UUID(item_id),
                    self._read_view,
                )
                resolved.append(ReferenceScope(
                    item_id=item_id,
                    run_id=scope.run_id,
                    session_id=(
                        None
                        if item.item.content_type
                        in {"query_understood", "topology_ready"}
                        else str(item.item.session_id)
                        if item.item.session_id is not None
                        else None
                    ),
                ))
        except (
            ValueError,
            TranscriptItemNotFoundError,
            TranscriptContentUnreadableError,
        ) as error:
            raise EvidenceScopeViolationError() from error
        return tuple(resolved)

    async def _write_source_batch(
        self,
        scope: EvidenceSessionScope,
        *,
        operation_id: str,
        manifest: CollectionManifest,
        records: tuple[SourceRecord, ...],
    ) -> ItemReference:
        return await self._write(
            scope,
            content_type="evidence_source_records",
            body=EvidenceSourceRecords(
                operation_id=operation_id,
                batch_id=manifest.manifest_id,
                manifest=manifest,
                records=records,
            ),
            operation_id=operation_id,
        )

    async def _write(
        self,
        scope: EvidenceSessionScope,
        *,
        content_type: str,
        body: object,
        operation_id: str | None,
    ) -> ItemReference:
        identity = await self._operations.reserve_transcript_item(
            scope.run_id,
            session_id=scope.session_id,
            operation_id=operation_id,
        )
        await self._operations.finalize_transcript_item(
            scope.run_id,
            identity.item_id,
            content_type,
            1,
            body,
        )
        return ItemReference(item_id=str(identity.item_id))

    async def _read_session_item(
        self,
        scope: EvidenceSessionScope,
        item_id: str,
    ) -> object:
        try:
            item = await self._reader.read_registered_item(
                UUID(scope.run_id),
                UUID(item_id),
                self._read_view,
                session_id=UUID(scope.session_id),
            )
        except (
            ValueError,
            TranscriptItemNotFoundError,
            TranscriptContentUnreadableError,
        ) as error:
            raise EvidenceScopeViolationError() from error
        return item.body


def _assign_records_to_manifests(
    manifests: tuple[CollectionManifest, ...],
    records: tuple[SourceRecord, ...],
) -> tuple[tuple[SourceRecord, ...], ...]:
    page_ids = tuple(
        {
            page.page_identity
            for page in manifest.page_or_chunk_manifests
        }
        for manifest in manifests
    )
    assigned: list[list[SourceRecord]] = [[] for _ in manifests]
    for record in records:
        identities = {
            *record.provenance.discovery_identities,
            record.provenance.page_identity,
            record.provenance.chunk_identity,
        } - {None}
        index = next(
            (
                candidate
                for candidate, manifest_pages in enumerate(page_ids)
                if identities & manifest_pages
            ),
            0,
        )
        assigned[index].append(record)
    return tuple(tuple(batch) for batch in assigned)


def _group_record_references(
    references: tuple[SourceRecordReference, ...],
) -> dict[str, tuple[SourceRecordReference, ...]]:
    grouped: defaultdict[str, list[SourceRecordReference]] = defaultdict(list)
    for reference in references:
        grouped[reference.item_id].append(reference)
    return {key: tuple(value) for key, value in grouped.items()}

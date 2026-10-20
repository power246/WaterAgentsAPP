import asyncio
from collections import defaultdict
from uuid import NAMESPACE_URL, UUID, uuid5

from app.application.analysis_runs.models import SessionSummary
from app.application.analysis_runs.persistence import RAGConsumptionSource
from app.application.analysis_runs.workflow import WorkflowOperations
from app.application.evidence_policy import PolicyObservation
from app.application.evidence_tools.collection_service import build_policy_observation
from app.application.evidence_tools.errors import (
    EvidencePersistenceUnavailableError,
    EvidenceScopeViolationError,
)
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
    GeoPoint,
    HistoricalTranscriptRecord,
    ItemReference,
    NumericDependencyPlan,
    ParameterCapabilityRegistry,
    ReferenceScope,
    SourceRecord,
    SourceRecordReference,
    TimeRange,
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

    async def write_numeric_plan(
        self,
        scope: EvidenceSessionScope,
        plan: NumericDependencyPlan,
    ) -> ItemReference:
        if (
            plan.run_id != scope.run_id
            or plan.session_id != scope.session_id
            or plan.target_comid != scope.agent_comid
        ):
            raise EvidenceScopeViolationError()
        return await self._write(
            scope,
            content_type="numeric_dependency_plan",
            body=plan,
            operation_id=str(uuid5(
                NAMESPACE_URL,
                f"m5:numeric-plan:{scope.run_id}:{scope.session_id}:{plan.plan_digest}",
            )),
        )

    async def read_numeric_plan(
        self,
        scope: EvidenceSessionScope,
        reference: ItemReference,
    ) -> NumericDependencyPlan:
        body = await self._read_session_item(scope, reference.item_id)
        if (
            not isinstance(body, NumericDependencyPlan)
            or body.run_id != scope.run_id
            or body.session_id != scope.session_id
            or body.target_comid != scope.agent_comid
        ):
            raise EvidenceScopeViolationError()
        return body

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
        return (await self.read_fragments(scope, (reference,)))[0]

    async def read_fragments(
        self,
        scope: EvidenceSessionScope,
        references: tuple[EvidenceFragmentReference, ...],
    ) -> tuple[EvidenceFragment, ...]:
        bodies = await self._read_session_items(
            scope, tuple(reference.item_id for reference in references)
        )
        fragments = []
        for reference, body in zip(references, bodies, strict=True):
            if (
                not isinstance(body, EvidenceFragment)
                or body.evidence_id != reference.evidence_id
                or body.target_comid != scope.agent_comid
            ):
                raise EvidenceScopeViolationError()
            fragments.append(body)
        return tuple(fragments)

    async def read_policy_observations(
        self,
        scope: EvidenceSessionScope,
        references: tuple[SourceRecordReference, ...],
        window: TimeRange,
    ) -> StoredPolicyObservations:
        if not references or len(references) != len(set(references)):
            raise EvidenceScopeViolationError()
        bodies: dict[str, EvidenceSourceRecords] = {}
        records: dict[SourceRecordReference, SourceRecord] = {}
        grouped_references = _group_record_references(references)
        source_bodies = await self._read_session_items(
            scope, tuple(grouped_references)
        )
        for (item_id, grouped), body in zip(
            grouped_references.items(), source_bodies, strict=True
        ):
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
        selected = observations[0]
        eligible_refs = {
            reference
            for item_id, body in bodies.items()
            for record in body.records
            if (
                observation := build_policy_observation(
                    self._capabilities,
                    record,
                    SourceRecordReference(item_id=item_id, record_id=record.record_id),
                    provider_name=body.manifest.provider,
                    channel=body.manifest.channel,
                )
            ) is not None
            if observation.parameter == selected.parameter
            and observation.station_id == selected.station_id
            and observation.series_identity == selected.series_identity
            and (
                window.contains(observation.observed_time)
                if observation.observed_time is not None
                else observation.valid_time_range is not None
                and window.overlaps(observation.valid_time_range)
            )
            for reference in (observation.record_ref,)
        }
        return StoredPolicyObservations(
            observations=tuple(item for item in observations if item is not None),
            collection_complete=all(
                body.manifest.completion_proof.complete for body in bodies.values()
            ),
            selection_complete=eligible_refs == set(references),
            station_point=_consistent_station_point(observations),
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
        items = await self._read_items(scope, item_ids)
        return tuple(ReferenceScope(
            item_id=item_id,
            run_id=scope.run_id,
            session_id=(
                None
                if item.item.content_type in {"query_understood", "topology_ready"}
                else str(item.item.session_id)
                if item.item.session_id is not None
                else None
            ),
        ) for item_id, item in zip(item_ids, items, strict=True))

    async def _write_source_batch(
        self,
        scope: EvidenceSessionScope,
        *,
        operation_id: str,
        manifest: CollectionManifest,
        records: tuple[SourceRecord, ...],
    ) -> ItemReference:
        consumption_sources = tuple(
            RAGConsumptionSource(
                source_generation_id=UUID(record.case_ref.generation_id),
                source_case_id=UUID(record.case_ref.case_id),
                source_run_id=UUID(record.case_ref.run_id),
                source_session_id=UUID(record.case_ref.session_id),
                source_result_item_id=UUID(record.case_ref.result_item_id),
            )
            for record in records
            if isinstance(record, HistoricalTranscriptRecord)
        )
        body = await asyncio.to_thread(
            EvidenceSourceRecords,
            operation_id=operation_id,
            batch_id=manifest.manifest_id,
            manifest=manifest,
            records=records,
        )
        return await self._write(
            scope,
            content_type="evidence_source_records",
            body=body,
            operation_id=operation_id,
            rag_consumption_sources=consumption_sources,
        )

    async def _write(
        self,
        scope: EvidenceSessionScope,
        *,
        content_type: str,
        body: object,
        operation_id: str | None,
        rag_consumption_sources: tuple[RAGConsumptionSource, ...] = (),
    ) -> ItemReference:
        identity = await self._operations.reserve_transcript_item(
            scope.run_id,
            session_id=scope.session_id,
            operation_id=operation_id,
        )
        if rag_consumption_sources:
            await self._operations.finalize_transcript_item(
                scope.run_id,
                identity.item_id,
                content_type,
                1,
                body,
                rag_consumption_sources=rag_consumption_sources,
            )
        else:
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
        return (await self._read_session_items(scope, (item_id,)))[0]

    async def _read_session_items(
        self,
        scope: EvidenceSessionScope,
        item_ids: tuple[str, ...],
    ) -> tuple[object, ...]:
        items = await self._read_items(scope, item_ids)
        if any(
            item.item.session_id != UUID(scope.session_id)
            for item in items
        ):
            raise EvidenceScopeViolationError()
        return tuple(item.body for item in items)

    async def _read_items(
        self,
        scope: EvidenceSessionScope,
        item_ids: tuple[str, ...],
    ):
        try:
            keys = tuple(UUID(item_id) for item_id in item_ids)
            warning = await self._operations.wait_for_transcript_items_persisted(
                scope.run_id, keys
            )
            if warning is not None:
                raise EvidencePersistenceUnavailableError()
            selected = await self._reader.read_registered_items_persisted(
                UUID(scope.run_id), keys, self._read_view
            )
        except (
            ValueError,
            TranscriptItemNotFoundError,
            TranscriptContentUnreadableError,
            TranscriptRunNotFoundError,
        ) as error:
            raise EvidenceScopeViolationError() from error
        by_id = {item.item.item_id: item for item in selected}
        if len(by_id) != len(keys) or any(
            item_id not in by_id or by_id[item_id].body is None
            for item_id in keys
        ):
            raise EvidenceScopeViolationError()
        return tuple(by_id[item_id] for item_id in keys)


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


def _consistent_station_point(
    observations: tuple[PolicyObservation | None, ...],
) -> GeoPoint | None:
    if not observations or observations[0] is None:
        return None
    point = observations[0].location_point
    if point is None or any(
        item is None or item.location_point != point for item in observations[1:]
    ):
        return None
    return point

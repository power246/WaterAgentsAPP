from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.application.analysis_runs.topology_result_models import (
    DependencyOutcome,
    DependencyStatus,
)
from app.application.canonical_json import canonical_json_digest
from app.application.transcripts.models import ReadTranscriptItem
from app.application.transcripts.read_models import ReadView
from app.domain import (
    CollectionCompletionContract,
    CollectionCompletionProof,
    CollectionCounts,
    CollectionPageManifest,
    EvidenceLocation,
    EvidenceBundle,
    InferenceResult,
    ItemReference,
    OutcomeStatus,
    ParameterKey,
    Provenance,
    ProviderCollectionBatch,
    ProviderQuality,
    TimeRange,
    UpstreamResultRecord,
    UpstreamResultRequest,
    canonical_model_bytes,
)


@dataclass(frozen=True, slots=True)
class ResolvedInferenceResult:
    result: InferenceResult
    closure_refs: tuple[ItemReference, ...]


class UpstreamInferenceResultReader(Protocol):
    async def resolve(
        self,
        outcome: DependencyOutcome,
    ) -> ResolvedInferenceResult: ...


class RegisteredTranscriptReader(Protocol):
    async def read_registered_session_items(
        self,
        run_id: UUID,
        session_id: UUID,
        view: ReadView,
    ) -> tuple[ReadTranscriptItem, ...]: ...


class TranscriptUpstreamInferenceResultReader:
    """Resolve a committed result and its registered same-session closure."""

    def __init__(self, reader: RegisteredTranscriptReader) -> None:
        self._reader = reader

    async def resolve(
        self,
        outcome: DependencyOutcome,
    ) -> ResolvedInferenceResult:
        if outcome.result_ref is None:
            raise ValueError("dependency outcome does not reference a result")
        items = await self._reader.read_registered_session_items(
            outcome.run_id,
            outcome.session_id,
            ReadView.AUTO,
        )
        item = next(
            (
                candidate
                for candidate in items
                if candidate.item.item_id == outcome.result_ref.item_id
            ),
            None,
        )
        if (
            item is None
            or item.item.content_type != "inference_result"
            or item.item.content_schema_version != 1
            or not isinstance(item.body, InferenceResult)
        ):
            raise ValueError("dependency result is not inference_result v1")
        closure_ids = _session_result_closure_ids(item, items)
        if not closure_ids or outcome.result_ref.item_id in closure_ids:
            raise ValueError("dependency result requires a distinct reference closure")
        return ResolvedInferenceResult(
            result=item.body,
            closure_refs=tuple(
                ItemReference(item_id=str(item_id)) for item_id in closure_ids
            ),
        )


def validate_resolved_outcome(
    outcome: DependencyOutcome,
    resolved: ResolvedInferenceResult,
) -> None:
    if outcome.result_ref is None or outcome.result_summary is None:
        raise ValueError("resolved outcome requires result identity")
    result = resolved.result
    summary = outcome.result_summary
    if (
        result.run_id != outcome.run_id
        or result.session_id != outcome.session_id
        or result.target_comid != outcome.target_comid
        or result.result_id != summary.result_id
        or summary.target_comid != result.target_comid
        or summary.risk_level != result.overall_assessment.risk_level
        or summary.availability != result.overall_assessment.availability
        or summary.overall_confidence != result.overall_confidence
    ):
        raise ValueError("dependency result identity or summary does not match")
    closure_ids = tuple(reference.item_id for reference in resolved.closure_refs)
    if not closure_ids or len(closure_ids) != len(set(closure_ids)):
        raise ValueError("dependency result requires a unique reference closure")
    if str(outcome.result_ref.item_id) in closure_ids:
        raise ValueError("result item must be distinct from its reference closure")

    points = {
        point.cell_id: (series.parameter.key, point)
        for series in result.series
        for point in series.points
    }
    eligible = {
        cell_id
        for cell_id, (_, point) in points.items()
        if point.outcome_status in {OutcomeStatus.QUANTIFIED, OutcomeStatus.DIRECTIONAL}
    }
    actual = {reference.cell_id for reference in outcome.usable_cell_refs}
    if outcome.status is DependencyStatus.USABLE:
        if actual != eligible:
            raise ValueError("usable cell references must exactly cover eligible cells")
        for reference in outcome.usable_cell_refs:
            parameter, point = points[reference.cell_id]
            expected_window = _window_id(parameter, point.cell_id)
            if (
                reference.item_id != outcome.result_ref.item_id
                or reference.window_id != expected_window
            ):
                raise ValueError("usable cell reference does not match its result cell")
    elif eligible:
        raise ValueError("insufficient result cannot contain usable cells")


def project_upstream_results(
    request: UpstreamResultRequest,
    dependencies: tuple[tuple[DependencyOutcome, ResolvedInferenceResult], ...],
) -> ProviderCollectionBatch[UpstreamResultRecord]:
    allowed_parameters = set(request.parameter_keys)
    allowed_windows = set(request.affected_window_ids)
    records: list[UpstreamResultRecord] = []
    for outcome, resolved in dependencies:
        result = resolved.result
        points = {
            point.cell_id: (series.parameter.key, point)
            for series in result.series
            for point in series.points
        }
        if outcome.result_ref is None:
            raise ValueError("usable dependency does not reference its result")
        for reference in outcome.usable_cell_refs:
            parameter, point = points[reference.cell_id]
            window_id = _window_id(parameter, point.cell_id)
            if parameter not in allowed_parameters or window_id not in allowed_windows:
                continue
            provider_record_id = (
                f"upstream:{outcome.result_ref.item_id}:{point.cell_id}"
            )
            records.append(UpstreamResultRecord(
                record_id=provider_record_id,
                provider="analysis_run",
                channel="upstream_result",
                source_uri=None,
                retrieved_at=outcome.completed_at,
                valid_time_range=TimeRange(
                    start=point.start_time,
                    end=point.end_time,
                ),
                location=EvidenceLocation(
                    point=None,
                    comid=outcome.target_comid,
                    station_id=None,
                    distance_to_target_km=None,
                ),
                provider_quality=ProviderQuality(),
                provenance=Provenance(
                    operation_id=request.operation_id,
                    provider_record_id=provider_record_id,
                ),
                upstream_session_id=str(outcome.session_id),
                result_ref=ItemReference(item_id=str(outcome.result_ref.item_id)),
                parameter=parameter,
                window_id=window_id,
                result_kind=point.outcome_status.value,
                result_source=point.source,
                confidence=point.confidence,
                evidence_closure_refs=resolved.closure_refs,
            ))
    ordered = tuple(sorted(
        records,
        key=lambda record: (
            record.location.comid or "",
            record.parameter.code_system,
            record.parameter.code,
            record.window_id,
            record.record_id,
        ),
    ))
    response_bytes = sum(len(canonical_model_bytes(record)) for record in ordered)
    page_identity = f"run-local:{request.operation_id}"
    page = CollectionPageManifest(
        page_identity=page_identity,
        record_count=len(ordered),
        response_bytes=response_bytes,
        page_complete=True,
    )
    proof_values = {
        "contract": CollectionCompletionContract.SINGLE_RESPONSE.value,
        "complete": True,
        "terminal_identity": page_identity,
        "pages": [page.model_dump(mode="json")],
    }
    return ProviderCollectionBatch[UpstreamResultRecord](
        operation_id=request.operation_id,
        records=ordered,
        page_or_chunk_manifests=(page,),
        completed_ranges=(),
        counts=CollectionCounts(
            requests=1,
            pages=1,
            records=len(ordered),
            response_bytes=response_bytes,
        ),
        completion_proof=CollectionCompletionProof(
            contract=CollectionCompletionContract.SINGLE_RESPONSE,
            complete=True,
            terminal_identity=page_identity,
            digest=canonical_json_digest(proof_values),
        ),
    )


def _session_result_closure_ids(
    result_item: ReadTranscriptItem,
    session_items: tuple[ReadTranscriptItem, ...],
) -> tuple[UUID, ...]:
    result = result_item.body
    if not isinstance(result, InferenceResult):
        raise ValueError("dependency result is not inference_result v1")
    evidence_ids = {
        reference.evidence_id for reference in result.evidence_chain.references
    }
    bundles = tuple(
        item
        for item in session_items
        if isinstance(item.body, EvidenceBundle)
        and evidence_ids
        <= {reference.evidence_id for reference in item.body.fragment_refs}
    )
    if bundles:
        selected = bundles[-1]
        bundle = selected.body
        if not isinstance(bundle, EvidenceBundle):
            raise ValueError("dependency result bundle is invalid")
        original = (
            selected.item.item_id,
            *(UUID(reference.item_id) for reference in bundle.fragment_refs),
            *(UUID(item_id) for item_id in bundle.source_record_item_refs),
            *(UUID(reference.item_id) for reference in bundle.closure_refs),
            *(UUID(reference.item_id) for reference in bundle.tool_result_refs),
        )
    else:
        original = tuple(
            UUID(reference)
            for reference in (
                *result.analysis_detail.transcript_item_refs,
                *(
                    reference
                    for step in result.analysis_detail.steps
                    for reference in step.transcript_item_refs
                ),
            )
        )
    closure = tuple(dict.fromkeys(original))
    session_item_ids = {item.item.item_id for item in session_items}
    if not closure or not set(closure) <= session_item_ids:
        raise ValueError("dependency result reference closure is incomplete")
    return closure


def _window_id(parameter: ParameterKey, cell_id: str) -> str:
    prefix = f"cell:{parameter.code_system}:{parameter.code}:"
    if not cell_id.startswith(prefix) or cell_id == prefix:
        raise ValueError("result cell ID does not preserve parameter/window identity")
    return cell_id.removeprefix(prefix)

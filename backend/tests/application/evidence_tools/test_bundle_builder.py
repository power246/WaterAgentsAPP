import asyncio
from functools import wraps

import pytest
from pydantic import ValidationError

from app.application.evidence_tools import (
    EvidenceBundleBuilder,
    EvidenceCollectionInput,
    EvidenceCollectionService,
    EvidenceScopeViolationError,
)
from app.domain import (
    CollectionCompletionProof,
    CollectionCounts,
    CollectionPageManifest,
    ConversionPayload,
    EvidenceApplicability,
    EvidenceBundle,
    EvidenceCellRole,
    EvidenceCellStatus,
    EvidenceDerivation,
    EvidenceFragment,
    EvidenceFragmentReference,
    EvidenceLocation,
    EvidenceResultEligibility,
    ItemReference,
    ParameterProvider,
    ProviderCollectionBatch,
    TimeRange,
    canonical_model_bytes,
)
from app.infrastructure.config import load_parameter_capability_registry
from tests.application.evidence_tools.fakes import FixedClock, MemoryEvidenceStore
from tests.application.evidence_tools.test_collection_service import observation


def run_async(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return wrapped


def provider_batch(scope, records, *, complete=True, completed_ranges=None):
    if completed_ranges is None:
        completed_ranges = (
            TimeRange(
                start=scope.time_windows[0].time_range.start,
                end=scope.time_windows[-1].time_range.end,
            ),
        ) if complete else ()
    page_id = "page:bundle"
    return ProviderCollectionBatch(
        operation_id="provider-bundle",
        records=tuple(records),
        page_or_chunk_manifests=(
            CollectionPageManifest(
                page_identity=page_id,
                record_count=len(records),
                response_bytes=512,
                page_complete=complete,
            ),
        ),
        completed_ranges=completed_ranges,
        counts=CollectionCounts(
            requests=1,
            pages=1,
            records=len(records),
            response_bytes=512,
        ),
        completion_proof=CollectionCompletionProof(
            contract="single_response",
            complete=complete,
            terminal_identity=page_id if complete else None,
            digest=f"digest:{complete}:{len(records)}",
        ),
        incomplete_reason=None if complete else "incomplete_source",
    )


def collection(scope, provider_batch):
    return EvidenceCollectionInput(
        provider=ParameterProvider.WDFN_TIME_SERIES.value,
        channel="continuous",
        request_range=scope.query_envelope,
        requested_series=("series-do", "series-flow"),
        parameter_keys=(
            scope.requested_parameters[0],
            scope.supporting_parameters[0],
        ),
        affected_window_ids=tuple(item.window_id for item in scope.time_windows),
        batch=provider_batch,
        station_id="USGS-123",
    )


async def assembled(scope, *, complete=True, completed_ranges=None):
    records = tuple(
        record
        for index, _ in enumerate(scope.time_windows)
        for record in (
            observation(scope, index * 2, window_index=index),
            observation(
                scope,
                index * 2 + 1,
                parameter=scope.supporting_parameters[0],
                window_index=index,
            ),
        )
    )
    store = MemoryEvidenceStore()
    service = EvidenceCollectionService(
        registry=load_parameter_capability_registry(),
        writer=store,
    )
    artifacts = await service.process(
        scope,
        operation_id="bundle-collection",
        collections=(
            collection(
                scope,
                provider_batch(
                    scope,
                    records,
                    complete=complete,
                    completed_ranges=completed_ranges,
                ),
            ),
        ),
    )
    store.seed_scope("tool-result-1")
    return store, artifacts


@run_async
async def test_bundle_has_exact_target_and_discharge_cells_with_reference_closure(
    evidence_scope,
):
    store, artifacts = await assembled(evidence_scope)
    builder = EvidenceBundleBuilder(reader=store, writer=store)
    bundle = await builder.build(
        evidence_scope,
        fragment_refs=(*artifacts.fragment_refs, *artifacts.collection_fact_refs),
        tool_result_refs=(ItemReference(item_id="tool-result-1"),),
        created_at=FixedClock().now(),
    )

    assert len(bundle.cells) == len(evidence_scope.time_windows) * 2
    assert {cell.role for cell in bundle.cells} == {
        EvidenceCellRole.TARGET,
        EvidenceCellRole.REQUIRED_SUPPORT,
    }
    assert {cell.status for cell in bundle.cells} == {EvidenceCellStatus.SUPPORTED}
    assert bundle.source_record_item_refs
    assert not hasattr(bundle, "fragments")


@run_async
@pytest.mark.parametrize("missing_role", [EvidenceCellRole.TARGET, EvidenceCellRole.REQUIRED_SUPPORT])
async def test_target_and_discharge_missing_states_are_independent(
    evidence_scope,
    missing_role,
):
    store, artifacts = await assembled(evidence_scope)
    parameter = (
        evidence_scope.requested_parameters[0]
        if missing_role is EvidenceCellRole.TARGET
        else evidence_scope.supporting_parameters[0]
    )
    first_window = evidence_scope.time_windows[0]
    refs = tuple(
        reference
        for reference in (*artifacts.fragment_refs, *artifacts.collection_fact_refs)
        if not (
            (fragment := store.fragments[(reference.item_id, reference.evidence_id)])
            .parameter_refs
            == (parameter,)
            and fragment.time_range == first_window.time_range
            and fragment.data_type != "collection_fact"
        )
    )
    bundle = await EvidenceBundleBuilder(reader=store, writer=store).build(
        evidence_scope,
        fragment_refs=refs,
        tool_result_refs=(ItemReference(item_id="tool-result-1"),),
        created_at=FixedClock().now(),
    )
    cells = {
        (cell.window_id, cell.role): cell
        for cell in bundle.cells
    }
    missing = cells[(first_window.window_id, missing_role)]
    other_role = (
        EvidenceCellRole.REQUIRED_SUPPORT
        if missing_role is EvidenceCellRole.TARGET
        else EvidenceCellRole.TARGET
    )
    assert missing.status is EvidenceCellStatus.UNSUPPORTED
    assert cells[(first_window.window_id, other_role)].status is EvidenceCellStatus.SUPPORTED


@run_async
async def test_incomplete_windows_block_without_erasing_independently_complete_windows(
    evidence_scope,
):
    midpoint = len(evidence_scope.time_windows) // 2
    completed = (
        TimeRange(
            start=evidence_scope.time_windows[0].time_range.start,
            end=evidence_scope.time_windows[midpoint - 1].time_range.end,
        ),
    )
    store, artifacts = await assembled(
        evidence_scope,
        complete=False,
        completed_ranges=completed,
    )
    bundle = await EvidenceBundleBuilder(reader=store, writer=store).build(
        evidence_scope,
        fragment_refs=(*artifacts.fragment_refs, *artifacts.collection_fact_refs),
        tool_result_refs=(ItemReference(item_id="tool-result-1"),),
        created_at=FixedClock().now(),
    )
    by_window = {
        window.window_id: {
            cell.status for cell in bundle.cells if cell.window_id == window.window_id
        }
        for window in evidence_scope.time_windows
    }
    assert all(
        by_window[item.window_id] == {EvidenceCellStatus.SUPPORTED}
        for item in evidence_scope.time_windows[:midpoint]
    )
    assert all(
        by_window[item.window_id] == {EvidenceCellStatus.BLOCKED}
        for item in evidence_scope.time_windows[midpoint:]
    )


@run_async
async def test_all_unproven_windows_produce_a_deterministic_all_blocked_bundle(
    evidence_scope,
):
    store, artifacts = await assembled(
        evidence_scope,
        complete=False,
        completed_ranges=(),
    )
    bundle = await EvidenceBundleBuilder(reader=store, writer=store).build(
        evidence_scope,
        fragment_refs=(*artifacts.fragment_refs, *artifacts.collection_fact_refs),
        tool_result_refs=(ItemReference(item_id="tool-result-1"),),
        created_at=FixedClock().now(),
    )
    assert {cell.status for cell in bundle.cells} == {EvidenceCellStatus.BLOCKED}
    assert {reason for cell in bundle.cells for reason in cell.reasons} == {
        "incomplete_source"
    }


@run_async
async def test_builder_rejects_missing_duplicate_cross_scope_and_unpersisted_refs(
    evidence_scope,
):
    store, artifacts = await assembled(evidence_scope)
    refs = (*artifacts.fragment_refs, *artifacts.collection_fact_refs)
    builder = EvidenceBundleBuilder(reader=store, writer=store)
    with pytest.raises(EvidenceScopeViolationError):
        await builder.build(
            evidence_scope,
            fragment_refs=(*refs, refs[0]),
            tool_result_refs=(ItemReference(item_id="tool-result-1"),),
            created_at=FixedClock().now(),
        )
    with pytest.raises(EvidenceScopeViolationError):
        await builder.build(
            evidence_scope,
            fragment_refs=(EvidenceFragmentReference(item_id="missing", evidence_id="missing"),),
            tool_result_refs=(),
            created_at=FixedClock().now(),
        )
    with pytest.raises(EvidenceScopeViolationError):
        await builder.build(
            evidence_scope,
            fragment_refs=refs,
            tool_result_refs=(ItemReference(item_id="unpersisted-result"),),
            created_at=FixedClock().now(),
        )

    fragment_item = refs[0].item_id
    store.seed_scope(fragment_item, run_id="other-run")
    with pytest.raises(EvidenceScopeViolationError):
        await builder.build(
            evidence_scope,
            fragment_refs=refs,
            tool_result_refs=(ItemReference(item_id="tool-result-1"),),
            created_at=FixedClock().now(),
        )


@run_async
async def test_conversion_cycle_is_rejected_before_bundle_assembly(evidence_scope):
    store = MemoryEvidenceStore()
    window = evidence_scope.time_windows[0].time_range
    references = (
        EvidenceFragmentReference(item_id="fragment-a", evidence_id="conversion-a"),
        EvidenceFragmentReference(item_id="fragment-b", evidence_id="conversion-b"),
    )
    for reference, dependency in zip(references, reversed(references), strict=True):
        fragment = EvidenceFragment(
            evidence_id=reference.evidence_id,
            source="deterministic_conversion",
            collection_status="available",
            target_comid=evidence_scope.agent_comid,
            location=EvidenceLocation(
                point=evidence_scope.target_node.point,
                comid=evidence_scope.agent_comid,
                station_id=None,
                distance_to_target_km=0,
            ),
            time_range=window,
            parameter_refs=(evidence_scope.requested_parameters[0],),
            data_type="conversion",
            applicability=EvidenceApplicability.DIRECT_MEASUREMENT,
            series_identity="series-do",
            quality_class="valid",
            quality_flags=(),
            coverage={
                "status": "partial",
                "observed_count": 1,
                "covered_duration_seconds": 0,
                "window_duration_seconds": int((window.end - window.start).total_seconds()),
            },
            payload=ConversionPayload(
                value="8",
                unit="mg/L",
                input_evidence_ref=dependency,
                conversion_id="unit:mg-l->mg-l:v1",
            ),
            source_record_refs=(),
            derivation=EvidenceDerivation(
                algorithm="decimal_unit_conversion",
                algorithm_version=1,
                policy_id="unit:mg-l->mg-l:v1",
                registry_digest="registry",
            ),
            provenance={"operation_id": "conversion"},
            result_eligibility=EvidenceResultEligibility.OBSERVED_OR_INFERRED,
        )
        store.seed_fragment(reference, fragment)
        store.seed_scope(reference.item_id)
    with pytest.raises(EvidenceScopeViolationError):
        await EvidenceBundleBuilder(reader=store, writer=store).build(
            evidence_scope,
            fragment_refs=references,
            tool_result_refs=(),
            created_at=FixedClock().now(),
        )


@run_async
async def test_bundle_serialization_is_order_independent_and_write_failure_is_not_committed(
    evidence_scope,
):
    store, artifacts = await assembled(evidence_scope)
    refs = (*artifacts.fragment_refs, *artifacts.collection_fact_refs)
    builder = EvidenceBundleBuilder(reader=store, writer=store)
    first = await builder.build(
        evidence_scope,
        fragment_refs=refs,
        tool_result_refs=(ItemReference(item_id="tool-result-1"),),
        created_at=FixedClock().now(),
    )
    second = await builder.build(
        evidence_scope,
        fragment_refs=tuple(reversed(refs)),
        tool_result_refs=(ItemReference(item_id="tool-result-1"),),
        created_at=FixedClock().now(),
    )
    assert canonical_model_bytes(first) == canonical_model_bytes(second)

    values = first.model_dump(mode="python")
    values["fragments"] = []
    with pytest.raises(ValidationError):
        EvidenceBundle.model_validate(values)

    store.fail_bundle_write = True
    with pytest.raises(RuntimeError, match="bundle write failed"):
        await builder.build_and_write(
            evidence_scope,
            fragment_refs=refs,
            tool_result_refs=(ItemReference(item_id="tool-result-1"),),
            created_at=FixedClock().now(),
        )
    assert store.bundles == []

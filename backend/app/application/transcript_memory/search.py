from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from heapq import heappop, heappush
from math import isfinite
from typing import Callable, Protocol

from app.application.evidence_tools.scope import EvidenceSessionScope
from app.application.transcript_memory.case_policy import (
    canonical_intent_tokens,
    meteorological_season,
    meteorological_season_year,
)
from app.application.transcript_memory.embedding_contract import (
    EmbeddingContractError,
    validated_embedding_batch,
)
from app.application.transcript_memory.generations import (
    GenerationService,
    GenerationUnavailableError,
)
from app.application.transcript_memory.projection_builder import (
    RAGProjectionBuildError,
    build_prepared_projection,
)
from app.application.transcript_memory.search_policies import (
    RAGSearchPolicyError,
    matching_projection_windows,
    stable_candidate_order,
)
from app.application.transcript_memory.source_closure import (
    RAGSourceClosureError,
    SourceClosureExpectation,
    validate_projection_expectation,
)
from app.application.transcript_memory.source_models import RAGCaseSource
from app.application.transcript_memory.source_reader import (
    RAGSourceReadError,
    RAGSourceUnavailableError,
)
from app.domain import (
    AuthorizedRAGComid,
    AuthorizedRAGSearchQuery,
    CanonicalCaseQueryV1,
    EmbeddingVector,
    EvidenceLocation,
    EvidenceQualityClass,
    GeoPoint,
    HistoricalCaseRef,
    HistoricalResultProjection,
    HistoricalSearchIntentV1,
    HistoricalSearchStrategy,
    HistoricalSupportingFragmentProjection,
    HistoricalTopologyRelation,
    HistoricalTranscriptRecord,
    HydrologyParticipation,
    InferenceMode,
    MemoryMode,
    MeteorologicalSeason,
    Provenance,
    ProviderQuality,
    RAGCandidateRow,
    RAGCaseWindowV1,
    RAGIndexGenerationStatus,
    RAGQualityTier,
    TimeRange,
)
from app.domain.hydrology.waterbody_geometry import point_to_linestring_distance_km
from app.domain.time.timezone import get_iana_timezone
from app.ports import (
    EmbeddingCancelledError,
    EmbeddingPort,
    InvalidPortResponseError,
    PortConfigurationError,
    PortRateLimitError,
    PortResponseLimitError,
    PortTimeoutError,
    PortUnavailableError,
    RAGIndexPort,
)

MAX_INTERNAL_CANDIDATES = 50
MAX_RETURNED_CASES = 5
MAX_CASES_PER_SOURCE_RUN = 2
MAX_FRAGMENTS_PER_CASE = 2
DEFAULT_PREVIEW_MAX_BYTES = 16_384
NEARBY_RADIUS_KM = 25.0


class RAGSearchError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class RAGSearchUnavailableError(RAGSearchError):
    pass


class RAGSearchInvalidError(RAGSearchError):
    pass


class HistoricalCaseSourceLoader(Protocol):
    async def load_historical_case_source(
        self,
        case_ref: HistoricalCaseRef,
    ) -> RAGCaseSource: ...


@dataclass(frozen=True, slots=True)
class _PreparedQuery:
    canonical: CanonicalCaseQueryV1
    windows: tuple[RAGCaseWindowV1, ...]
    comids: tuple[AuthorizedRAGComid, ...]
    spatial_radius_km: float | None = None
    topology_max_hops: int | None = None
    topology_max_distance_km: float | None = None


class RAGSearchService:
    """Creates a session-bounded searcher; production M5 wiring remains in M8."""

    def __init__(
        self,
        *,
        generations: GenerationService,
        index: RAGIndexPort,
        source_loader: HistoricalCaseSourceLoader,
        embedding: EmbeddingPort | None,
        embedding_for_run: Callable[[str], EmbeddingPort | None] | None = None,
        preview_max_bytes: int = DEFAULT_PREVIEW_MAX_BYTES,
    ) -> None:
        if type(preview_max_bytes) is not int or preview_max_bytes < 2:
            raise ValueError("preview_max_bytes must be an integer of at least 2")
        self._generations = generations
        self._index = index
        self._source_loader = source_loader
        self._embedding = embedding
        self._embedding_for_run = embedding_for_run
        self._preview_max_bytes = preview_max_bytes

    def bind(self, scope: EvidenceSessionScope) -> RAGSearchSession:
        embedding = (
            self._embedding_for_run(scope.run_id)
            if self._embedding_for_run is not None else self._embedding
        )
        return RAGSearchSession(service=self, scope=scope, embedding=embedding)


class RAGSearchSession:
    """One sealed waterbody session and its successful semantic-vector cache."""

    def __init__(
        self, *, service: RAGSearchService, scope: EvidenceSessionScope,
        embedding: EmbeddingPort | None = None,
    ) -> None:
        self._service = service
        self._scope = scope
        self._embedding = embedding if embedding is not None else service._embedding
        self._embedding_cache: dict[tuple[str, str], EmbeddingVector] = {}

    def rebind(self, scope: EvidenceSessionScope) -> RAGSearchSession:
        updated = RAGSearchSession(service=self._service, scope=scope)
        updated._embedding_cache = self._embedding_cache
        return updated

    async def search(
        self,
        intent: HistoricalSearchIntentV1,
        *,
        operation_id: str,
        deadline: float | None = None,
    ) -> tuple[HistoricalTranscriptRecord, ...]:
        _validate_intent(self._scope, intent)
        try:
            async with self._service._generations.pin_active_generation() as generation:
                if generation.status is not RAGIndexGenerationStatus.ACTIVE:
                    raise RAGSearchInvalidError("historical_index_generation_invalid")
                prepared = _prepare_query(self._scope, intent)
                if not prepared.comids:
                    return ()
                embedding = await self._query_embedding(
                    generation.profile,
                    prepared.canonical,
                    intent.strategy,
                    deadline,
                )
                query = AuthorizedRAGSearchQuery(
                    generation_id=generation.generation_id,
                    profile_id=generation.profile.profile_id,
                    intent=intent,
                    current_comid=self._scope.agent_comid,
                    candidate_comids=tuple(item.comid for item in prepared.comids),
                    comid_scope=prepared.comids,
                    query_windows=prepared.windows,
                    canonical_query=prepared.canonical.canonical_text,
                    canonical_query_digest=prepared.canonical.canonical_digest,
                    spatial_radius_km=prepared.spatial_radius_km,
                    topology_max_hops=prepared.topology_max_hops,
                    topology_max_distance_km=prepared.topology_max_distance_km,
                    semantic_min_similarity=(
                        generation.profile.semantic_min_similarity
                        if intent.strategy
                        is HistoricalSearchStrategy.SEMANTIC_CASE_QUERY
                        else None
                    ),
                    query_embedding=embedding,
                )
                candidates = await self._service._index.search_candidates(query)
                if len(candidates) > MAX_INTERNAL_CANDIDATES:
                    raise RAGSearchInvalidError("historical_candidate_limit_exceeded")
                ordered = tuple(sorted(candidates, key=stable_candidate_order))
                if intent.strategy is HistoricalSearchStrategy.SEMANTIC_CASE_QUERY:
                    threshold = generation.profile.semantic_min_similarity
                    if any(
                        item.matched_by.vector_similarity is None
                        or item.matched_by.vector_similarity < threshold
                        for item in ordered
                    ):
                        raise RAGSearchInvalidError("historical_semantic_threshold_invalid")
                return await self._hydrate_and_fit(
                    query,
                    ordered,
                    operation_id=operation_id,
                    limit=min(intent.limit, MAX_RETURNED_CASES),
                )
        except GenerationUnavailableError as error:
            raise RAGSearchUnavailableError("index_building") from error
        except PortUnavailableError as error:
            raise RAGSearchUnavailableError("historical_index_unavailable") from error
        except (RAGSearchPolicyError, ValueError) as error:
            raise RAGSearchInvalidError("historical_index_invalid") from error

    def clear(self) -> None:
        self._embedding_cache.clear()

    async def _query_embedding(
        self,
        profile,
        canonical: CanonicalCaseQueryV1,
        strategy: HistoricalSearchStrategy,
        deadline: float | None,
    ) -> EmbeddingVector | None:
        if strategy is not HistoricalSearchStrategy.SEMANTIC_CASE_QUERY:
            return None
        key = (profile.profile_id, canonical.canonical_digest)
        cached = self._embedding_cache.get(key)
        if cached is not None:
            return cached
        if self._embedding is None:
            raise RAGSearchUnavailableError("embedding_unavailable")
        try:
            batch = await self._embedding.embed(
                profile,
                (canonical.canonical_text,),
                deadline,
                self._scope.cancel_token,
            )
            if batch.profile != profile:
                raise EmbeddingContractError("embedding profile does not match generation")
            validated = validated_embedding_batch(
                profile,
                tuple(vector.values for vector in batch.vectors),
                expected_count=1,
            )
        except (
            PortUnavailableError,
            PortTimeoutError,
            PortRateLimitError,
        ) as error:
            raise RAGSearchUnavailableError("embedding_unavailable") from error
        except EmbeddingCancelledError as error:
            raise RAGSearchUnavailableError("embedding_cancelled") from error
        except (
            EmbeddingContractError,
            InvalidPortResponseError,
            PortConfigurationError,
            PortResponseLimitError,
            ValueError,
        ) as error:
            raise RAGSearchInvalidError("embedding_protocol_invalid") from error
        vector = validated.vectors[0]
        self._embedding_cache[key] = vector
        return vector

    async def _hydrate_and_fit(
        self,
        query: AuthorizedRAGSearchQuery,
        candidates: tuple[RAGCandidateRow, ...],
        *,
        operation_id: str,
        limit: int,
    ) -> tuple[HistoricalTranscriptRecord, ...]:
        records: list[HistoricalTranscriptRecord] = []
        source_results: set[tuple[str, str, str]] = set()
        run_counts: dict[str, int] = {}
        preview_bytes = 2
        too_large = 0
        for candidate in candidates:
            source_result = (
                candidate.case_ref.run_id,
                candidate.case_ref.session_id,
                candidate.case_ref.result_item_id,
            )
            run_id = candidate.case_ref.run_id
            if (
                source_result in source_results
                or run_counts.get(run_id, 0) >= MAX_CASES_PER_SOURCE_RUN
            ):
                continue
            record = await self._hydrate_one(
                query,
                candidate,
                operation_id=operation_id,
            )
            encoded_bytes = len(record.model_dump_json().encode("utf-8"))
            incremental = encoded_bytes + (1 if records else 0)
            if preview_bytes + incremental > self._service._preview_max_bytes:
                too_large += 1
                continue
            records.append(record)
            preview_bytes += incremental
            source_results.add(source_result)
            run_counts[run_id] = run_counts.get(run_id, 0) + 1
            if len(records) == limit:
                break
        if not records and too_large:
            raise RAGSearchInvalidError("result_too_large")
        return tuple(records)

    async def _hydrate_one(
        self,
        query: AuthorizedRAGSearchQuery,
        candidate: RAGCandidateRow,
        *,
        operation_id: str,
    ) -> HistoricalTranscriptRecord:
        try:
            source = await self._service._source_loader.load_historical_case_source(
                candidate.case_ref
            )
            bundle = build_prepared_projection(
                source,
                generation_id=query.generation_id,
                profile_id=query.profile_id,
            )
            windows = matching_projection_windows(query, bundle.case)
            window_ids = tuple(window.window_id for window in windows)
            try:
                validate_projection_expectation(
                    source,
                    projection=bundle.case,
                    expectation=SourceClosureExpectation(
                        active_generation_id=query.generation_id,
                        case_ref=candidate.case_ref,
                        projection_digest=candidate.projection_digest,
                        authorized_comids=query.candidate_comids,
                        authorized_parameter_keys=query.intent.parameter_keys,
                        authorized_source_window_ids=window_ids,
                    ),
                )
            except RAGSourceClosureError as error:
                if error.code == "source_active_projection_mismatch":
                    raise RAGSearchInvalidError(
                        "historical_source_projection_mismatch"
                    ) from error
                raise
            selected_cells = tuple(
                sorted(
                    (
                        cell
                        for cell in bundle.case.result_projection.cells
                        if cell.parameter in query.intent.parameter_keys
                        and cell.window_id in window_ids
                    ),
                    key=lambda cell: (
                        cell.parameter.code_system,
                        cell.parameter.code,
                        cell.time_range.start,
                        cell.time_range.end,
                        cell.window_id,
                    ),
                )
            )
            if not selected_cells:
                raise RAGSearchInvalidError("historical_source_cells_missing")
            derived_quality = (
                RAGQualityTier.SUSPICIOUS
                if any(
                    fragment.quality_tier is RAGQualityTier.SUSPICIOUS
                    for fragment in bundle.fragments
                )
                else RAGQualityTier.VALID
            )
            if (
                derived_quality is not candidate.quality_tier
                or candidate.original_confidence is None
                or candidate.original_confidence
                != bundle.case.result_projection.overall_confidence
            ):
                raise RAGSearchInvalidError("historical_source_rank_metadata_mismatch")
            supporting = _supporting_fragments(bundle.fragments, selected_cells)
            result = HistoricalResultProjection(
                mode=bundle.case.result_projection.mode,
                source_completed_at=bundle.case.result_projection.source_completed_at,
                overall_risk=bundle.case.result_projection.overall_risk,
                overall_confidence=bundle.case.result_projection.overall_confidence,
                availability=bundle.case.result_projection.availability,
                cells=selected_cells,
            )
            node = next(
                (
                    item
                    for item in source.topology.body.nodes
                    if item.comid == candidate.case_ref.target_comid
                ),
                None,
            )
            if node is None or node.participation is not HydrologyParticipation.ACTIVE:
                raise RAGSearchInvalidError("historical_source_target_missing")
            start = min(cell.time_range.start for cell in selected_cells)
            end = max(cell.time_range.end for cell in selected_cells)
            return HistoricalTranscriptRecord(
                provider="wateragents_transcript",
                channel="historical_memory",
                source_uri=None,
                retrieved_at=bundle.case.source_completed_at,
                observed_time=None,
                valid_time_range=TimeRange(start=start, end=end),
                location=EvidenceLocation(
                    point=node.point,
                    comid=candidate.case_ref.target_comid,
                    station_id=None,
                    distance_to_target_km=_distance_for(query, candidate.case_ref.target_comid),
                ),
                provider_quality=ProviderQuality(
                    approval="validated_transcript",
                    qualifiers=(candidate.quality_tier.value,),
                ),
                raw_unit=None,
                provenance=Provenance(
                    operation_id=operation_id,
                    request_identity=query.canonical_query_digest,
                    provider_record_id=(
                        f"rag:{query.generation_id}:{candidate.case_ref.case_id}"
                    ),
                ),
                case_ref=candidate.case_ref,
                profile_id=query.profile_id,
                projection_id=candidate.case_ref.case_id,
                projection_digest=candidate.projection_digest,
                rag_score=candidate.rag_score,
                matched_by=candidate.matched_by,
                original_quality=EvidenceQualityClass(candidate.quality_tier.value),
                original_confidence=candidate.original_confidence,
                result_projection=result,
                supporting_fragments=supporting,
            )
        except RAGSourceUnavailableError as error:
            raise RAGSearchUnavailableError("historical_source_unavailable") from error
        except RAGSearchError:
            raise
        except (
            RAGSourceReadError,
            RAGSourceClosureError,
            RAGProjectionBuildError,
            RAGSearchPolicyError,
            ValueError,
        ) as error:
            raise RAGSearchInvalidError("historical_source_invalid") from error


def _validate_intent(
    scope: EvidenceSessionScope,
    intent: HistoricalSearchIntentV1,
) -> None:
    if scope.memory_mode is not MemoryMode.MEMORY_AUGMENTED:
        raise RAGSearchInvalidError("historical_scope_memory_mode_invalid")
    allowed_parameters = {
        *scope.requested_parameters,
        *scope.supporting_parameters,
        *(
            parameter
            for rule in scope.evidence_rule_plan.effective_rules
            for parameter in (rule.output_parameter, *rule.input_parameters)
        ),
    }
    if not set(intent.parameter_keys) <= allowed_parameters:
        raise RAGSearchInvalidError("historical_scope_parameter_invalid")
    available_windows = {window.window_id for window in scope.time_windows}
    if not set(intent.window_ids) <= available_windows:
        raise RAGSearchInvalidError("historical_scope_window_invalid")


def _prepare_query(
    scope: EvidenceSessionScope,
    intent: HistoricalSearchIntentV1,
) -> _PreparedQuery:
    windows_by_id = {window.window_id: window for window in scope.time_windows}
    query_windows = tuple(
        _query_window(scope, windows_by_id[window_id], intent)
        for window_id in intent.window_ids
    )
    canonical = CanonicalCaseQueryV1(
        mode=scope.mode,
        requested_parameter_keys=intent.parameter_keys,
        supporting_parameter_keys=tuple(
            parameter
            for parameter in scope.supporting_parameters
            if parameter not in intent.parameter_keys
        ),
        window_descriptors=tuple(
            window.canonical_descriptor() for window in query_windows
        ),
        normalized_intent_tokens=canonical_intent_tokens(
            scope.mode,
            scope.supporting_parameters,
        ),
    )
    strategy = intent.strategy
    if strategy is HistoricalSearchStrategy.EXACT_COMID_PARAMETER_WINDOW:
        return _PreparedQuery(
            canonical=canonical,
            windows=query_windows,
            comids=(AuthorizedRAGComid(comid=scope.agent_comid),),
        )
    if strategy is HistoricalSearchStrategy.NEARBY_COMID_PARAMETER:
        radius = min(NEARBY_RADIUS_KM, scope.topology.limits.max_distance_km)
        comids = _nearby_comids(scope, radius)
        return _PreparedQuery(
            canonical=canonical,
            windows=query_windows,
            comids=comids,
            spatial_radius_km=radius,
        )
    if strategy in {
        HistoricalSearchStrategy.PARAMETER_SEASON,
        HistoricalSearchStrategy.SEMANTIC_CASE_QUERY,
    }:
        comids = tuple(
            AuthorizedRAGComid(comid=node.comid)
            for node in scope.topology.nodes
            if node.participation is HydrologyParticipation.ACTIVE
        )
        return _PreparedQuery(
            canonical=canonical,
            windows=query_windows,
            comids=comids,
        )
    return _PreparedQuery(
        canonical=canonical,
        windows=query_windows,
        comids=_topology_comids(scope),
        topology_max_hops=scope.topology.limits.max_hops,
        topology_max_distance_km=scope.topology.limits.max_distance_km,
    )


def _query_window(scope, window, intent) -> RAGCaseWindowV1:
    zone = get_iana_timezone(scope.analysis_timezone)
    touched = (
        window.time_range.start.astimezone(zone),
        (window.time_range.end - timedelta(microseconds=1)).astimezone(zone),
    )
    months = tuple(sorted({value.month for value in touched}))
    seasons = tuple(
        sorted(
            {meteorological_season(value.month) for value in touched},
            key=lambda item: tuple(MeteorologicalSeason).index(item),
        )
    )
    season_years = {
        meteorological_season_year(value.year, value.month) for value in touched
    }
    if len(season_years) != 1:
        raise RAGSearchInvalidError("historical_query_season_invalid")
    duration = (window.time_range.end - window.time_range.start).total_seconds()
    if not duration.is_integer():
        raise RAGSearchInvalidError("historical_query_duration_invalid")
    window_kind = window.window_kind or scope.mode
    if window_kind is InferenceMode.MIXED:
        raise RAGSearchInvalidError("historical_query_window_mode_invalid")
    return RAGCaseWindowV1(
        window_id=window.window_id,
        time_range=window.time_range,
        window_kind=window_kind,
        local_months=months,
        season_buckets=seasons,
        season_year=season_years.pop(),
        duration_seconds=int(duration),
        resolution=scope.time_resolution,
        parameter_keys=intent.parameter_keys,
    )


def _nearby_comids(
    scope: EvidenceSessionScope,
    radius: float,
) -> tuple[AuthorizedRAGComid, ...]:
    current = scope.topology.geometries.get(scope.target_node.geometry_ref or "")
    if current is None:
        return ()
    matches: list[AuthorizedRAGComid] = []
    for node in scope.topology.nodes:
        if (
            node.comid == scope.agent_comid
            or node.participation is not HydrologyParticipation.ACTIVE
            or node.geometry_ref is None
        ):
            continue
        candidate = scope.topology.geometries.get(node.geometry_ref)
        if candidate is None:
            continue
        distance = _geometry_distance(current.full_coordinates, candidate.full_coordinates)
        if distance <= radius:
            matches.append(
                AuthorizedRAGComid(
                    comid=node.comid,
                    spatial_distance_km=distance,
                )
            )
    return tuple(
        sorted(
            matches,
            key=lambda item: (item.spatial_distance_km, item.comid),
        )
    )


def _geometry_distance(first, second) -> float:
    if any(
        _segments_intersect(first_start, first_end, second_start, second_end)
        for first_start, first_end in zip(first, first[1:])
        for second_start, second_end in zip(second, second[1:])
    ):
        return 0.0
    distances = [
        point_to_linestring_distance_km(
            GeoPoint(latitude=latitude, longitude=longitude),
            second,
        )
        for longitude, latitude in first
    ]
    distances.extend(
        point_to_linestring_distance_km(
            GeoPoint(latitude=latitude, longitude=longitude),
            first,
        )
        for longitude, latitude in second
    )
    distance = min(distances)
    if not isfinite(distance) or distance < 0:
        raise RAGSearchInvalidError("historical_saved_geometry_invalid")
    return distance


def _segments_intersect(first_start, first_end, second_start, second_end) -> bool:
    def orientation(start, end, point) -> float:
        return (end[0] - start[0]) * (point[1] - start[1]) - (
            end[1] - start[1]
        ) * (point[0] - start[0])

    values = (
        orientation(first_start, first_end, second_start),
        orientation(first_start, first_end, second_end),
        orientation(second_start, second_end, first_start),
        orientation(second_start, second_end, first_end),
    )
    if values[0] == values[1] == values[2] == values[3] == 0:
        return (
            max(min(first_start[0], first_end[0]), min(second_start[0], second_end[0]))
            <= min(max(first_start[0], first_end[0]), max(second_start[0], second_end[0]))
            and max(min(first_start[1], first_end[1]), min(second_start[1], second_end[1]))
            <= min(max(first_start[1], first_end[1]), max(second_start[1], second_end[1]))
        )
    return values[0] * values[1] <= 0 and values[2] * values[3] <= 0


def _topology_comids(scope: EvidenceSessionScope) -> tuple[AuthorizedRAGComid, ...]:
    edges = tuple(edge for edge in scope.topology.edges if edge.participating)
    matches: list[AuthorizedRAGComid] = []
    for node in scope.topology.nodes:
        if (
            node.comid == scope.agent_comid
            or node.participation is not HydrologyParticipation.ACTIVE
        ):
            continue
        ancestor = _saved_path(edges, node.comid, scope.agent_comid)
        descendant = _saved_path(edges, scope.agent_comid, node.comid)
        if ancestor is not None:
            relation = HistoricalTopologyRelation.ANCESTOR
            hops, distance = ancestor
        elif descendant is not None:
            relation = HistoricalTopologyRelation.DESCENDANT
            hops, distance = descendant
        else:
            continue
        matches.append(
            AuthorizedRAGComid(
                comid=node.comid,
                topology_relation=relation,
                topology_hops=hops,
                topology_path_distance_km=distance,
            )
        )
    return tuple(
        sorted(
            matches,
            key=lambda item: (
                0
                if item.topology_relation is HistoricalTopologyRelation.ANCESTOR
                else 1,
                item.topology_hops,
                item.topology_path_distance_km,
                item.comid,
            ),
        )
    )


def _saved_path(edges, start: str, target: str) -> tuple[int, float] | None:
    adjacency: dict[str, list[tuple[str, float]]] = {}
    for edge in edges:
        adjacency.setdefault(edge.upstream_comid, []).append(
            (edge.downstream_comid, edge.distance_km)
        )
    queue: list[tuple[int, float, str]] = [(0, 0.0, start)]
    best: dict[str, tuple[int, float]] = {}
    while queue:
        hops, distance, current = heappop(queue)
        if current == target:
            return hops, distance
        if best.get(current, (hops + 1, distance + 1)) <= (hops, distance):
            continue
        best[current] = (hops, distance)
        for next_comid, edge_distance in sorted(adjacency.get(current, ())):
            heappush(queue, (hops + 1, distance + edge_distance, next_comid))
    return None


def _supporting_fragments(fragments, selected_cells):
    cells = {(cell.parameter, cell.window_id): cell for cell in selected_cells}
    projections: list[HistoricalSupportingFragmentProjection] = []
    seen: set[tuple[str, str]] = set()
    for fragment in sorted(
        fragments,
        key=lambda item: (
            item.parameter.code_system,
            item.parameter.code,
            item.window_id,
            item.source_fragment_item_id,
            item.evidence_id,
        ),
    ):
        cell = cells.get((fragment.parameter, fragment.window_id))
        identity = (fragment.source_fragment_item_id, fragment.evidence_id)
        if (
            cell is None
            or identity in seen
            or fragment.valid_time_range.start >= cell.time_range.end
            or fragment.valid_time_range.end <= cell.time_range.start
        ):
            continue
        seen.add(identity)
        projections.append(
            HistoricalSupportingFragmentProjection(
                original_item_id=fragment.source_fragment_item_id,
                evidence_id=fragment.evidence_id,
                parameter=fragment.parameter,
                valid_time_range=TimeRange(
                    start=max(
                        fragment.valid_time_range.start,
                        cell.time_range.start,
                    ),
                    end=min(
                        fragment.valid_time_range.end,
                        cell.time_range.end,
                    ),
                ),
                quality_class=EvidenceQualityClass(fragment.quality_tier.value),
            )
        )
        if len(projections) == MAX_FRAGMENTS_PER_CASE:
            break
    return tuple(projections)


def _distance_for(query: AuthorizedRAGSearchQuery, comid: str) -> float | None:
    if comid == query.current_comid:
        return 0.0
    scope = next(item for item in query.comid_scope if item.comid == comid)
    if scope.spatial_distance_km is not None:
        return scope.spatial_distance_km
    return scope.topology_path_distance_km

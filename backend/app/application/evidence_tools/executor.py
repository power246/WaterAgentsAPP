import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from app.application.analysis_runs.workflow import WorkflowStopped
from app.application.evidence_policy import (
    EvidencePolicyError,
    aggregate_observations,
    convert_value,
)
from app.application.evidence_tools.authorization import (
    authorize_evidence_tool_request,
    authorize_provider_request,
    build_water_observation_plans,
    canonical_request_key,
)
from app.application.evidence_tools.budget import EvidenceBudgetLedger
from app.application.evidence_tools.dedup import RunLocalSingleFlight
from app.application.evidence_tools.errors import (
    EvidenceAggregationNotAllowedError,
    EvidenceCollectionIncompleteError,
    FacilityRadiusTooSmallForBudgetError as EvidenceFacilityRadiusTooSmallError,
    EvidenceProducerUnavailableError,
    EvidenceScopeViolationError,
    EvidenceToolDeadlineExceededError,
    EvidenceToolError,
    EvidenceToolLimitExceededError,
    EvidenceUnitUnresolvedError,
    WaterbodyGeometryUnavailableError,
    map_evidence_error,
)
from app.application.evidence_tools.inspection_service import (
    EvidenceInspectionService,
)
from app.application.evidence_tools.collection_service import EvidenceCollectionService
from app.application.evidence_tools.models import (
    EvidenceClock,
    EvidenceCollectionArtifacts,
    EvidenceCollectionInput,
    EvidenceInspectionPage,
    EvidenceOperationValue,
    EvidencePolicyClass,
    EvidenceProducerStatus,
    EvidenceToolLimits,
    EvidenceToolReader,
    EvidenceToolWriter,
)
from app.application.evidence_tools.registry import EvidenceToolRegistry
from app.application.evidence_tools.scope import EvidenceSessionScope
from app.application.hydrology_topology import build_facility_corridor_plan
from app.application.hydrology_topology.corridor import (
    FacilityRadiusTooSmallForBudgetError,
    WaterbodyGeometryUnavailableError as CorridorGeometryUnavailableError,
)
from app.domain import (
    AggregatePayload,
    CollectionStatus,
    ConversionPayload,
    DirectRecordPayload,
    EvidenceApplicability,
    EvidenceDerivation,
    EvidenceFragment,
    EvidenceFragmentReference,
    EvidenceLocation,
    EvidenceQualityClass,
    EvidenceResultEligibility,
    EvidenceToolCounts,
    EvidenceToolExecutionStatus,
    EvidenceToolRequest,
    EvidenceToolResult,
    FacilityEvidenceRequest,
    ItemReference,
    ParameterCapabilityRegistry,
    ProviderCollectionBatch,
    Provenance,
    SafeEvidenceError,
    TranscriptEvidenceRequest,
    UpstreamResultRequest,
    WeatherEvidenceRequest,
)
from app.ports import (
    FacilityEvidencePort,
    TranscriptEvidencePort,
    UpstreamResultPort,
    WaterObservationPort,
    WeatherEvidencePort,
)


@dataclass(frozen=True, slots=True)
class _CompletedOperation:
    result: EvidenceToolResult
    result_ref: ItemReference
    value: EvidenceOperationValue


class _UtcClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class EvidenceToolExecutor:
    """The sole application entry point for all eight evidence tools."""

    def __init__(
        self,
        *,
        registry: EvidenceToolRegistry,
        capability_registry: ParameterCapabilityRegistry,
        writer: EvidenceToolWriter,
        reader: EvidenceToolReader,
        water_port: WaterObservationPort | None = None,
        weather_port: WeatherEvidencePort | None = None,
        facility_port: FacilityEvidencePort | None = None,
        upstream_port: UpstreamResultPort | None = None,
        transcript_port: TranscriptEvidencePort | None = None,
        limits: EvidenceToolLimits = EvidenceToolLimits(),
        clock: EvidenceClock | None = None,
        monotonic: Callable[[], float] | None = None,
        operation_id_factory: Callable[[], str] | None = None,
        single_flight: RunLocalSingleFlight[_CompletedOperation] | None = None,
    ) -> None:
        self._registry = registry
        self._capabilities = capability_registry
        self._writer = writer
        self._reader = reader
        self._water_port = water_port
        self._weather_port = weather_port
        self._facility_port = facility_port
        self._upstream_port = upstream_port
        self._transcript_port = transcript_port
        self._limits = limits
        self._clock = clock or _UtcClock()
        self._monotonic = monotonic
        self._operation_id_factory = operation_id_factory or (
            lambda: str(uuid4())
        )
        self._single_flight = single_flight or RunLocalSingleFlight()
        self._inspection = EvidenceInspectionService(
            reader,
            default_page_size=limits.inspection_default_page_size,
            max_page_size=limits.inspection_max_page_size,
        )
        self._collection_service = EvidenceCollectionService(
            registry=capability_registry,
            writer=writer,
            limits=limits,
        )
        self._budgets: dict[tuple[str, str], EvidenceBudgetLedger] = {}
        self._deadlines: dict[tuple[str, str], float] = {}
        self._inspection_pages: dict[tuple[str, str], EvidenceInspectionPage] = {}

    async def execute(
        self,
        scope: EvidenceSessionScope,
        request: EvidenceToolRequest,
    ) -> EvidenceToolResult:
        operation_id = self._operation_id_factory()
        descriptor = self._registry.get(request.tool_name)
        try:
            if descriptor is None:
                raise EvidenceScopeViolationError()
            authorize_evidence_tool_request(
                scope,
                request,
                max_agent_record_limit=self._limits.inspection_max_page_size,
            )
            await self._writer.write_call(
                scope,
                request,
                operation_id=operation_id,
                policy=descriptor.policy_class,
            )
            self._authorize_descriptor(scope, request, descriptor)
            scope.cancel_token.checkpoint()
            canonical_key = canonical_request_key(
                scope,
                request,
                policy_manifest_digest=self._capabilities.manifest_digest,
            )
            self._reserve_budget(scope, descriptor.policy_class, canonical_key)
            self._check_deadline(scope)

            async def work() -> _CompletedOperation:
                value = await self._dispatch(
                    scope,
                    request,
                    operation_id=operation_id,
                )
                self._check_deadline(scope)
                result = self._result_from_value(
                    request,
                    operation_id=operation_id,
                    value=value,
                )
                result_ref = await self._writer.write_result(scope, result)
                return _CompletedOperation(
                    result=result,
                    result_ref=result_ref,
                    value=value,
                )

            completed, reused = await self._single_flight.execute(
                scope.run_id,
                f"{scope.session_id}:{canonical_key}",
                work,
                cache_success=lambda item: (
                    item.value.cacheable
                    and item.result.execution_status
                    is EvidenceToolExecutionStatus.COMPLETED
                ),
            )
            if completed.value.inspection_page is not None:
                self._inspection_pages[(scope.run_id, request.tool_call_id)] = (
                    completed.value.inspection_page
                )
            if not reused:
                return completed.result
            return await self._write_reused_result(
                scope,
                request,
                operation_id=operation_id,
                completed=completed,
            )
        except (WorkflowStopped, asyncio.CancelledError):
            return await self._write_failure_result(
                scope,
                request,
                operation_id=operation_id,
                cancelled=True,
                error=SafeEvidenceError(
                    code="evidence_cancelled",
                    message="The evidence operation was cancelled.",
                    retryable=False,
                ),
            )
        except EvidencePolicyError as error:
            return await self._write_failure_result(
                scope,
                request,
                operation_id=operation_id,
                error=_map_policy_error(error),
            )
        except Exception as error:
            safe_error = map_evidence_error(error)
            return await self._write_failure_result(
                scope,
                request,
                operation_id=operation_id,
                error=safe_error,
            )

    async def cleanup_run(self, run_id: str) -> None:
        await self._single_flight.cleanup_run(run_id)
        self._budgets = {
            key: value for key, value in self._budgets.items() if key[0] != run_id
        }
        self._deadlines = {
            key: value for key, value in self._deadlines.items() if key[0] != run_id
        }
        self._inspection_pages = {
            key: value
            for key, value in self._inspection_pages.items()
            if key[0] != run_id
        }

    def inspection_page(
        self,
        run_id: str,
        tool_call_id: str,
    ) -> EvidenceInspectionPage | None:
        return self._inspection_pages.get((run_id, tool_call_id))

    def live_dedup_counts(self, run_id: str) -> tuple[int, int]:
        return self._single_flight.live_counts(run_id)

    def _authorize_descriptor(self, scope, request, descriptor) -> None:
        if scope.mode not in descriptor.allowed_modes:
            raise EvidenceScopeViolationError()
        if (
            descriptor.name == "collect_water_observations"
            and not scope.station_refs
        ):
            raise EvidenceScopeViolationError()
        if descriptor.producer_status is EvidenceProducerStatus.UNAVAILABLE:
            raise EvidenceProducerUnavailableError()
        supplied = set(
            request.options.model_dump(
                mode="python",
                exclude_none=True,
                exclude_defaults=True,
            )
        )
        if not supplied <= set(descriptor.allowed_option_fields):
            raise EvidenceScopeViolationError()
        options = request.options
        if request.tool_name == "collect_facility_evidence":
            if options.facility_radius_miles is None:
                raise EvidenceScopeViolationError()
        elif request.tool_name == "convert_evidence_unit":
            if (
                len(request.parameter_keys) != 1
                or len(request.window_ids) != 1
                or len(options.fragment_refs) != 1
                or options.target_unit_id is None
                or options.target_unit_id not in descriptor.allowed_target_unit_ids
            ):
                raise EvidenceScopeViolationError()
        elif request.tool_name == "aggregate_evidence":
            if (
                len(request.parameter_keys) != 1
                or len(request.window_ids) != 1
                or not options.source_record_refs
                or options.aggregation_method is None
                or options.aggregation_method.value
                not in descriptor.allowed_aggregation_methods
            ):
                raise EvidenceScopeViolationError()
        elif request.tool_name == "inspect_evidence_records":
            if options.source_item_id is None:
                raise EvidenceScopeViolationError()

    def _reserve_budget(
        self,
        scope: EvidenceSessionScope,
        policy_class: EvidencePolicyClass,
        canonical_key: str,
    ) -> None:
        ledger = self._budgets.setdefault(
            (scope.run_id, scope.session_id),
            EvidenceBudgetLedger(
                collection_limit=self._limits.collection_calls,
                transform_limit=self._limits.transform_calls,
            ),
        )
        kind = (
            "transform"
            if policy_class is EvidencePolicyClass.DETERMINISTIC
            else "collection"
        )
        ledger.reserve(kind, canonical_key)

    def _check_deadline(self, scope: EvidenceSessionScope) -> float:
        loop = asyncio.get_running_loop()
        monotonic = self._monotonic or loop.time
        now = monotonic()
        key = (scope.run_id, scope.session_id)
        total_deadline = self._deadlines.setdefault(
            key,
            now + self._limits.total_deadline_seconds,
        )
        if now >= total_deadline:
            raise EvidenceToolDeadlineExceededError()
        scope.cancel_token.checkpoint()
        return min(
            total_deadline,
            now + self._limits.provider_timeout_seconds,
        )

    async def _dispatch(
        self,
        scope,
        request,
        *,
        operation_id,
    ) -> EvidenceOperationValue:
        if request.tool_name == "collect_water_observations":
            plans = build_water_observation_plans(
                scope,
                request,
                operation_id=operation_id,
                registry=self._capabilities,
            )
            batches = []
            collections = []
            for plan in plans:
                scope.cancel_token.checkpoint()
                if batches:
                    self._ensure_another_collection_request(tuple(batches))
                if self._water_port is None:
                    raise EvidenceProducerUnavailableError()
                batch = await self._await_provider(
                    scope,
                    self._water_port.collect(
                        plan,
                        deadline=self._check_deadline(scope),
                    ),
                )
                batches.append(batch)
                collections.append(
                    EvidenceCollectionInput(
                        provider=plan.provider.value,
                        channel=plan.channel,
                        request_range=plan.request_range,
                        requested_series=plan.series_refs,
                        parameter_keys=plan.parameter_keys,
                        affected_window_ids=plan.affected_window_ids,
                        batch=batch,
                        station_id=plan.station_id,
                    )
                )
                self._validate_collected_batches(tuple(batches))
            return await self._finish_collection(
                scope,
                operation_id,
                tuple(collections),
            )

        if request.tool_name == "collect_weather_evidence":
            if self._weather_port is None:
                raise EvidenceProducerUnavailableError()
            kinds = (
                ("observation", "forecast")
                if request.options.include_forecast
                else ("observation",)
            )
            batches = []
            collections = []
            for index, kind in enumerate(kinds, start=1):
                scope.cancel_token.checkpoint()
                if batches:
                    self._ensure_another_collection_request(tuple(batches))
                plan = WeatherEvidenceRequest(
                    operation_id=f"{operation_id}-{index:03d}",
                    evidence_kind=kind,
                    location=scope.target_node.point,
                    variables=("temperature",),
                    request_range=scope.query_envelope,
                    affected_window_ids=request.window_ids,
                )
                authorize_provider_request(scope, plan, registry=self._capabilities)
                batch = await self._await_provider(
                    scope,
                    self._weather_port.collect(
                        plan,
                        deadline=self._check_deadline(scope),
                    ),
                )
                batches.append(batch)
                collections.append(
                    EvidenceCollectionInput(
                        provider="nws",
                        channel=kind,
                        request_range=plan.request_range,
                        requested_series=plan.variables,
                        parameter_keys=request.parameter_keys,
                        affected_window_ids=plan.affected_window_ids,
                        batch=batch,
                    )
                )
                self._validate_collected_batches(tuple(batches))
            return await self._finish_collection(
                scope,
                operation_id,
                tuple(collections),
            )

        if request.tool_name == "collect_facility_evidence":
            if self._facility_port is None:
                raise EvidenceProducerUnavailableError()
            try:
                plan = build_facility_corridor_plan(
                    scope.topology,
                    agent_radius_miles=request.options.facility_radius_miles,
                )
            except CorridorGeometryUnavailableError as error:
                raise WaterbodyGeometryUnavailableError() from error
            except FacilityRadiusTooSmallForBudgetError as error:
                raise EvidenceFacilityRadiusTooSmallError() from error
            provider_request = FacilityEvidenceRequest(
                operation_id=f"{operation_id}-001",
                geometry_ref=plan.geometry_ref,
                exact_radius_miles=float(plan.agent_radius_miles),
                provider_radius_miles=float(plan.provider_radius_miles),
                sample_centers=plan.centers,
                affected_window_ids=request.window_ids,
            )
            authorize_provider_request(
                scope,
                provider_request,
                registry=self._capabilities,
                max_facility_centers=self._limits.facility_max_centers,
            )
            batch = await self._await_provider(
                scope,
                self._facility_port.collect(
                    provider_request,
                    deadline=self._check_deadline(scope),
                ),
            )
            return await self._finish_collection(
                scope,
                operation_id,
                (
                    EvidenceCollectionInput(
                        provider="epa_echo",
                        channel="facility",
                        request_range=scope.query_envelope,
                        requested_series=(provider_request.geometry_ref,),
                        parameter_keys=request.parameter_keys,
                        affected_window_ids=provider_request.affected_window_ids,
                        batch=batch,
                        completion_applies_to_all_windows=True,
                    ),
                ),
            )

        if request.tool_name == "get_upstream_results":
            if self._upstream_port is None or not scope.direct_upstream_comids:
                raise EvidenceProducerUnavailableError()
            plan = UpstreamResultRequest(
                operation_id=f"{operation_id}-001",
                run_id=scope.run_id,
                session_id=scope.session_id,
                agent_comid=scope.agent_comid,
                direct_upstream_comids=scope.direct_upstream_comids,
                parameter_keys=request.parameter_keys,
                affected_window_ids=request.window_ids,
            )
            authorize_provider_request(scope, plan, registry=self._capabilities)
            batch = await self._await_provider(
                scope,
                self._upstream_port.collect(
                    plan,
                    deadline=self._check_deadline(scope),
                ),
            )
            return await self._finish_collection(
                scope,
                operation_id,
                (
                    EvidenceCollectionInput(
                        provider="analysis_run",
                        channel="upstream_result",
                        request_range=scope.query_envelope,
                        requested_series=tuple(
                            sorted(
                                (
                                    *plan.direct_upstream_comids,
                                    *(
                                        f"{item.code_system}:{item.code}"
                                        for item in plan.parameter_keys
                                    ),
                                )
                            )
                        ),
                        parameter_keys=plan.parameter_keys,
                        affected_window_ids=plan.affected_window_ids,
                        batch=batch,
                        completion_applies_to_all_windows=True,
                    ),
                ),
            )

        if request.tool_name == "search_transcript_evidence":
            if self._transcript_port is None:
                raise EvidenceProducerUnavailableError()
            plan = TranscriptEvidenceRequest(
                operation_id=f"{operation_id}-001",
                run_id=scope.run_id,
                session_id=scope.session_id,
                memory_mode=scope.memory_mode,
                query=scope.query,
                max_records=(
                    request.options.record_limit
                    or self._limits.inspection_default_page_size
                ),
            )
            authorize_provider_request(
                scope,
                plan,
                registry=self._capabilities,
                max_transcript_records=self._limits.inspection_max_page_size,
            )
            batch = await self._await_provider(
                scope,
                self._transcript_port.collect(
                    plan,
                    deadline=self._check_deadline(scope),
                ),
            )
            query_identity = sha256(plan.query.encode("utf-8")).hexdigest()
            return await self._finish_collection(
                scope,
                operation_id,
                (
                    EvidenceCollectionInput(
                        provider="transcript",
                        channel="historical",
                        request_range=scope.query_envelope,
                        requested_series=(f"query:sha256:{query_identity}",),
                        parameter_keys=request.parameter_keys,
                        affected_window_ids=request.window_ids,
                        batch=batch,
                        completion_applies_to_all_windows=True,
                    ),
                ),
            )

        if request.tool_name == "convert_evidence_unit":
            return await self._convert(scope, request, operation_id)
        if request.tool_name == "aggregate_evidence":
            return await self._aggregate(scope, request, operation_id)
        if request.tool_name == "inspect_evidence_records":
            page = await self._inspection.inspect(
                scope,
                source_item_id=request.options.source_item_id or "",
                offset=request.options.page_offset or 0,
                limit=request.options.record_limit,
            )
            return EvidenceOperationValue(
                source_record_refs=page.record_refs,
                records=len(page.records),
                inspection_page=page,
            )
        raise EvidenceScopeViolationError()

    async def _await_provider(
        self,
        scope: EvidenceSessionScope,
        operation: Awaitable[ProviderCollectionBatch],
    ) -> ProviderCollectionBatch:
        provider_task = asyncio.create_task(operation)
        cancellation_task = asyncio.create_task(scope.cancel_token.wait())
        try:
            done, _ = await asyncio.wait(
                (provider_task, cancellation_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancellation_task in done:
                provider_task.cancel()
                await asyncio.gather(provider_task, return_exceptions=True)
                raise WorkflowStopped()
            cancellation_task.cancel()
            await asyncio.gather(cancellation_task, return_exceptions=True)
            return provider_task.result()
        finally:
            for task in (provider_task, cancellation_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                provider_task,
                cancellation_task,
                return_exceptions=True,
            )

    async def _finish_collection(
        self,
        scope: EvidenceSessionScope,
        operation_id: str,
        collections: tuple[EvidenceCollectionInput, ...],
    ) -> EvidenceOperationValue:
        scope.cancel_token.checkpoint()
        if not collections:
            raise EvidenceScopeViolationError()
        batches = tuple(item.batch for item in collections)
        self._validate_collected_batches(batches)
        requests = sum(batch.counts.requests for batch in batches)
        pages = sum(batch.counts.pages for batch in batches)
        records = sum(batch.counts.records for batch in batches)
        self._check_deadline(scope)
        scope.cancel_token.checkpoint()
        artifacts: EvidenceCollectionArtifacts = await self._collection_service.process(
            scope,
            operation_id=operation_id,
            collections=collections,
        )
        complete = all(batch.completion_proof.complete for batch in batches)
        status = (
            CollectionStatus.UNAVAILABLE
            if not complete
            else CollectionStatus.AVAILABLE
            if records
            else CollectionStatus.EMPTY
        )
        return EvidenceOperationValue(
            collection_status=status,
            manifest_refs=artifacts.manifest_refs,
            fragment_refs=artifacts.fragment_refs,
            collection_fact_refs=artifacts.collection_fact_refs,
            source_record_refs=artifacts.source_record_refs,
            requests=requests,
            pages=pages,
            records=records,
            cacheable=complete,
            safe_error_code=None if complete else "evidence_collection_incomplete",
        )

    def _ensure_another_collection_request(
        self,
        batches: tuple[ProviderCollectionBatch, ...],
    ) -> None:
        if (
            sum(batch.counts.pages for batch in batches)
            >= self._limits.pages_per_operation
            or sum(batch.counts.records for batch in batches)
            >= self._limits.records_per_operation
        ):
            raise EvidenceToolLimitExceededError()

    def _validate_collected_batches(
        self,
        batches: tuple[ProviderCollectionBatch, ...],
    ) -> None:
        if (
            sum(batch.counts.pages for batch in batches)
            > self._limits.pages_per_operation
            or sum(batch.counts.records for batch in batches)
            > self._limits.records_per_operation
            or any(
                page.response_bytes > self._limits.response_bytes_per_request
                for batch in batches
                for page in batch.page_or_chunk_manifests
            )
        ):
            raise EvidenceToolLimitExceededError()

    async def _convert(self, scope, request, operation_id) -> EvidenceOperationValue:
        self._check_deadline(scope)
        reference = request.options.fragment_refs[0]
        fragment = await self._reader.read_fragment(scope, reference)
        scope.cancel_token.checkpoint()
        if (
            fragment.target_comid != scope.agent_comid
            or request.parameter_keys[0] not in fragment.parameter_refs
        ):
            raise EvidenceScopeViolationError()
        value, unit = _fragment_numeric_value(fragment)
        capability = self._capabilities.get(request.parameter_keys[0])
        if capability is None:
            raise EvidenceScopeViolationError()
        converted = convert_value(
            self._capabilities,
            capability,
            value,
            unit,
            target_unit_id=request.options.target_unit_id,
        )
        derived = EvidenceFragment(
            evidence_id=f"{operation_id}:conversion",
            source="deterministic_conversion",
            collection_status=CollectionStatus.AVAILABLE,
            target_comid=fragment.target_comid,
            location=fragment.location,
            time_range=fragment.time_range,
            parameter_refs=fragment.parameter_refs,
            data_type="conversion",
            applicability=fragment.applicability,
            relation_id=fragment.relation_id,
            series_identity=fragment.series_identity,
            quality_class=fragment.quality_class,
            quality_flags=fragment.quality_flags,
            coverage=fragment.coverage,
            payload=ConversionPayload(
                value=converted.value,
                unit=converted.target_unit,
                input_evidence_ref=reference,
                conversion_id=converted.formula_id,
            ),
            source_record_refs=(),
            derivation=EvidenceDerivation(
                algorithm="decimal_unit_conversion",
                algorithm_version=1,
                policy_id=converted.formula_id,
                registry_digest=converted.registry_digest,
            ),
            provenance=fragment.provenance.model_copy(
                update={
                    "operation_id": operation_id,
                    "registry_digest": converted.registry_digest,
                }
            ),
            result_eligibility=fragment.result_eligibility,
            rag_eligible=fragment.rag_eligible,
        )
        self._check_deadline(scope)
        derived_ref = await self._writer.write_fragment(
            scope,
            operation_id=operation_id,
            fragment=derived,
        )
        return EvidenceOperationValue(fragment_refs=(derived_ref,))

    async def _aggregate(self, scope, request, operation_id) -> EvidenceOperationValue:
        self._check_deadline(scope)
        stored = await self._reader.read_policy_observations(
            scope,
            request.options.source_record_refs,
        )
        scope.cancel_token.checkpoint()
        if (
            {item.record_ref for item in stored.observations}
            != set(request.options.source_record_refs)
            or not stored.observations
        ):
            raise EvidenceScopeViolationError()
        parameter = request.parameter_keys[0]
        if any(
            item.parameter != parameter
            or item.location_comid not in {None, scope.agent_comid}
            for item in stored.observations
        ):
            raise EvidenceScopeViolationError()
        capability = self._capabilities.get(parameter)
        if capability is None:
            raise EvidenceScopeViolationError()
        window = next(
            item.time_range
            for item in scope.time_windows
            if item.window_id == request.window_ids[0]
        )
        outcome = aggregate_observations(
            self._capabilities,
            capability,
            stored.observations,
            window,
            request.options.aggregation_method,
            collection_complete=stored.collection_complete,
            expected_cadence_seconds=stored.expected_cadence_seconds,
        )
        first = stored.observations[0]
        derived = EvidenceFragment(
            evidence_id=f"{operation_id}:aggregate",
            source="deterministic_aggregation",
            collection_status=CollectionStatus.AVAILABLE,
            target_comid=scope.agent_comid,
            location=EvidenceLocation(
                point=None,
                comid=scope.agent_comid,
                station_id=first.station_id,
                distance_to_target_km=None,
            ),
            time_range=window,
            parameter_refs=(parameter,),
            data_type="aggregate",
            applicability=EvidenceApplicability.DIRECT_MEASUREMENT,
            series_identity=first.series_identity,
            quality_class=outcome.quality_class,
            quality_flags=outcome.quality_flags,
            coverage=outcome.coverage,
            payload=outcome.payload,
            source_record_refs=outcome.payload.input_record_refs,
            derivation=outcome.derivation,
            provenance=Provenance(
                operation_id=operation_id,
                registry_digest=self._capabilities.manifest_digest,
            ),
            result_eligibility=EvidenceResultEligibility.INFERRED_ONLY,
            rag_eligible=outcome.quality_class is not EvidenceQualityClass.INVALID,
        )
        self._check_deadline(scope)
        derived_ref = await self._writer.write_fragment(
            scope,
            operation_id=operation_id,
            fragment=derived,
        )
        return EvidenceOperationValue(fragment_refs=(derived_ref,))

    def _result_from_value(self, request, *, operation_id, value):
        failed = value.safe_error_code is not None
        safe_error = None
        if failed:
            safe_error = SafeEvidenceError(
                code=value.safe_error_code,
                message="The evidence collection could not be proven complete.",
                retryable=True,
            )
        return EvidenceToolResult(
            tool_call_id=request.tool_call_id,
            operation_id=operation_id,
            tool_name=request.tool_name,
            execution_status=(
                EvidenceToolExecutionStatus.FAILED
                if failed
                else EvidenceToolExecutionStatus.COMPLETED
            ),
            collection_status=value.collection_status,
            manifest_refs=value.manifest_refs,
            fragment_refs=value.fragment_refs,
            collection_fact_refs=value.collection_fact_refs,
            source_record_refs=value.source_record_refs,
            counts=EvidenceToolCounts(
                requests=value.requests,
                pages=value.pages,
                records=value.records,
                fragments=len(value.fragment_refs) + len(value.collection_fact_refs),
            ),
            safe_error=safe_error,
            completed_at=self._clock.now(),
        )

    async def _write_reused_result(
        self,
        scope,
        request,
        *,
        operation_id,
        completed,
    ) -> EvidenceToolResult:
        successful = (
            completed.result.execution_status is EvidenceToolExecutionStatus.COMPLETED
        )
        update = {
            "tool_call_id": request.tool_call_id,
            "operation_id": operation_id,
            "completed_at": self._clock.now(),
            "deduplicated": successful,
            "deduplicated_from_operation_id": (
                completed.result.operation_id if successful else None
            ),
            "deduplicated_from_result_ref": (
                completed.result_ref if successful else None
            ),
        }
        payload = completed.result.model_dump(mode="python")
        payload.update(update)
        result = EvidenceToolResult.model_validate(payload)
        await self._writer.write_result(scope, result)
        return result

    async def _write_failure_result(
        self,
        scope,
        request,
        *,
        operation_id,
        error,
        cancelled=False,
    ) -> EvidenceToolResult:
        result = EvidenceToolResult(
            tool_call_id=request.tool_call_id,
            operation_id=operation_id,
            tool_name=request.tool_name,
            execution_status=(
                EvidenceToolExecutionStatus.CANCELLED
                if cancelled
                else EvidenceToolExecutionStatus.FAILED
            ),
            collection_status=None,
            counts=EvidenceToolCounts(),
            safe_error=error,
            completed_at=self._clock.now(),
        )
        await self._writer.write_result(scope, result)
        return result


def _fragment_numeric_value(fragment: EvidenceFragment) -> tuple[str, str]:
    payload = fragment.payload
    if isinstance(payload, DirectRecordPayload):
        return payload.displayed_value, payload.displayed_unit
    if isinstance(payload, (AggregatePayload, ConversionPayload)):
        return payload.value, payload.unit
    raise EvidenceUnitUnresolvedError()


def _map_policy_error(error: EvidencePolicyError) -> SafeEvidenceError:
    if error.code == "evidence_unit_unresolved":
        mapped: EvidenceToolError = EvidenceUnitUnresolvedError()
    elif error.code == "evidence_collection_incomplete":
        mapped = EvidenceCollectionIncompleteError()
    else:
        mapped = EvidenceAggregationNotAllowedError()
    return map_evidence_error(mapped)

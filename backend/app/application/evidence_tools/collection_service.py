import json
from collections import defaultdict
from hashlib import sha256

from app.application.evidence_policy import (
    EvidencePolicyError,
    IndirectEvidenceContext,
    PolicyObservation,
    apply_rag_policy,
    assess_provider_quality,
    calculate_coverage,
    convert_value,
    resolve_applicability,
)
from app.application.evidence_tools.collection_manifest import (
    build_collection_manifest,
)
from app.application.evidence_tools.errors import EvidenceScopeViolationError
from app.application.evidence_tools.models import (
    EvidenceCollectionArtifacts,
    EvidenceCollectionInput,
    EvidenceRecordPreview,
    EvidenceToolLimits,
    EvidenceToolWriter,
)
from app.application.evidence_tools.scope import EvidenceSessionScope, EvidenceWindow
from app.domain import (
    CollectionFactPayload,
    CollectionManifest,
    CollectionStatus,
    ContextPayload,
    CoverageStatus,
    DataQualityFlag,
    DirectRecordPayload,
    DischargeObservationRecord,
    EvidenceApplicability,
    EvidenceCoverage,
    EvidenceFragment,
    EvidenceQualityClass,
    EvidenceResultEligibility,
    EvidenceRuleId,
    FacilityRecord,
    HistoricalInferencePayload,
    HistoricalTranscriptRecord,
    ParameterCapabilityRegistry,
    ParameterKey,
    ParameterProvider,
    Provenance,
    SourceRecord,
    SourceRecordReference,
    UpstreamResultPayload,
    UpstreamResultRecord,
    WaterObservationRecord,
    WeatherForecastRecord,
    WeatherObservationRecord,
    canonical_model_bytes,
)

_OBSERVATION_TYPES = (WaterObservationRecord, DischargeObservationRecord)
_CONTEXT_TYPES = (
    WeatherObservationRecord,
    WeatherForecastRecord,
    FacilityRecord,
    UpstreamResultRecord,
    HistoricalTranscriptRecord,
)


def build_policy_observation(
    registry: ParameterCapabilityRegistry,
    record: SourceRecord,
    reference: SourceRecordReference | None,
    *,
    provider_name: str | None = None,
    channel: str | None = None,
) -> PolicyObservation | None:
    if reference is None or not isinstance(record, _OBSERVATION_TYPES):
        return None
    try:
        provider = ParameterProvider(provider_name or record.provider)
    except ValueError:
        return None
    capability = registry.get(record.parameter)
    policy = registry.get_channel_policy(
        record.parameter,
        provider,
        channel or record.channel,
    )
    if capability is None or policy is None:
        return None
    quality = assess_provider_quality(
        registry,
        capability,
        provider,
        channel or record.channel,
        record.provider_quality,
    )
    return PolicyObservation(
        record_ref=reference,
        parameter=record.parameter,
        provider=provider,
        channel=channel or record.channel,
        location_comid=record.location.comid,
        station_id=record.location.station_id or "unknown-station",
        medium=record.medium,
        fraction=record.fraction,
        method=record.method or "missing-method",
        statistic=record.statistic,
        value_semantics=policy.value_semantics,
        observed_time=record.observed_time,
        valid_time_range=record.valid_time_range,
        value=record.value,
        unit=record.raw_unit or "missing-unit",
        quality=quality,
        series_identity=_series_identity(record, policy.value_semantics.value),
    )


class EvidenceCollectionService:
    """Turn normalized provider records into persisted, policy-labelled fragments."""

    def __init__(
        self,
        *,
        registry: ParameterCapabilityRegistry,
        writer: EvidenceToolWriter,
        limits: EvidenceToolLimits = EvidenceToolLimits(),
    ) -> None:
        self._registry = registry
        self._writer = writer
        self._limits = limits

    async def process(
        self,
        scope: EvidenceSessionScope,
        *,
        operation_id: str,
        collections: tuple[EvidenceCollectionInput, ...],
    ) -> EvidenceCollectionArtifacts:
        if not collections:
            raise EvidenceScopeViolationError()
        ordered = tuple(sorted(collections, key=_collection_sort_key))
        manifests = tuple(self._manifest(scope, item) for item in ordered)
        records, discoveries = _deduplicate_records(ordered)
        stored = await self._writer.write_collection(
            scope,
            operation_id=operation_id,
            manifests=manifests,
            records=records,
        )
        if (
            len(stored.manifest_refs) != len(manifests)
            or {ref.record_id for ref in stored.source_record_refs}
            != {record.record_id for record in records}
            or stored.fragment_refs
            or stored.collection_fact_refs
        ):
            raise EvidenceScopeViolationError()
        refs_by_record = {
            reference.record_id: reference
            for reference in stored.source_record_refs
        }

        fragments: dict[str, EvidenceFragment] = {}
        facts: dict[str, EvidenceFragment] = {}
        for collection, manifest in zip(ordered, manifests, strict=True):
            built, collection_facts = self._build_collection_fragments(
                scope,
                collection,
                manifest,
                refs_by_record,
                discoveries,
            )
            for fragment in built:
                fragments.setdefault(fragment.evidence_id, fragment)
            for fact in collection_facts:
                facts.setdefault(fact.evidence_id, fact)

        fragment_refs = []
        fact_refs = []
        for fragment in sorted(fragments.values(), key=lambda item: item.evidence_id):
            fragment_refs.append(
                await self._writer.write_fragment(
                    scope,
                    operation_id=operation_id,
                    fragment=fragment,
                )
            )
        for fact in sorted(facts.values(), key=lambda item: item.evidence_id):
            fact_refs.append(
                await self._writer.write_fragment(
                    scope,
                    operation_id=operation_id,
                    fragment=fact,
                )
            )
        return EvidenceCollectionArtifacts(
            manifest_refs=stored.manifest_refs,
            source_record_refs=stored.source_record_refs,
            fragment_refs=tuple(fragment_refs),
            collection_fact_refs=tuple(fact_refs),
            record_preview=build_record_preview(
                records,
                max_records=self._limits.preview_max_records,
                max_bytes=self._limits.preview_max_bytes,
            ),
        )

    def _manifest(
        self,
        scope: EvidenceSessionScope,
        collection: EvidenceCollectionInput,
    ) -> CollectionManifest:
        window_by_id = {item.window_id: item for item in scope.time_windows}
        if not set(collection.affected_window_ids) <= set(window_by_id):
            raise EvidenceScopeViolationError()
        windows = tuple(
            item
            for item in scope.time_windows
            if item.window_id in set(collection.affected_window_ids)
        )
        identity = {
            "provider": collection.provider,
            "channel": collection.channel,
            "request_range": collection.request_range.model_dump(mode="json"),
            "requested_series": sorted(collection.requested_series),
            "affected_window_ids": sorted(collection.affected_window_ids),
            "batch": collection.batch.model_dump(mode="json", exclude={"records"}),
        }
        digest = sha256(_canonical_json(identity)).hexdigest()
        return build_collection_manifest(
            manifest_id=f"manifest:sha256:{digest}",
            provider=collection.provider,
            channel=collection.channel,
            request_range=collection.request_range,
            requested_series=tuple(sorted(collection.requested_series)),
            windows=windows,
            batch=collection.batch,
            completion_applies_to_all_windows=(
                collection.completion_applies_to_all_windows
            ),
        )

    def _build_collection_fragments(
        self,
        scope: EvidenceSessionScope,
        collection: EvidenceCollectionInput,
        manifest: CollectionManifest,
        refs_by_record: dict[str, SourceRecordReference],
        discoveries: dict[str, tuple[str, ...]],
    ) -> tuple[tuple[EvidenceFragment, ...], tuple[EvidenceFragment, ...]]:
        completeness = {
            item.window_id: item for item in manifest.window_completeness
        }
        windows = tuple(
            item
            for item in scope.time_windows
            if item.window_id in set(collection.affected_window_ids)
        )
        observation_details = self._observation_details(
            collection,
            refs_by_record,
        )
        observations_by_series: dict[str, list[PolicyObservation]] = defaultdict(list)
        for _, observation, _ in observation_details.values():
            observations_by_series[observation.series_identity].append(observation)

        fragments: list[EvidenceFragment] = []
        unit_blockers: dict[tuple[ParameterKey, str], list[SourceRecordReference]] = (
            defaultdict(list)
        )
        unique_records = {record.record_id: record for record in collection.batch.records}
        for record in sorted(unique_records.values(), key=_record_sort_key):
            reference = refs_by_record[record.record_id]
            for window in windows:
                if not _record_applies_to_window(record, window):
                    continue
                complete = completeness[window.window_id].complete
                if isinstance(record, _OBSERVATION_TYPES):
                    detail = observation_details.get(record.record_id)
                    if detail is None:
                        continue
                    quality, observation, conversion = detail
                    if conversion is None:
                        unit_blockers[(record.parameter, window.window_id)].append(
                            reference
                        )
                    fragments.append(
                        self._observation_fragment(
                            scope,
                            collection,
                            record,
                            reference,
                            window,
                            complete=complete,
                            quality=quality,
                            observation=observation,
                            conversion=conversion,
                            series_observations=tuple(
                                observations_by_series[observation.series_identity]
                            ),
                            discovery_identities=discoveries[record.record_id],
                        )
                    )
                elif isinstance(record, _CONTEXT_TYPES):
                    fragments.append(
                        self._context_fragment(
                            scope,
                            record,
                            reference,
                            window,
                            complete=complete,
                            parameter_keys=(
                                (record.parameter,)
                                if isinstance(record, UpstreamResultRecord)
                                else collection.parameter_keys
                            ),
                            discovery_identities=discoveries[record.record_id],
                        )
                    )

        facts = list(
            self._collection_facts(
                scope,
                collection,
                manifest,
                refs_by_record,
            )
        )
        for (parameter, window_id), references in sorted(
            unit_blockers.items(),
            key=lambda item: (
                item[0][0].code_system,
                item[0][0].code,
                item[0][1],
            ),
        ):
            window = next(item for item in windows if item.window_id == window_id)
            facts.append(
                self._fact_fragment(
                    scope,
                    manifest,
                    parameter,
                    window,
                    status=CollectionStatus.INVALID,
                    reason="unit_unresolved",
                    record_refs=tuple(sorted(set(references), key=_source_ref_sort_key)),
                )
            )
        return tuple(fragments), tuple(facts)

    def _observation_details(self, collection, refs_by_record):
        details = {}
        for record in collection.batch.records:
            observation = build_policy_observation(
                self._registry,
                record,
                refs_by_record.get(record.record_id),
                provider_name=collection.provider,
                channel=collection.channel,
            )
            if observation is None:
                continue
            capability = self._registry.get(observation.parameter)
            assert capability is not None
            try:
                conversion = convert_value(
                    self._registry,
                    capability,
                    record.value,
                    record.raw_unit or "",
                )
            except EvidencePolicyError as error:
                if error.code != "evidence_unit_unresolved":
                    raise
                conversion = None
            details[record.record_id] = (observation.quality, observation, conversion)
        return details

    def _observation_fragment(
        self,
        scope,
        collection,
        record,
        reference,
        window,
        *,
        complete,
        quality,
        observation,
        conversion,
        series_observations,
        discovery_identities,
    ) -> EvidenceFragment:
        capability = self._registry.get(record.parameter)
        if capability is None:
            raise EvidenceScopeViolationError()
        station_confirmed = any(
            item.station_id == record.location.station_id
            and item.provider.value == collection.provider
            for item in scope.station_refs
        )
        applicability = resolve_applicability(
            self._registry,
            capability,
            scope.agent_comid,
            window.time_range,
            observation=observation,
            station_segment_confirmed=station_confirmed,
            collection_complete=complete,
            reference_complete=True,
        )
        usable_direct = (
            conversion is not None
            and quality.quality_class is not EvidenceQualityClass.INVALID
            and applicability is EvidenceApplicability.DIRECT_MEASUREMENT
        )
        try:
            coverage = calculate_coverage(
                self._registry,
                capability,
                observation.provider,
                observation.channel,
                series_observations,
                window.time_range,
                collection_complete=complete,
            )
        except EvidencePolicyError:
            coverage = _basic_coverage(window, count=1, complete=complete)
        flags = set(quality.flags)
        if conversion is None:
            flags.add(DataQualityFlag.UNIT_UNRESOLVED)
        if usable_direct:
            payload = DirectRecordPayload(
                record_ref=reference,
                displayed_value=conversion.value,
                displayed_unit=conversion.target_unit,
            )
            final_applicability = EvidenceApplicability.DIRECT_MEASUREMENT
            final_quality = quality.quality_class
            eligibility = EvidenceResultEligibility.OBSERVED_OR_INFERRED
        else:
            payload = ContextPayload(
                summary_fields=(
                    ("record_type", record.record_type),
                    (
                        "reason",
                        "unit_unresolved"
                        if conversion is None
                        else "not_usable_as_direct_measurement",
                    ),
                ),
                record_refs=(reference,),
            )
            final_applicability = EvidenceApplicability.CONTEXTUAL_ONLY
            final_quality = (
                EvidenceQualityClass.INVALID
                if conversion is None
                else quality.quality_class
            )
            eligibility = (
                EvidenceResultEligibility.EXPLANATORY_ONLY
                if final_quality is EvidenceQualityClass.INVALID
                else EvidenceResultEligibility.INFERRED_ONLY
            )
        fragment = EvidenceFragment(
            evidence_id=_evidence_id(
                "record",
                reference.item_id,
                reference.record_id,
                window.window_id,
                record.parameter.code_system,
                record.parameter.code,
            ),
            source=record.provider,
            collection_status=CollectionStatus.AVAILABLE,
            target_comid=scope.agent_comid,
            location=record.location,
            time_range=window.time_range,
            parameter_refs=(record.parameter,),
            data_type=record.record_type,
            applicability=final_applicability,
            series_identity=observation.series_identity,
            quality_class=final_quality,
            quality_flags=tuple(sorted(flags, key=lambda item: item.value)),
            coverage=coverage,
            payload=payload,
            source_record_refs=(reference,),
            provenance=_fragment_provenance(
                record.provenance,
                operation_id=collection.batch.operation_id,
                registry_digest=self._registry.manifest_digest,
                discovery_identities=discovery_identities,
            ),
            result_eligibility=eligibility,
        )
        return apply_rag_policy(
            fragment,
            operation_complete=complete,
            reference_complete=True,
            redacted=True,
        )

    def _context_fragment(
        self,
        scope,
        record,
        reference,
        window,
        *,
        complete,
        parameter_keys,
        discovery_identities,
    ) -> EvidenceFragment:
        if not parameter_keys:
            raise EvidenceScopeViolationError()
        quality = (
            record.original_quality
            if isinstance(record, HistoricalTranscriptRecord)
            and record.original_quality is not None
            else EvidenceQualityClass.SUSPICIOUS
        )
        flags = (
            (DataQualityFlag.HISTORICAL_INFERENCE,)
            if isinstance(record, HistoricalTranscriptRecord)
            else (DataQualityFlag.PROVIDER_FLAGGED,)
        )
        applicability = EvidenceApplicability.CONTEXTUAL_ONLY
        relation_id = None
        if isinstance(record, UpstreamResultRecord):
            capability = self._registry.get(record.parameter)
            if capability is None:
                raise EvidenceScopeViolationError()
            context = IndirectEvidenceContext(
                relation_id=EvidenceRuleId.SAME_PARAMETER_UPSTREAM,
                input_parameters=(record.parameter,),
                input_windows=(record.valid_time_range,),
                reference_complete=True,
                upstream_result_complete=True,
            )
            applicability = resolve_applicability(
                self._registry,
                capability,
                scope.agent_comid,
                window.time_range,
                indirect_context=context,
            )
            if applicability is EvidenceApplicability.ALLOWED_INDIRECT:
                relation_id = context.relation_id
                payload = UpstreamResultPayload(
                    result_ref=record.result_ref,
                    closure_refs=record.evidence_closure_refs,
                )
                source_refs = ()
            else:
                payload = ContextPayload(
                    summary_fields=(("result_kind", record.result_kind),),
                    record_refs=(reference,),
                )
                source_refs = (reference,)
        elif isinstance(record, HistoricalTranscriptRecord):
            payload = HistoricalInferencePayload(
                original_item_ref=record.original_item_ref,
                projection=f"Historical {record.content_type} match via {record.matched_by}",
            )
            source_refs = ()
        else:
            payload = ContextPayload(
                summary_fields=_context_summary(record),
                record_refs=(reference,),
            )
            source_refs = (reference,)
        fragment = EvidenceFragment(
            evidence_id=_evidence_id(
                "context",
                reference.item_id,
                reference.record_id,
                window.window_id,
                *(f"{item.code_system}:{item.code}" for item in parameter_keys),
            ),
            source=record.provider,
            collection_status=CollectionStatus.AVAILABLE,
            target_comid=scope.agent_comid,
            location=record.location,
            time_range=window.time_range,
            parameter_refs=tuple(parameter_keys),
            data_type=record.record_type,
            applicability=applicability,
            relation_id=relation_id,
            series_identity=None,
            quality_class=quality,
            quality_flags=flags,
            coverage=_basic_coverage(window, count=1, complete=complete),
            payload=payload,
            source_record_refs=source_refs,
            provenance=_fragment_provenance(
                record.provenance,
                operation_id=record.provenance.operation_id,
                registry_digest=self._registry.manifest_digest,
                discovery_identities=discovery_identities,
            ),
            result_eligibility=(
                EvidenceResultEligibility.EXPLANATORY_ONLY
                if quality is EvidenceQualityClass.INVALID
                else EvidenceResultEligibility.INFERRED_ONLY
            ),
        )
        return apply_rag_policy(
            fragment,
            operation_complete=complete,
            reference_complete=True,
            redacted=True,
        )

    def _collection_facts(
        self,
        scope,
        collection,
        manifest,
        refs_by_record,
    ):
        parameters = collection.parameter_keys or (
            *scope.requested_parameters,
            *scope.supporting_parameters,
        )
        windows = {item.window_id: item for item in scope.time_windows}
        for completeness in manifest.window_completeness:
            window = windows[completeness.window_id]
            for parameter in parameters:
                matching = tuple(
                    refs_by_record[record.record_id]
                    for record in collection.batch.records
                    if _record_applies_to_window(record, window)
                    and (
                        not isinstance(record, _OBSERVATION_TYPES)
                        or record.parameter == parameter
                    )
                )
                reason = (
                    completeness.reason
                    if not completeness.complete
                    else "no_records_in_completed_query"
                    if not matching
                    else "records_collected"
                )
                yield self._fact_fragment(
                    scope,
                    manifest,
                    parameter,
                    window,
                    status=completeness.status,
                    reason=reason,
                    record_refs=tuple(
                        sorted(set(matching), key=_source_ref_sort_key)
                    ),
                )

    def _fact_fragment(
        self,
        scope,
        manifest,
        parameter,
        window,
        *,
        status,
        reason,
        record_refs,
    ):
        series_identity = _evidence_id(
            "manifest-series",
            manifest.manifest_id,
            parameter.code_system,
            parameter.code,
        )
        return EvidenceFragment(
            evidence_id=_evidence_id(
                "collection-fact",
                manifest.manifest_id,
                parameter.code_system,
                parameter.code,
                window.window_id,
                reason,
            ),
            source=manifest.provider,
            collection_status=status,
            target_comid=scope.agent_comid,
            location=_target_location(scope),
            time_range=window.time_range,
            parameter_refs=(parameter,),
            data_type="collection_fact",
            applicability=EvidenceApplicability.CONTEXTUAL_ONLY,
            series_identity=series_identity,
            quality_class=None,
            quality_flags=(),
            coverage=_basic_coverage(window, count=0, complete=status is not CollectionStatus.UNAVAILABLE),
            payload=CollectionFactPayload(
                status=status,
                reason=reason,
                affected_window_ids=(window.window_id,),
                record_refs=record_refs,
            ),
            source_record_refs=record_refs,
            provenance=Provenance(
                operation_id=manifest.operation_id,
                registry_digest=self._registry.manifest_digest,
            ),
            result_eligibility=EvidenceResultEligibility.EXPLANATORY_ONLY,
        )


def build_record_preview(
    records: tuple[SourceRecord, ...],
    *,
    max_records: int,
    max_bytes: int,
) -> EvidenceRecordPreview:
    selected = []
    used = 2
    for record in records:
        if len(selected) >= max_records:
            break
        encoded = canonical_model_bytes(record)
        added = len(encoded) + (1 if selected else 0)
        if used + added > max_bytes:
            break
        selected.append(record)
        used += added
    return EvidenceRecordPreview(
        records=tuple(selected),
        total_records=len(records),
        serialized_bytes=used,
        truncated=len(selected) < len(records),
    )


def _collection_sort_key(item: EvidenceCollectionInput):
    return (
        item.provider,
        item.channel,
        item.batch.operation_id,
        tuple(sorted(item.requested_series)),
    )


def _deduplicate_records(collections):
    by_id = {}
    discoveries: dict[str, set[str]] = defaultdict(set)
    for collection in collections:
        for record in collection.batch.records:
            previous = by_id.get(record.record_id)
            if previous is not None and _record_core(previous) != _record_core(record):
                raise EvidenceScopeViolationError()
            if previous is None or _record_sort_key(record) < _record_sort_key(previous):
                by_id[record.record_id] = record
            discoveries[record.record_id].update(_discovery_identities(record))
    records = tuple(sorted(by_id.values(), key=_record_sort_key))
    return records, {
        record_id: tuple(sorted(values))
        for record_id, values in discoveries.items()
    }


def _record_core(record):
    return record.model_dump(
        mode="json",
        exclude={"retrieved_at", "provenance"},
    )


def _discovery_identities(record):
    provenance = record.provenance
    values = {
        f"{name}:{value}"
        for name, value in (
            ("request", provenance.request_identity),
            ("page", provenance.page_identity),
            ("chunk", provenance.chunk_identity),
            ("series", provenance.provider_series_id),
            ("record", provenance.provider_record_id),
        )
        if value is not None
    }
    return values or {f"record:{record.record_id}"}


def _record_sort_key(record):
    return record.record_id, canonical_model_bytes(record)


def _source_ref_sort_key(reference):
    return reference.item_id, reference.record_id


def _canonical_json(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _series_identity(record, value_semantics):
    identity = {
        "provider": record.provider,
        "channel": record.channel,
        "station_id": record.location.station_id,
        "parameter": record.parameter.model_dump(mode="json"),
        "medium": record.medium,
        "fraction": record.fraction,
        "method": record.method,
        "statistic": record.statistic,
        "value_semantics": value_semantics,
        "provider_series_id": record.provenance.provider_series_id,
        "qualifiers": sorted(record.provider_quality.qualifiers),
    }
    return f"sha256:{sha256(_canonical_json(identity)).hexdigest()}"


def _evidence_id(kind: str, *parts: str) -> str:
    payload = _canonical_json({"kind": kind, "parts": parts})
    return f"evidence:sha256:{sha256(payload).hexdigest()}"


def _record_applies_to_window(record: SourceRecord, window: EvidenceWindow) -> bool:
    if isinstance(record, FacilityRecord):
        return True
    if isinstance(record, UpstreamResultRecord):
        return record.window_id == window.window_id
    if record.observed_time is not None:
        return window.time_range.contains(record.observed_time)
    return bool(
        record.valid_time_range is not None
        and window.time_range.overlaps(record.valid_time_range)
    )


def _window_seconds(window: EvidenceWindow) -> int:
    delta = window.time_range.end - window.time_range.start
    return delta.days * 86400 + delta.seconds


def _basic_coverage(window, *, count, complete):
    return EvidenceCoverage(
        status=(
            CoverageStatus.PARTIAL
            if count
            else CoverageStatus.INSUFFICIENT
            if complete
            else CoverageStatus.UNKNOWN
        ),
        observed_count=count,
        expected_count=None,
        ratio=None,
        covered_duration_seconds=0,
        window_duration_seconds=_window_seconds(window),
        largest_gap_seconds=None,
    )


def _fragment_provenance(
    provenance,
    *,
    operation_id,
    registry_digest,
    discovery_identities,
):
    values = provenance.model_dump(mode="python")
    values.update(
        operation_id=operation_id,
        registry_digest=registry_digest,
        discovery_identities=discovery_identities,
    )
    return Provenance.model_validate(values)


def _context_summary(record):
    if isinstance(record, (WeatherObservationRecord, WeatherForecastRecord)):
        return (
            ("weather_variable", record.weather_variable),
            ("value", record.value),
            ("unit", record.raw_unit or "unknown"),
            ("statistic", record.statistic),
        )
    if isinstance(record, FacilityRecord):
        return (
            ("program", record.program),
            ("status", record.status),
            ("facility_id", record.facility_id),
        )
    return (("record_type", record.record_type),)


def _target_location(scope):
    from app.domain import EvidenceLocation

    return EvidenceLocation(
        point=scope.target_node.point,
        comid=scope.agent_comid,
        station_id=None,
        distance_to_target_km=0,
    )

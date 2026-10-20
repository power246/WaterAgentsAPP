import json
from dataclasses import dataclass
from hashlib import sha256

from app.application.evidence_tools.errors import (
    EvidenceScopeViolationError,
    EvidenceStationNotAuthorizedError,
    EvidenceTimeOutOfRangeError,
    WaterbodyGeometryUnavailableError,
)
from app.application.evidence_tools.scope import EvidenceSessionScope, EvidenceWindow
from app.domain import (
    EvidenceToolRequest,
    FacilityEvidenceRequest,
    InferenceMode,
    MemoryMode,
    ParameterCapabilityRegistry,
    ParameterKey,
    TimeRange,
    TopologySeriesRef,
    TranscriptEvidenceRequest,
    UpstreamResultRequest,
    WaterObservationRequest,
    WeatherEvidenceRequest,
)


@dataclass(frozen=True, slots=True)
class AuthorizedEvidenceRequest:
    request: EvidenceToolRequest
    windows: tuple[EvidenceWindow, ...]
    parameters: tuple[ParameterKey, ...]


def authorize_evidence_tool_request(
    scope: EvidenceSessionScope,
    request: EvidenceToolRequest,
    *,
    max_agent_record_limit: int = 200,
) -> AuthorizedEvidenceRequest:
    if request.session_id != scope.session_id:
        raise EvidenceScopeViolationError()
    requested_window_ids = set(request.window_ids)
    if not requested_window_ids <= {item.window_id for item in scope.time_windows}:
        raise EvidenceScopeViolationError()
    windows = tuple(
        item for item in scope.time_windows if item.window_id in requested_window_ids
    )
    allowed_parameters = set(
        (
            *scope.requested_parameters,
            *scope.supporting_parameters,
            *scope.intermediate_parameters,
            *(
                key
                for rule in scope.evidence_rule_plan.effective_rules
                for key in (rule.output_parameter, *rule.input_parameters)
            ),
        )
    )
    if not set(request.parameter_keys) <= allowed_parameters:
        raise EvidenceScopeViolationError()
    record_limit = request.options.record_limit
    if record_limit is not None and record_limit > max_agent_record_limit:
        raise EvidenceScopeViolationError()
    return AuthorizedEvidenceRequest(
        request=request,
        windows=windows,
        parameters=tuple(
            sorted(
                request.parameter_keys,
                key=lambda item: (item.code_system, item.code),
            )
        ),
    )


def build_water_observation_plans(
    scope: EvidenceSessionScope,
    request: EvidenceToolRequest,
    *,
    operation_id: str,
    registry: ParameterCapabilityRegistry,
) -> tuple[WaterObservationRequest, ...]:
    authorized = authorize_evidence_tool_request(scope, request)
    if request.tool_name != "collect_water_observations":
        raise EvidenceScopeViolationError()
    plans: list[WaterObservationRequest] = []
    for station in scope.station_refs:
        for parameter in authorized.parameters:
            capability = registry.get(parameter)
            if capability is None:
                raise EvidenceScopeViolationError()
            policies = tuple(
                policy
                for policy in capability.channel_policies
                if policy.provider is station.provider
                and policy.direct_observation
                and station.provider in capability.observation_providers
            )
            for policy in policies:
                references = tuple(
                    reference
                    for reference in station.series_refs
                    if reference.parameter == parameter
                    and reference.channel == policy.channel
                )
                series_by_windows: dict[tuple[str, ...], list[str]] = {}
                for reference in references:
                    covered_windows = tuple(
                        window.window_id
                        for window in authorized.windows
                        if _reference_covers_window(reference, window)
                    )
                    if covered_windows:
                        series_by_windows.setdefault(covered_windows, []).append(
                            reference.series_identity
                        )
                for affected_windows, series_refs in series_by_windows.items():
                    covered_ranges = tuple(
                        window.time_range
                        for window in authorized.windows
                        if window.window_id in affected_windows
                    )
                    plans.append(
                        WaterObservationRequest(
                            operation_id=f"{operation_id}-{len(plans) + 1:03d}",
                            provider=station.provider,
                            channel=policy.channel,
                            station_id=station.station_id,
                            series_refs=tuple(series_refs),
                            parameter_keys=(parameter,),
                            request_range=TimeRange(
                                start=min(item.start for item in covered_ranges),
                                end=max(item.end for item in covered_ranges),
                            ),
                            analysis_timezone=scope.analysis_timezone,
                            affected_window_ids=affected_windows,
                        )
                    )
    for plan in plans:
        authorize_provider_request(scope, plan, registry=registry)
    return tuple(plans)


def authorize_provider_request(
    scope: EvidenceSessionScope,
    request: WaterObservationRequest
    | WeatherEvidenceRequest
    | FacilityEvidenceRequest
    | UpstreamResultRequest
    | TranscriptEvidenceRequest,
    *,
    registry: ParameterCapabilityRegistry,
    max_facility_centers: int = 32,
    max_transcript_records: int = 5,
) -> None:
    scope.cancel_token.checkpoint()
    if isinstance(request, WaterObservationRequest):
        _authorize_water_request(scope, request, registry)
    elif isinstance(request, WeatherEvidenceRequest):
        _authorize_time_range(scope, request.request_range)
        if request.location != scope.target_node.point:
            raise EvidenceScopeViolationError()
        _authorize_windows(scope, request.affected_window_ids)
    elif isinstance(request, FacilityEvidenceRequest):
        if scope.geometry_ref is None:
            raise WaterbodyGeometryUnavailableError()
        if request.geometry_ref != scope.geometry_ref:
            raise EvidenceScopeViolationError()
        if len(request.sample_centers) > max_facility_centers:
            raise EvidenceScopeViolationError()
        _authorize_windows(scope, request.affected_window_ids)
    elif isinstance(request, UpstreamResultRequest):
        if (
            request.run_id != scope.run_id
            or request.session_id != scope.session_id
            or request.agent_comid != scope.agent_comid
            or request.direct_upstream_comids != scope.direct_upstream_comids
        ):
            raise EvidenceScopeViolationError()
        _authorize_parameters(scope, request.parameter_keys)
        _authorize_windows(scope, request.affected_window_ids)
    elif isinstance(request, TranscriptEvidenceRequest):
        if (
            scope.memory_mode is MemoryMode.STATELESS
            or request.run_id != scope.run_id
            or request.session_id != scope.session_id
            or request.memory_mode is not scope.memory_mode
            or request.intent.limit > max_transcript_records
        ):
            raise EvidenceScopeViolationError()
        _authorize_parameters(scope, request.intent.parameter_keys)
        _authorize_windows(scope, request.intent.window_ids)
    else:
        raise EvidenceScopeViolationError()


def canonical_request_key(
    scope: EvidenceSessionScope,
    request: EvidenceToolRequest,
    *,
    policy_manifest_digest: str,
) -> str:
    options = request.options.model_dump(
        mode="json",
        exclude_none=True,
        exclude_defaults=True,
    )
    if options.get("include_forecast") is False:
        options.pop("include_forecast")
    if options.get("page_offset") == 0:
        options.pop("page_offset")
    for field_name, identity_fields in (
        ("source_record_refs", ("item_id", "record_id")),
        ("fragment_refs", ("item_id", "evidence_id")),
    ):
        if field_name in options:
            options[field_name] = sorted(
                options[field_name],
                key=lambda item: tuple(item[field] for field in identity_fields),
            )
    payload = {
        "run_id": scope.run_id,
        "agent_comid": scope.agent_comid,
        "tool_name": request.tool_name,
        "parameter_keys": sorted(
            f"{item.code_system}:{item.code}" for item in request.parameter_keys
        ),
        "window_ids": sorted(request.window_ids),
        "options": options,
        "policy_manifest_digest": policy_manifest_digest,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _authorize_water_request(
    scope: EvidenceSessionScope,
    request: WaterObservationRequest,
    registry: ParameterCapabilityRegistry,
) -> None:
    _authorize_time_range(scope, request.request_range)
    if request.analysis_timezone != scope.analysis_timezone:
        raise EvidenceScopeViolationError()
    _authorize_windows(scope, request.affected_window_ids)
    _authorize_parameters(scope, request.parameter_keys)
    station = next(
        (
            item
            for item in scope.station_refs
            if item.provider is request.provider
            and item.station_id == request.station_id
        ),
        None,
    )
    if station is None or len(request.parameter_keys) != 1:
        raise EvidenceStationNotAuthorizedError()
    parameter = request.parameter_keys[0]
    policy = registry.get_channel_policy(
        parameter,
        request.provider,
        request.channel,
    )
    capability = registry.get(parameter)
    if (
        policy is None
        or not policy.direct_observation
        or capability is None
        or request.provider not in capability.observation_providers
    ):
        raise EvidenceScopeViolationError()
    affected_windows = tuple(
        window
        for window in scope.time_windows
        if window.window_id in request.affected_window_ids
    )
    references = tuple(
        reference
        for reference in station.series_refs
        if reference.parameter == parameter
        and reference.channel == request.channel
    )
    selected = tuple(
        reference
        for reference in references
        if reference.series_identity in request.series_refs
    )
    if request.series_refs != tuple(
        reference.series_identity for reference in selected
    ):
        raise EvidenceStationNotAuthorizedError()
    if any(
        not all(
            _reference_covers_window(reference, window)
            for window in affected_windows
        )
        for reference in selected
    ):
        raise EvidenceStationNotAuthorizedError()


def _reference_covers_window(
    reference: TopologySeriesRef,
    window: EvidenceWindow,
) -> bool:
    if reference.covered_window_ids is None:
        return window.segment_index in reference.covered_segment_indexes
    if window.window_kind is InferenceMode.PREDICTION:
        return window.segment_index in reference.covered_segment_indexes
    return bool(reference.covered_window_ids) and (
        window.segment_index in reference.covered_segment_indexes
    )


def _authorize_time_range(scope: EvidenceSessionScope, request_range) -> None:
    if (
        request_range.start < scope.query_envelope.start
        or request_range.end > scope.query_envelope.end
    ):
        raise EvidenceTimeOutOfRangeError()


def _authorize_windows(
    scope: EvidenceSessionScope,
    window_ids: tuple[str, ...],
) -> None:
    allowed = {item.window_id for item in scope.time_windows}
    if not set(window_ids) <= allowed:
        raise EvidenceScopeViolationError()


def _authorize_parameters(
    scope: EvidenceSessionScope,
    parameters: tuple[ParameterKey, ...],
) -> None:
    allowed = {
        *scope.requested_parameters,
        *scope.supporting_parameters,
        *scope.intermediate_parameters,
        *(
            key
            for rule in scope.evidence_rule_plan.effective_rules
            for key in (rule.output_parameter, *rule.input_parameters)
        ),
    }
    if not set(parameters) <= allowed:
        raise EvidenceScopeViolationError()

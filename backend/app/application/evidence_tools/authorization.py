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
    MemoryMode,
    ParameterCapabilityRegistry,
    ParameterKey,
    TranscriptEvidenceRequest,
    UpstreamResultRequest,
    WaterObservationRequest,
    WeatherEvidenceRequest,
)


_TOOL_NAMES = frozenset(
    {
        "collect_water_observations",
        "collect_weather_evidence",
        "collect_facility_evidence",
        "get_upstream_results",
        "search_transcript_evidence",
        "convert_evidence_unit",
        "aggregate_evidence",
        "inspect_evidence_records",
    }
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
    scope.cancel_token.checkpoint()
    if request.session_id != scope.session_id or request.tool_name not in _TOOL_NAMES:
        raise EvidenceScopeViolationError()
    window_by_id = {item.window_id: item for item in scope.time_windows}
    try:
        windows = tuple(window_by_id[item] for item in request.window_ids)
    except KeyError as error:
        raise EvidenceScopeViolationError() from error
    allowed_parameters = set(
        (*scope.requested_parameters, *scope.supporting_parameters)
    )
    if not set(request.parameter_keys) <= allowed_parameters:
        raise EvidenceScopeViolationError()
    record_limit = request.options.record_limit
    if record_limit is not None and record_limit > max_agent_record_limit:
        raise EvidenceScopeViolationError()
    return AuthorizedEvidenceRequest(
        request=request,
        windows=windows,
        parameters=request.parameter_keys,
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
                plans.append(
                    WaterObservationRequest(
                        operation_id=f"{operation_id}-{len(plans) + 1:03d}",
                        provider=station.provider,
                        channel=policy.channel,
                        station_id=station.station_id,
                        series_refs=station.series_refs,
                        parameter_keys=(parameter,),
                        request_range=scope.query_envelope,
                        affected_window_ids=tuple(
                            window.window_id for window in authorized.windows
                        ),
                    )
                )
    if scope.station_refs and not plans:
        raise EvidenceStationNotAuthorizedError()
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
    max_transcript_records: int = 200,
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
            or request.max_records > max_transcript_records
        ):
            raise EvidenceScopeViolationError()
    else:
        raise EvidenceScopeViolationError()


def canonical_request_key(
    scope: EvidenceSessionScope,
    request: EvidenceToolRequest,
    *,
    policy_manifest_digest: str,
) -> str:
    payload = {
        "run_id": scope.run_id,
        "agent_comid": scope.agent_comid,
        "tool_name": request.tool_name,
        "parameter_keys": sorted(
            f"{item.code_system}:{item.code}" for item in request.parameter_keys
        ),
        "window_ids": sorted(request.window_ids),
        "options": request.options.model_dump(mode="json", exclude_none=True),
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
    if station is None or request.series_refs != station.series_refs:
        raise EvidenceStationNotAuthorizedError()
    for parameter in request.parameter_keys:
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
    allowed = set((*scope.requested_parameters, *scope.supporting_parameters))
    if not set(parameters) <= allowed:
        raise EvidenceScopeViolationError()

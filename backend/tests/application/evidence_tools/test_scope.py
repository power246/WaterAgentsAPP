import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.application.analysis_runs.workflow import CancellationToken
from app.application.evidence_tools import (
    EvidenceBudgetLedger,
    EvidenceScopeViolationError,
    EvidenceSessionScope,
    EvidenceStationNotAuthorizedError,
    EvidenceTimeOutOfRangeError,
    EvidenceToolBudgetExceededError,
    assemble_evidence_session_scope,
    authorize_evidence_tool_request,
    authorize_provider_request,
    build_water_observation_plans,
    canonical_request_key,
)
from app.domain import (
    EvidenceToolOptions,
    EvidenceToolRequest,
    FacilityEvidenceRequest,
    GeoPoint,
    ParameterKey,
    ReferenceScope,
    TimeRange,
    WaterObservationRequest,
    WeatherEvidenceRequest,
)
from app.infrastructure.config import AppSettings, load_parameter_capability_registry


def tool_request(evidence_scope, **changes) -> EvidenceToolRequest:
    values = {
        "tool_call_id": "call-1",
        "session_id": evidence_scope.session_id,
        "tool_name": "collect_water_observations",
        "purpose": "Collect direct evidence.",
        "window_ids": tuple(
            window.window_id for window in evidence_scope.time_windows
        ),
        "parameter_keys": (
            *evidence_scope.requested_parameters,
            *evidence_scope.supporting_parameters,
        ),
    }
    values.update(changes)
    return EvidenceToolRequest(**values)


def test_scope_is_sealed_and_assembled_only_from_matching_saved_facts(
    evidence_scope,
    context_factory,
    graph_factory,
) -> None:
    values = {
        name: getattr(evidence_scope, name)
        for name in evidence_scope.__dataclass_fields__
    }
    with pytest.raises(TypeError, match="assembled from saved facts"):
        EvidenceSessionScope(**values, _seal=object())

    base_args = {
        "run_id": "run-1",
        "session_id": "session-1",
        "session_fact": ReferenceScope(
            item_id="session-fact",
            run_id="run-1",
            session_id="session-1",
        ),
        "analysis_context_ref": ReferenceScope(
            item_id="context-item",
            run_id="run-1",
        ),
        "topology_ref": ReferenceScope(
            item_id="topology-item",
            run_id="run-1",
        ),
        "context": context_factory(),
        "topology": graph_factory(),
        "agent_comid": "123",
        "registry": load_parameter_capability_registry(),
        "cancel_token": CancellationToken(asyncio.Event()),
    }
    with pytest.raises(ValueError, match="session fact"):
        assemble_evidence_session_scope(
            **(
                base_args
                | {
                    "session_fact": ReferenceScope(
                        item_id="session-fact",
                        run_id="run-1",
                        session_id="other-session",
                    )
                }
            )
        )
    with pytest.raises(ValueError, match="run-global fact"):
        assemble_evidence_session_scope(
            **(
                base_args
                | {
                    "analysis_context_ref": ReferenceScope(
                        item_id="context-item",
                        run_id="other-run",
                    )
                }
            )
        )

    with pytest.raises(ValueError, match="active node"):
        assemble_evidence_session_scope(
            **(base_args | {"agent_comid": "999"})
        )


@pytest.mark.parametrize(
    ("start_date", "end_date", "decision", "expected_start", "expected_end"),
    [
        (
            date(2026, 3, 8),
            date(2026, 3, 8),
            datetime(2026, 3, 8, 16, tzinfo=timezone.utc),
            datetime(2026, 3, 7, 5, tzinfo=timezone.utc),
            datetime(2026, 3, 10, 4, tzinfo=timezone.utc),
        ),
        (
            date(2026, 11, 1),
            date(2026, 11, 1),
            datetime(2026, 11, 1, 17, tzinfo=timezone.utc),
            datetime(2026, 10, 31, 4, tzinfo=timezone.utc),
            datetime(2026, 11, 3, 5, tzinfo=timezone.utc),
        ),
    ],
)
def test_local_calendar_envelope_is_dst_aware_and_does_not_add_result_windows(
    scope_factory,
    context_factory,
    start_date,
    end_date,
    decision,
    expected_start,
    expected_end,
) -> None:
    context = context_factory(
        start_date=start_date,
        end_date=end_date,
        analysis_timezone="America/New_York",
        decision_instant=decision,
    )
    scope = scope_factory(context=context)

    assert scope.query_envelope == TimeRange(
        start=expected_start,
        end=expected_end,
    )
    assert scope.time_windows[0].time_range.start == context.time_range.start
    assert scope.time_windows[-1].time_range.end == context.time_range.end
    assert all(
        window.time_range.start >= context.time_range.start
        and window.time_range.end <= context.time_range.end
        for window in scope.time_windows
    )
    assert scope.query_envelope.end - scope.query_envelope.start in {
        timedelta(hours=71),
        timedelta(hours=73),
    }


def test_authorizer_and_plan_reject_cross_scope_station_channel_time_and_geometry(
    evidence_scope,
) -> None:
    registry = load_parameter_capability_registry()
    request = tool_request(evidence_scope)
    authorized = authorize_evidence_tool_request(evidence_scope, request)
    assert authorized.windows == evidence_scope.time_windows

    plans = build_water_observation_plans(
        evidence_scope,
        request,
        operation_id="operation-1",
        registry=registry,
    )
    assert plans
    assert {plan.request_range for plan in plans} == {evidence_scope.query_envelope}
    assert {plan.station_id for plan in plans} == {"USGS-123"}
    plan = plans[0]

    with pytest.raises(EvidenceStationNotAuthorizedError):
        authorize_provider_request(
            evidence_scope,
            plan.model_copy(update={"station_id": "USGS-other"}),
            registry=registry,
        )
    with pytest.raises(EvidenceScopeViolationError):
        authorize_provider_request(
            evidence_scope,
            plan.model_copy(update={"channel": "invented-channel"}),
            registry=registry,
        )
    with pytest.raises(EvidenceTimeOutOfRangeError):
        authorize_provider_request(
            evidence_scope,
            plan.model_copy(
                update={
                    "request_range": TimeRange(
                        start=evidence_scope.query_envelope.start
                        - timedelta(seconds=1),
                        end=evidence_scope.query_envelope.end,
                    )
                }
            ),
            registry=registry,
        )
    with pytest.raises(EvidenceScopeViolationError):
        authorize_evidence_tool_request(
            evidence_scope,
            tool_request(evidence_scope, window_ids=("other-window",)),
        )
    with pytest.raises(EvidenceScopeViolationError):
        authorize_evidence_tool_request(
            evidence_scope,
            tool_request(
                evidence_scope,
                parameter_keys=(
                    ParameterKey(code_system="usgs", code="99999"),
                ),
            ),
        )

    facility = FacilityEvidenceRequest(
        operation_id="facility-operation",
        geometry_ref=evidence_scope.geometry_ref,
        exact_radius_miles=2,
        provider_radius_miles=2,
        sample_centers=(evidence_scope.target_node.point,),
        affected_window_ids=(evidence_scope.time_windows[0].window_id,),
    )
    with pytest.raises(EvidenceScopeViolationError):
        authorize_provider_request(
            evidence_scope,
            facility.model_copy(update={"geometry_ref": "other-geometry"}),
            registry=registry,
        )
    weather = WeatherEvidenceRequest(
        operation_id="weather-operation",
        evidence_kind="observation",
        location=GeoPoint(latitude=0, longitude=0),
        variables=("temperature",),
        request_range=evidence_scope.query_envelope,
        affected_window_ids=(evidence_scope.time_windows[0].window_id,),
    )
    with pytest.raises(EvidenceScopeViolationError):
        authorize_provider_request(evidence_scope, weather, registry=registry)


def test_public_request_rejects_provider_url_station_geometry_and_server_limits(
    evidence_scope,
) -> None:
    base = tool_request(evidence_scope).model_dump(mode="python")
    for field_name in ("provider", "url", "station_id", "comid", "geometry_ref"):
        with pytest.raises(ValidationError):
            EvidenceToolRequest(**base, **{field_name: "forged"})

    schema_text = str(EvidenceToolRequest.model_json_schema()).lower()
    for server_field in (
        "provider_timeout",
        "provider_attempts",
        "max_collection_calls",
        "max_response_bytes",
        "secret",
        "authorization",
    ):
        assert server_field not in schema_text

    with pytest.raises(EvidenceScopeViolationError):
        authorize_evidence_tool_request(
            evidence_scope,
            tool_request(
                evidence_scope,
                options=EvidenceToolOptions(record_limit=101),
            ),
            max_agent_record_limit=100,
        )


def test_canonical_accounting_ignores_synonymous_purpose_but_counts_split_work(
    evidence_scope,
) -> None:
    first = tool_request(evidence_scope)
    synonym = first.model_copy(
        update={"tool_call_id": "call-2", "purpose": "Fetch the same facts."}
    )
    first_key = canonical_request_key(
        evidence_scope,
        first,
        policy_manifest_digest="manifest-digest",
    )
    synonym_key = canonical_request_key(
        evidence_scope,
        synonym,
        policy_manifest_digest="manifest-digest",
    )
    assert synonym_key == first_key

    ledger = EvidenceBudgetLedger(collection_limit=1, transform_limit=1)
    assert ledger.reserve("collection", first_key)
    assert not ledger.reserve("collection", synonym_key)
    split = first.model_copy(update={"window_ids": (first.window_ids[0],)})
    split_key = canonical_request_key(
        evidence_scope,
        split,
        policy_manifest_digest="manifest-digest",
    )
    with pytest.raises(EvidenceToolBudgetExceededError):
        ledger.reserve("collection", split_key)


def test_evidence_settings_defaults_tightening_and_deployment_caps() -> None:
    settings = AppSettings(_env_file=None)
    assert (
        settings.evidence_provider_timeout_seconds,
        settings.evidence_provider_attempts,
        settings.evidence_total_deadline_seconds,
    ) == (20, 2, 90)
    assert (
        settings.evidence_max_collection_calls,
        settings.evidence_max_transform_calls,
        settings.evidence_max_pages,
        settings.evidence_max_records,
        settings.evidence_max_response_bytes,
    ) == (12, 24, 10, 10_000, 8 * 1024 * 1024)
    assert (
        settings.evidence_inspection_page_size,
        settings.evidence_inspection_max_page_size,
        settings.evidence_preview_max_records,
        settings.evidence_preview_max_bytes,
        settings.evidence_facility_max_centers,
    ) == (50, 200, 32, 16 * 1024, 32)

    tightened = AppSettings(
        _env_file=None,
        evidence_provider_timeout_seconds=10,
        evidence_provider_attempts=1,
        evidence_total_deadline_seconds=45,
        evidence_max_collection_calls=6,
        evidence_max_transform_calls=12,
        evidence_max_pages=5,
        evidence_max_records=5000,
        evidence_max_response_bytes=4 * 1024 * 1024,
        evidence_inspection_page_size=25,
        evidence_inspection_max_page_size=100,
        evidence_preview_max_records=16,
        evidence_preview_max_bytes=8 * 1024,
        evidence_facility_max_centers=16,
    )
    assert tightened.evidence_max_collection_calls == 6
    assert tightened.evidence_inspection_max_page_size == 100

    for field_name, value in (
        ("evidence_provider_timeout_seconds", 21),
        ("evidence_provider_attempts", 3),
        ("evidence_total_deadline_seconds", 91),
        ("evidence_max_collection_calls", 13),
        ("evidence_max_transform_calls", 25),
        ("evidence_max_pages", 11),
        ("evidence_max_records", 10_001),
        ("evidence_max_response_bytes", 8 * 1024 * 1024 + 1),
        ("evidence_facility_max_centers", 33),
    ):
        with pytest.raises(ValidationError):
            AppSettings(_env_file=None, **{field_name: value})
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, evidence_unknown_limit=1)
    with pytest.raises(ValidationError, match="must not exceed"):
        AppSettings(
            _env_file=None,
            evidence_inspection_page_size=101,
            evidence_inspection_max_page_size=100,
        )

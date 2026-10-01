from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from app.application.analysis_runs.models import AnalysisConfigurationSnapshot
from app.application.query_understanding import (
    AnalysisContext,
    DateResolver,
    EvidenceInferenceStrength,
    EvidenceRuleOrigin,
    EvidenceSpatialRelation,
    EvidenceTemporalRelation,
    ParameterConcept,
    ParameterIntentSource,
    ParsedIntentV1,
    ResolvedParameterIntent,
    ResolvedTarget,
    SupportingIntentReason,
    SupportingParameterIntent,
    TargetSource,
    append_evidence_rules,
    create_evidence_rule,
    create_evidence_rule_plan,
)
from app.domain import (
    GeoPoint,
    MemoryMode,
    ModelSelection,
    ParameterKey,
    TimeRange,
    TimeResolutionPreference,
    TimeResolutionPreferenceSource,
)
from app.infrastructure.config import load_parameter_capability_registry


DECISION = datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)
PARSER_MODEL = ModelSelection(provider="openai", model="parser-model")
WATERBODY_MODEL = ModelSelection(provider="openai", model="waterbody-model")


def parser_intent() -> ParsedIntentV1:
    return ParsedIntentV1(
        start_date=date(2026, 9, 24),
        end_date=date(2026, 9, 25),
        parameters=(
            ParameterConcept(
                concept="dissolved oxygen",
                rationale="Dissolved oxygen is the requested safety indicator.",
                explicitly_named=True,
            ),
        ),
    )


def configuration(
    *,
    parser_model: ModelSelection = PARSER_MODEL,
    waterbody_model: ModelSelection = WATERBODY_MODEL,
    memory_mode: MemoryMode = MemoryMode.STATELESS,
) -> AnalysisConfigurationSnapshot:
    return AnalysisConfigurationSnapshot(
        analysis_timezone="America/New_York",
        time_resolution_preference=TimeResolutionPreference.AUTO,
        time_resolution_preference_source=TimeResolutionPreferenceSource.FRONTEND,
        parser_model=parser_model,
        waterbody_model=waterbody_model,
        memory_mode=memory_mode,
    )


def valid_context() -> AnalysisContext:
    intent = parser_intent()
    dates = DateResolver().resolve(
        None,
        intent,
        decision_instant=DECISION,
        analysis_timezone="America/New_York",
        resolution_preference=TimeResolutionPreference.AUTO,
        resolution_preference_source=TimeResolutionPreferenceSource.FRONTEND,
    )
    assert dates.temporal is not None
    temporal = dates.temporal
    return AnalysisContext(
        query="Will dissolved oxygen be unsafe?",
        decision_instant=DECISION,
        analysis_timezone="America/New_York",
        target=ResolvedTarget(
            comid="12345678",
            point=GeoPoint(latitude=38.9, longitude=-77.0),
            display_name="Potomac reach",
            country_code="US",
            subdivision_code="US-DC",
            source=TargetSource.QUERY,
        ),
        requested_date_range=temporal.requested_date_range,
        time_range=temporal.time_range,
        time_segments=temporal.time_segments,
        mode=temporal.mode,
        time_resolution=temporal.time_resolution,
        time_resolution_source=temporal.time_resolution_source,
        params_used=(
            ResolvedParameterIntent(
                key=ParameterKey(code_system="usgs", code="00300"),
                canonical_name="Dissolved oxygen",
                canonical_unit="mg/l",
                source=ParameterIntentSource.EXPLICIT,
                rationale="Dissolved oxygen is the requested safety indicator.",
            ),
        ),
        supporting_params=(
            SupportingParameterIntent(
                key=ParameterKey(code_system="usgs", code="00060"),
                canonical_name="Discharge",
                canonical_unit="ft3/s",
                reason=SupportingIntentReason.CAPABILITY_REQUIRED,
            ),
        ),
        parser_model=PARSER_MODEL,
        waterbody_model=WATERBODY_MODEL,
        memory_mode=MemoryMode.STATELESS,
    )


def rebuild(context: AnalysisContext, **changes) -> AnalysisContext:
    payload = context.model_dump()
    payload.update(changes)
    return AnalysisContext.model_validate(payload)


def test_valid_context_rechecks_capability_registry_and_run_configuration() -> None:
    context = valid_context()

    context.validate_capability_registry(load_parameter_capability_registry())
    context.validate_configuration(configuration())


def test_waterbody_agent_can_only_append_to_parser_rule_plan() -> None:
    context = valid_context()
    parser_rule = create_evidence_rule(
        output_parameter=context.params_used[0].key,
        input_parameters=(context.supporting_params[0].key,),
        temporal_relation=EvidenceTemporalRelation.SAME_WINDOW,
        spatial_relation=EvidenceSpatialRelation.SAME_WATERBODY,
        maximum_inference_strength=EvidenceInferenceStrength.DIRECTIONAL,
        required_collection_attempt=True,
        rationale="Parser baseline.",
        origin=EvidenceRuleOrigin.QUERY_PARSER,
    )
    agent_rule = create_evidence_rule(
        output_parameter=context.params_used[0].key,
        input_parameters=(context.params_used[0].key,),
        temporal_relation=EvidenceTemporalRelation.ADJACENT_LOCAL_DAY,
        spatial_relation=EvidenceSpatialRelation.SAME_WATERBODY,
        maximum_inference_strength=EvidenceInferenceStrength.DIRECTIONAL,
        required_collection_attempt=False,
        rationale="Agent-added context.",
        origin=EvidenceRuleOrigin.WATERBODY_AGENT,
        origin_session_id="session-123",
    )
    baseline = create_evidence_rule_plan((parser_rule,))
    extended = append_evidence_rules(baseline, (agent_rule,))

    assert extended.baseline_rules == baseline.baseline_rules
    assert set(extended.effective_rules) == {parser_rule, agent_rule}
    assert append_evidence_rules(extended, (agent_rule,)) == extended
    with pytest.raises(ValueError, match="waterbody Agent"):
        append_evidence_rules(baseline, (parser_rule,))


@pytest.mark.parametrize(
    ("country_code", "subdivision_code"),
    [("US", "US-AK"), ("US", "US-HI"), ("CA", "CA-ON")],
)
def test_resolved_target_rejects_non_conus_regions(
    country_code: str,
    subdivision_code: str,
) -> None:
    with pytest.raises(ValidationError):
        ResolvedTarget(
            comid="123",
            point=GeoPoint(latitude=1.0, longitude=1.0),
            country_code=country_code,
            subdivision_code=subdivision_code,
            source=TargetSource.QUERY,
        )


def test_resolved_target_rejects_non_positive_decimal_comid() -> None:
    with pytest.raises(ValidationError, match="positive decimal"):
        ResolvedTarget(
            comid="0123",
            point=GeoPoint(latitude=1.0, longitude=1.0),
            country_code="US",
            subdivision_code="US-DC",
            source=TargetSource.QUERY,
        )


def test_context_rejects_utc_range_that_does_not_match_local_inclusive_dates() -> None:
    context = valid_context()

    with pytest.raises(ValidationError, match="exactly cover"):
        rebuild(
            context,
            time_range=TimeRange(
                start=context.time_range.start,
                end=context.time_range.end.replace(hour=3),
            ),
        )


def test_context_rejects_incorrect_mode_or_segment_partition() -> None:
    context = valid_context()

    with pytest.raises(ValidationError, match="mode must be derived"):
        rebuild(context, mode="prediction")

    with pytest.raises(ValidationError, match="exactly partition"):
        rebuild(context, time_segments=(context.time_segments[0],))


def test_context_rejects_dates_outside_decision_snapshot_bounds() -> None:
    context = valid_context()
    payload = context.model_dump()
    payload["decision_instant"] = datetime(2026, 10, 10, tzinfo=timezone.utc)

    with pytest.raises(ValidationError, match="plus or minus 7 days"):
        AnalysisContext.model_validate(payload)


def test_context_rejects_auto_resolution_not_derived_from_calendar_days() -> None:
    context = valid_context()

    with pytest.raises(ValidationError, match="auto resolution"):
        rebuild(context, time_resolution="12h")


def test_context_rejects_duplicate_or_more_than_six_target_parameters() -> None:
    context = valid_context()
    duplicate = context.params_used + context.params_used

    with pytest.raises(ValidationError, match="params_used"):
        rebuild(context, params_used=duplicate)

    seven = tuple(
        ResolvedParameterIntent(
            key=ParameterKey(code_system="test", code=str(index)),
            canonical_name=f"parameter {index}",
            canonical_unit="unit",
            source=ParameterIntentSource.INFERRED,
            rationale="Needed for the requested analysis.",
        )
        for index in range(7)
    )
    with pytest.raises(ValidationError, match="between 1 and 6"):
        rebuild(context, params_used=seven)


def test_context_rejects_parameter_overlap_or_missing_discharge() -> None:
    context = valid_context()

    with pytest.raises(ValidationError, match="must not overlap"):
        rebuild(
            context,
            supporting_params=(
                SupportingParameterIntent(
                    key=context.params_used[0].key,
                    canonical_name=context.params_used[0].canonical_name,
                    canonical_unit=context.params_used[0].canonical_unit,
                    reason=SupportingIntentReason.CAPABILITY_REQUIRED,
                ),
            ),
        )

    with pytest.raises(ValidationError, match="must include usgs:00060"):
        rebuild(context, supporting_params=())


def test_context_registry_validation_rejects_unknown_or_mislabeled_parameters() -> None:
    context = valid_context()
    unknown = rebuild(
        context,
        params_used=(
            ResolvedParameterIntent(
                key=ParameterKey(code_system="usgs", code="99999"),
                canonical_name="Unknown",
                canonical_unit="unit",
                source=ParameterIntentSource.INFERRED,
                rationale="Needed for the requested analysis.",
            ),
        ),
    )
    with pytest.raises(ValueError, match="not executable"):
        unknown.validate_capability_registry(load_parameter_capability_registry())

    mislabeled = rebuild(
        context,
        params_used=(
            context.params_used[0].model_copy(update={"canonical_unit": "wrong"}),
        ),
    )
    with pytest.raises(ValueError, match="must match the capability registry"):
        mislabeled.validate_capability_registry(load_parameter_capability_registry())


@pytest.mark.parametrize(
    ("configuration_override", "message"),
    [
        (
            {"parser_model": ModelSelection(provider="other", model="parser")},
            "parser_model",
        ),
        (
            {"waterbody_model": ModelSelection(provider="other", model="waterbody")},
            "waterbody_model",
        ),
        ({"memory_mode": MemoryMode.MEMORY_AUGMENTED}, "memory_mode"),
    ],
)
def test_context_rejects_run_configuration_drift(
    configuration_override: dict[str, object],
    message: str,
) -> None:
    context = valid_context()

    with pytest.raises(ValueError, match=message):
        context.validate_configuration(configuration(**configuration_override))


def test_parser_contract_forbids_final_business_fields_and_empty_parameters() -> None:
    payload = {
        "parameters": [],
        "comid": "12345678",
    }

    with pytest.raises(ValidationError):
        ParsedIntentV1.model_validate(payload)


def test_default_configuration_cannot_claim_auto_or_24h() -> None:
    with pytest.raises(ValidationError, match="default time resolution preference"):
        AnalysisConfigurationSnapshot(
            analysis_timezone="America/New_York",
            time_resolution_preference=TimeResolutionPreference.AUTO,
            time_resolution_preference_source=TimeResolutionPreferenceSource.DEFAULT,
            parser_model=PARSER_MODEL,
            waterbody_model=WATERBODY_MODEL,
            memory_mode=MemoryMode.STATELESS,
        )

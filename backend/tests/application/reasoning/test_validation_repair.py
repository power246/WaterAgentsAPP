import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.application.analysis_runs.workflow import CancellationToken
from app.application.reasoning import (
    DraftValidationError,
    DraftValidator,
    ValidationExchange,
    ValidationRepairFailedError,
)
from app.application.reasoning.model_models import (
    StructuredOutputCompleted,
    Terminal,
    VisibleTextDelta,
    WaterbodyFinishReason,
    WaterbodyModelPhase,
)
from app.application.reasoning.prompt_builder import PromptBuilder
from app.application.reasoning.prompt_policy import DEFAULT_REASONING_PROMPT_POLICY, canonical_json_digest
from app.application.reasoning.working_context import BoundedEvidenceSummary, build_working_context_projection
from app.domain import (
    DataQualityFlag,
    EvidenceApplicability,
    EvidenceResultEligibility,
)
from app.domain.settings_admin import UnitPreference
from app.infrastructure.config import load_parameter_capability_registry
from app.ports import WaterbodyModelTimeoutError
from tests.application.reasoning.fakes import DeterministicWaterbodyModelPort
from tests.application.reasoning.m7_support import (
    batch_payload,
    m7_fixture,
    rebuild_catalog,
    synthesis_payload,
    validated_reasoning,
)
from tests.application.reasoning.test_draft_validator import (
    _low_basis_flow_support_fixture,
    _mixed_model_estimate_fixture,
)


def _repair_id(operation_id):
    return uuid5(NAMESPACE_URL, f"schema-repair:{operation_id}")


def _completed_script(operation_id, model, payload, *, output_id="output"):
    return (
        StructuredOutputCompleted(
            operation_id=operation_id,
            model=model,
            output_id=output_id,
            value=json.dumps(payload, sort_keys=True, separators=(",", ":")),
        ),
        Terminal(
            operation_id=operation_id,
            model=model,
            provider_response_id=f"response:{output_id}",
            finish_reason=WaterbodyFinishReason.COMPLETED,
        ),
    )


def _exchange(fake, validator):
    return ValidationExchange(
        model_port=fake,
        prompt_builder=PromptBuilder(DEFAULT_REASONING_PROMPT_POLICY),
        validator=validator,
    )


def _token():
    return CancellationToken(asyncio.Event())


def test_initial_invalid_repair_valid_is_correlated_and_scoped_once() -> None:
    _skeleton, plan, catalog, validator = m7_fixture()
    batch = plan.batch_plan.batches[0]
    invalid = batch_payload(batch, catalog)
    invalid["scope_digest"] = "sha256:wrong"
    repaired = batch_payload(batch, catalog)
    fake = DeterministicWaterbodyModelPort(
        _completed_script(batch.operation_id, batch.model, invalid, output_id="initial"),
        _completed_script(
            _repair_id(batch.operation_id),
            batch.model,
            repaired,
            output_id="repair",
        ),
    )

    outcome = asyncio.run(
        _exchange(fake, validator).run_batch(batch, cancellation=_token())
    )

    assert len(fake.calls) == 2
    initial_request, repair_request = fake.calls
    assert repair_request.phase is WaterbodyModelPhase.SCHEMA_REPAIR
    assert repair_request.model == initial_request.model == batch.model
    assert repair_request.scope_digest == initial_request.scope_digest
    assert repair_request.response_schema == initial_request.response_schema
    assert repair_request.evidence_bundle_ref == initial_request.evidence_bundle_ref
    assert repair_request.batch_id == initial_request.batch_id == batch.batch_id
    assert repair_request.tool_descriptors == ()
    assert len(repair_request.messages) == 1
    repair_body = json.loads(repair_request.messages[0].text)
    initial_body = json.loads(initial_request.messages[-1].text)
    assert repair_body["evidence_summaries"] == initial_body["evidence_summaries"]
    assert repair_body["batch_cells"] == initial_body["cells"]
    assert "only the listed issues" in repair_body["repair_instruction"]
    assert "preserve every other already valid field" in repair_body["repair_instruction"]
    assert "range_estimate" in repair_body["range_estimate_instruction"]
    assert "value_lower and value_upper" in repair_body["range_estimate_instruction"]
    assert "non-null decimal strings" in repair_body["range_estimate_instruction"]
    assert "bounded_reference_summaries" not in repair_body
    assert repair_body["repair_of_operation_id"] == str(batch.operation_id)
    assert outcome.audit.initial_issues[0].code == "scope_digest_mismatch"
    assert outcome.audit.accepted_operation_id == repair_request.operation_id
    assert outcome.audit.validated_digest == outcome.validated.validation_digest


@pytest.mark.parametrize("corrected", [True, False])
def test_series_repair_removes_uncited_refs_without_changing_evidence_or_numbers(corrected):
    _skeleton, plan, catalog, validator = m7_fixture()
    batch = plan.batch_plan.batches[0]
    valid = batch_payload(batch, catalog)
    invalid = copy.deepcopy(valid)
    invalid["cells"][0]["series_refs"].append("seal:uncited:00400")
    fake = DeterministicWaterbodyModelPort(
        _completed_script(batch.operation_id, batch.model, invalid),
        _completed_script(_repair_id(batch.operation_id), batch.model,
                          valid if corrected else invalid),
    )
    pending = _exchange(fake, validator).run_batch(batch, cancellation=_token())
    if corrected:
        outcome = asyncio.run(pending)
        assert outcome.validated.cells[0].evidence_refs == tuple(valid["cells"][0]["evidence_refs"])
        assert outcome.validated.cells[0].representative_value == valid["cells"][0]["representative_value"]
    else:
        with pytest.raises(ValidationRepairFailedError) as raised:
            asyncio.run(pending)
        assert "series_refs_incomplete" in {issue.code for issue in raised.value.audit.repair_issues}
    assert len(fake.calls) == 2
    body = json.loads(fake.calls[1].messages[0].text)
    assert "exactly the sorted union" in body["series_reference_instruction"]
    assert "cell actually cites" in body["series_reference_instruction"]
    assert "Remove unrelated entries" in body["series_reference_instruction"]
    assert "Preserve numeric values" in body["series_reference_instruction"]


@pytest.mark.parametrize("repair_kind", ["hypothesis", "omit", "enum_only"])
def test_contextual_attribution_repair_receives_current_scope_and_stays_validated(repair_kind):
    skeleton, plan, catalog, validator = m7_fixture(attribution_context=True)
    reasoning = validated_reasoning(plan, validator, catalog)
    baseline = synthesis_payload(skeleton, plan, reasoning)
    initial = copy.deepcopy(baseline)
    factor = initial["attribution_summary"]["primary_factors"][0]
    factor.update(basis="correlated", evidence_refs=["e:context"],
                  explanation="A contextual upstream estimate may explain this result.")
    repaired = copy.deepcopy(initial)
    if repair_kind == "omit":
        repaired["attribution_summary"].update(primary_factors=[], summary="No cause can be established.")
    else:
        repaired["attribution_summary"]["primary_factors"][0]["basis"] = (
            "hypothesized" if repair_kind == "hypothesis" else "inferred"
        )

    class Model:
        def __init__(self):
            self.calls = []

        async def stream(self, request):
            self.calls.append(request)
            if request.phase is WaterbodyModelPhase.SCHEMA_REPAIR:
                body = json.loads(request.messages[0].text)
                constraints = {v["evidence_ref"]: v for v in body["attribution_reference_constraints"]}
                assert constraints["e:context"]["current_waterbody_applicability"] == "contextual_only"
                assert constraints["e:relation"]["dependency_evidence_refs"]
                assert set(constraints) <= set(plan.synthesis_plan.context.reference_allowlist)
                assert "Changing the basis enum alone" in body["attribution_repair_instruction"]
                assert body["synthesis_scope"]["validated_cells"]
                payload = repaired
            else:
                payload = initial
            for event in _completed_script(request.operation_id, request.model, payload):
                yield event

    model = Model()
    pending = _exchange(model, validator).run_synthesis(
        skeleton, plan.batch_plan, plan.synthesis_plan, reasoning, cancellation=_token()
    )
    if repair_kind == "enum_only":
        with pytest.raises(ValidationRepairFailedError) as raised:
            asyncio.run(pending)
        assert [i.code for i in raised.value.audit.repair_issues] == ["attribution_basis_invalid"]
    else:
        outcome = asyncio.run(pending)
        assert outcome.audit.accepted_operation_id == model.calls[1].operation_id
        assert outcome.validated.overall == validator.validate_synthesis(
            skeleton, plan.batch_plan, plan.synthesis_plan, reasoning, baseline
        ).overall
    assert len(model.calls) == 2


def test_repair_cites_range_only_support_without_lowering_a_direct_target_confidence():
    original, catalog, planned, target, flow = _low_basis_flow_support_fixture()
    values = {name: getattr(original.context, name) for name in type(original.context).model_fields
              if name not in {"schema_version", "working_context_digest"}}
    values["reference_allowlist"] = (*original.context.reference_allowlist, flow.evidence_ref)
    values["evidence_summaries"] = (*original.context.evidence_summaries, BoundedEvidenceSummary(
        evidence_ref=flow.evidence_ref, summary="Range-only derived discharge support: 5 to 50 ft3/s, estimated.",
        truncated=False))
    context = build_working_context_projection(**values)
    values = original.model_dump(mode="python", exclude={"batch_digest"})
    values.update(context=context, cells=(planned, *original.cells[1:]))
    batch = type(original)(**values, batch_digest=canonical_json_digest(values))
    initial = batch_payload(original, catalog)
    repaired = copy.deepcopy(initial)
    repaired["cells"][0].update(evidence_refs=[target.evidence_ref, flow.evidence_ref],
                                series_refs=[target.series_ref, flow.series_ref], quality_flags=["estimated"])

    class Model:
        calls = []

        async def stream(self, request):
            self.calls.append(request)
            if request.phase is WaterbodyModelPhase.SCHEMA_REPAIR:
                body = json.loads(request.messages[0].text)
                assert body["required_support_reference_groups"] == [
                    {"cell_id": planned.cell_id, "reference_options": [flow.evidence_ref]}]
                payload = repaired
            else:
                payload = initial
            for event in _completed_script(request.operation_id, request.model, payload):
                yield event

    model = Model()
    result = asyncio.run(_exchange(model, DraftValidator(catalog)).run_batch(batch, cancellation=_token()))
    assert len(model.calls) == 2
    assert result.validated.cells[0].confidence == .8
    assert result.validated.cells[0].representative_value == initial["cells"][0]["representative_value"]


def test_fixed_window_zero_confidence_contract_is_present_initially_and_in_repair():
    skeleton, plan, catalog, validator = m7_fixture(supported=lambda _p, w: w < 2)
    reasoning = validated_reasoning(plan, validator, catalog)
    valid = synthesis_payload(skeleton, plan, reasoning)
    invalid = copy.deepcopy(valid)
    for window in invalid["window_assessments"][2:]:
        window["confidence"] = .05
    fake = DeterministicWaterbodyModelPort(
        _completed_script(plan.synthesis_plan.operation_id, plan.synthesis_plan.model, invalid),
        _completed_script(_repair_id(plan.synthesis_plan.operation_id), plan.synthesis_plan.model, valid),
    )
    outcome = asyncio.run(_exchange(fake, validator).run_synthesis(
        skeleton, plan.batch_plan, plan.synthesis_plan, reasoning, cancellation=_token()))
    assert len(fake.calls) == 2
    initial, repair = [json.loads(request.messages[-1].text) for request in fake.calls]
    assert initial["fixed_window_constraints"] == repair["fixed_window_constraints"] == [
        {"window_id": window.window_id, "risk_level": "indeterminate", "confidence": 0,
         "contributing_cell_refs": []} for window in skeleton.windows[2:]]
    assert all(window.confidence == 0 for window in outcome.validated.window_assessments[2:])


def test_quantified_point_with_missing_bounds_needs_no_paid_repair() -> None:
    batch, catalog, payload, _refs = _mixed_model_estimate_fixture()
    validator = DraftValidator(catalog)
    baseline = validator.validate_batch(batch, payload)
    initial = copy.deepcopy(payload)
    for index in (0, 2, 3):
        initial["cells"][index]["value_lower"] = None
        initial["cells"][index]["value_upper"] = None
    fake = DeterministicWaterbodyModelPort(
        _completed_script(batch.operation_id, batch.model, initial),
    )

    outcome = asyncio.run(
        _exchange(fake, validator).run_batch(batch, cancellation=_token())
    )

    assert len(fake.calls) == 1
    assert outcome.audit.repair_request is None
    assert outcome.audit.initial_issues == ()
    assert outcome.validated.validation_digest == baseline.validation_digest
    raw = outcome.audit.initial_response.events[0].value
    assert validator.validate_batch(batch, raw).validation_digest == baseline.validation_digest
    assert [cell.representative_value for cell in outcome.validated.cells] == [
        "7", None, "7", "7",
    ]


def test_quantified_value_repair_prompt_preserves_saved_point() -> None:
    batch, catalog, payload, _refs = _mixed_model_estimate_fixture()
    initial = copy.deepcopy(payload)
    initial["cells"][0]["representative_value"] = "8"
    fake = DeterministicWaterbodyModelPort(
        _completed_script(batch.operation_id, batch.model, initial),
        _completed_script(_repair_id(batch.operation_id), batch.model, payload),
    )

    outcome = asyncio.run(
        _exchange(fake, DraftValidator(catalog)).run_batch(
            batch, cancellation=_token()
        )
    )

    assert len(fake.calls) == 2
    assert any(issue.code == "quantified_value_not_backed"
               for issue in outcome.audit.initial_issues)
    instruction = json.loads(fake.calls[1].messages[0].text)[
        "quantified_value_instruction"
    ]
    assert "keep its representative_value" in instruction
    assert "copy both saved value_lower and value_upper exactly" in instruction
    assert "only when the cited saved result has no point" in instruction


def test_repair_missing_range_bounds_replays_from_cited_sealed_fact() -> None:
    _skeleton, plan, catalog, _validator = m7_fixture()
    batch = plan.batch_plan.batches[0]
    range_cell = batch.cells[1]
    range_ref = range_cell.allowed_evidence_refs[0]
    source_fact = next(fact for fact in catalog.facts if fact.evidence_ref == range_ref)
    range_fact = source_fact.model_copy(update={
        "payload_type": "numeric_derived",
        "applicability": EvidenceApplicability.ALLOWED_INDIRECT,
        "result_eligibility": EvidenceResultEligibility.INFERRED_ONLY,
        "value": None,
        "value_lower": "5",
        "value_upper": "9",
        "method_kind": "model_estimate",
        "inference_basis": "general_knowledge",
        "aggregation_method": None,
        "source_record_refs": (),
        "dependency_evidence_refs": (),
        "quality_flags": (DataQualityFlag.ESTIMATED,),
        "numeric_method_id": "test:range_estimate",
        "numeric_method_version": 1,
        "numeric_task_id": "task:test:range",
        "numeric_plan_digest": "sha256:test-plan",
        "numeric_plan_item_id": "item:test-plan",
        "numeric_method_registry_digest": "sha256:test-registry",
        "numeric_confidence_cap": 0.1,
    })
    catalog = rebuild_catalog(
        catalog,
        facts=tuple(
            range_fact if fact.evidence_ref == range_ref else fact
            for fact in catalog.facts
        ),
        cell_scopes=tuple(
            scope.model_copy(update={
                "target_direct_refs": (),
                "target_indirect_refs": (range_ref,),
            }) if scope.cell_id == range_cell.cell_id else scope
            for scope in catalog.cell_scopes
        ),
    )
    repair = batch_payload(batch, catalog)
    repair["cells"][1].update(
        outcome_status="range_estimate",
        representative_value=None,
        value_lower="5",
        value_upper="9",
        source="inferred",
        confidence=0.1,
        aggregation_method=None,
        quality_flags=["estimated"],
    )
    initial = copy.deepcopy(repair)
    initial["cells"][1]["evidence_refs"].append(
        batch.cells[0].allowed_evidence_refs[0]
    )
    del repair["cells"][1]["value_lower"]
    del repair["cells"][1]["value_upper"]
    fake = DeterministicWaterbodyModelPort(
        _completed_script(batch.operation_id, batch.model, initial),
        _completed_script(_repair_id(batch.operation_id), batch.model, repair),
    )

    outcome = asyncio.run(
        _exchange(fake, DraftValidator(catalog)).run_batch(
            batch, cancellation=_token()
        )
    )

    assert len(fake.calls) == 2
    assert [(issue.code, issue.path) for issue in outcome.audit.initial_issues] == [
        ("evidence_out_of_scope", "$.cells[1].evidence_refs")
    ]
    assert outcome.audit.repair_issues == ()
    assert outcome.audit.accepted_operation_id == _repair_id(batch.operation_id)
    raw_repair = outcome.audit.repair_response.events[0].value
    assert "value_lower" not in json.loads(raw_repair)["cells"][1]
    assert "value_upper" not in json.loads(raw_repair)["cells"][1]
    assert outcome.validated.cells[1].value_lower == "5"
    assert outcome.validated.cells[1].value_upper == "9"
    assert outcome.audit.validated_digest == DraftValidator(catalog).validate_batch(
        batch, raw_repair
    ).validation_digest


def test_uncited_known_evidence_in_rationale_repairs_by_removing_claim() -> None:
    _skeleton, plan, catalog, validator = m7_fixture()
    batch = plan.batch_plan.batches[0]
    initial = batch_payload(batch, catalog)
    foreign_ref = batch.cells[1].allowed_evidence_refs[0]
    initial["cells"][0]["risk_rationale"] = (
        f"The other cell's evidence {foreign_ref} supports this risk judgment."
    )
    repaired = batch_payload(batch, catalog)
    fake = DeterministicWaterbodyModelPort(
        _completed_script(batch.operation_id, batch.model, initial),
        _completed_script(_repair_id(batch.operation_id), batch.model, repaired),
    )

    outcome = asyncio.run(
        _exchange(fake, validator).run_batch(batch, cancellation=_token())
    )

    assert len(fake.calls) == 2
    assert any(
        issue.code == "text_evidence_ref_missing"
        and issue.path == "$.cells[0].risk_rationale"
        for issue in outcome.audit.initial_issues
    )
    assert json.loads(fake.calls[1].messages[0].text)["issues"] == [
        {"code": "text_evidence_ref_missing", "path": "$.cells[0].risk_rationale"}
    ]
    assert foreign_ref not in outcome.validated.cells[0].risk_rationale


def test_validated_batch_decision_records_applied_unit_source() -> None:
    baseline, _, _, _ = m7_fixture()
    preference = UnitPreference(
        parameter=baseline.requested_parameters[0],
        target_unit_id="milligram_per_liter",
        registry_digest=load_parameter_capability_registry().manifest_digest,
        source="session_override",
    )
    _, plan, catalog, validator = m7_fixture(unit_preference=preference)
    batch = plan.batch_plan.batches[0]
    fake = DeterministicWaterbodyModelPort(
        _completed_script(batch.operation_id, batch.model,
                          batch_payload(batch, catalog)),
    )
    outcome = asyncio.run(
        _exchange(fake, validator).run_batch(batch, cancellation=_token())
    )
    assert outcome.audit.unit_decisions == (preference,)


def test_single_allowed_unit_fills_explicit_null_without_model_repair(caplog) -> None:
    _skeleton, plan, catalog, validator = m7_fixture()
    batch = plan.batch_plan.batches[0]
    payload = batch_payload(batch, catalog)
    payload["cells"][0]["target_unit_id"] = None
    fake = DeterministicWaterbodyModelPort(
        _completed_script(batch.operation_id, batch.model, payload),
    )

    with caplog.at_level("INFO", logger="wateragents.reasoning"):
        outcome = asyncio.run(
            _exchange(fake, validator).run_batch(batch, cancellation=_token())
        )

    assert len(fake.calls) == 1
    assert outcome.audit.repair_request is None
    assert outcome.validated.cells[0].target_unit_id == batch.cells[0].allowed_unit_ids[0]
    assert json.loads(outcome.audit.initial_response.events[0].value)["cells"][0][
        "target_unit_id"
    ] is None
    assert any(
        record.event_name == "reasoning_draft_single_unit_filled"
        and record.schema_path == "$.cells[0].target_unit_id"
        for record in caplog.records
    )


def test_flag_only_mismatch_does_not_call_model_repair(caplog) -> None:
    _skeleton, plan, catalog, validator = m7_fixture()
    batch = plan.batch_plan.batches[0]
    payload = batch_payload(batch, catalog)
    payload["cells"][0]["quality_flags"] = ["partial_coverage"]
    fake = DeterministicWaterbodyModelPort(
        _completed_script(batch.operation_id, batch.model, payload),
    )

    with caplog.at_level("INFO", logger="wateragents.reasoning"):
        outcome = asyncio.run(
            _exchange(fake, validator).run_batch(batch, cancellation=_token())
        )

    assert len(fake.calls) == 1
    assert outcome.audit.repair_request is None
    assert outcome.audit.initial_issues == ()
    assert outcome.validated.cells[0].quality_flags == ()
    assert json.loads(outcome.audit.initial_response.events[0].value)["cells"][0][
        "quality_flags"
    ] == ["partial_coverage"]
    assert any(
        getattr(record, "event_name", None)
        == "reasoning_draft_quality_flags_normalized"
        and record.schema_path == "$.cells[0].quality_flags"
        for record in caplog.records
    )


def test_repair_response_with_only_extra_quality_flag_is_accepted() -> None:
    _skeleton, plan, catalog, validator = m7_fixture()
    batch = plan.batch_plan.batches[0]
    initial = batch_payload(batch, catalog)
    initial["cells"][0]["representative_value"] = 7
    repaired = batch_payload(batch, catalog)
    repaired["cells"][0]["quality_flags"] = ["partial_coverage"]
    fake = DeterministicWaterbodyModelPort(
        _completed_script(batch.operation_id, batch.model, initial),
        _completed_script(_repair_id(batch.operation_id), batch.model, repaired),
    )

    outcome = asyncio.run(
        _exchange(fake, validator).run_batch(batch, cancellation=_token())
    )

    assert len(fake.calls) == 2
    assert outcome.audit.initial_issues
    assert outcome.audit.repair_issues == ()
    assert outcome.audit.accepted_operation_id == _repair_id(batch.operation_id)
    assert outcome.validated.cells[0].quality_flags == ()
    assert json.loads(outcome.audit.repair_response.events[0].value)["cells"][0][
        "quality_flags"
    ] == ["partial_coverage"]


def test_single_unit_fill_still_requires_evidence_backed_value() -> None:
    _skeleton, plan, catalog, validator = m7_fixture()
    batch = plan.batch_plan.batches[0]
    invalid = batch_payload(batch, catalog)
    invalid["cells"][0]["target_unit_id"] = None
    invalid["cells"][0]["representative_value"] = "9999"
    fake = DeterministicWaterbodyModelPort(
        _completed_script(batch.operation_id, batch.model, invalid),
        _completed_script(
            _repair_id(batch.operation_id), batch.model, batch_payload(batch, catalog)
        ),
    )

    outcome = asyncio.run(
        _exchange(fake, validator).run_batch(batch, cancellation=_token())
    )

    assert len(fake.calls) == 2
    assert any(
        issue.code == "quantified_value_not_backed"
        and issue.path == "$.cells[0].representative_value"
        for issue in outcome.audit.initial_issues
    )


def test_missing_structured_output_repairs_once_with_bounded_visible_text() -> None:
    _skeleton, plan, catalog, validator = m7_fixture()
    batch = plan.batch_plan.batches[0]
    repaired = batch_payload(batch, catalog)
    initial_script = (
        VisibleTextDelta(
            operation_id=batch.operation_id,
            model=batch.model,
            message_id="visible-1",
            text="I omitted the required structured object.",
        ),
        Terminal(
            operation_id=batch.operation_id,
            model=batch.model,
            provider_response_id="response:visible-only",
            finish_reason=WaterbodyFinishReason.COMPLETED,
        ),
    )
    fake = DeterministicWaterbodyModelPort(
        initial_script,
        _completed_script(
            _repair_id(batch.operation_id),
            batch.model,
            repaired,
            output_id="repair",
        ),
    )

    outcome = asyncio.run(
        _exchange(fake, validator).run_batch(batch, cancellation=_token())
    )

    repair_body = json.loads(fake.calls[1].messages[0].text)
    assert outcome.audit.initial_output_id is None
    assert outcome.audit.initial_issues[0].code == "missing_structured_output"
    assert "omitted the required structured object" in repair_body[
        "original_visible_response"
    ]
    assert len(fake.calls) == 2


@pytest.mark.parametrize("bad_repair", [False, True])
@pytest.mark.parametrize("with_guidelines", [False, True])
def test_completed_malformed_json_from_real_http_adapter_enters_only_one_scoped_repair(bad_repair, with_guidelines):
    from app.infrastructure.adapters import OpenAIResponsesWaterbodyAdapter
    from tests.infrastructure.test_openai_responses_waterbody import client, encode_events, response_for, malformed_object_events

    _skeleton, plan, catalog, validator = m7_fixture()
    original = plan.batch_plan.batches[0]
    values = original.model_dump(mode="python", exclude={"batch_digest"})
    values["deadline"] = datetime.now(UTC) + timedelta(seconds=60)
    batch = type(original)(**values, batch_digest=canonical_json_digest(values))
    valid = batch_payload(batch, catalog)
    raw = '{"note":"pH indicates "low" risk"}'
    requests = []
    knowledge = None
    if with_guidelines:
        from tests.application.guidelines.test_model_context import Session, model_context
        from tests.guideline_support import version, content
        session = Session((version(content(parameter_keys=(batch.parameter,))),))
        session.pin.run_id, session.session_id = batch.run_id, batch.session_id
        knowledge = model_context(session, batch.model)

    def handler(request):
        requests.append(request)
        if with_guidelines:
            messages = [json.loads(message["content"]) for message in json.loads(request.content)["input"]]
            alias = next(entry["alias"] for message in messages for entry in message.get("advisory_knowledge", []))
            valid["cells"][0]["guideline_refs"] = [alias]
        answer = raw if len(requests) == 1 or bad_repair else json.dumps(valid)
        return response_for(request, (encode_events(malformed_object_events(answer, batch.model.model)),))

    async def exercise():
        async with client(handler) as http:
            adapter = OpenAIResponsesWaterbodyAdapter(http, api_key="qxv0K8pL7s6D5n4M3z2R",
                                                       base_url="https://api.openai.test/v1")

            class FixtureProvider:
                async def stream(self, request):
                    routed = request.model_copy(update={"model": type(request.model)(
                        provider="openai", model=request.model.model)})
                    async for event in adapter.stream(routed):
                        yield event.model_copy(update={"model": request.model})

            exchange = ValidationExchange(model_port=FixtureProvider(), validator=validator,
                prompt_builder=PromptBuilder(DEFAULT_REASONING_PROMPT_POLICY), guidelines=knowledge)
            return await exchange.run_batch(batch, cancellation=_token())

    if bad_repair:
        with pytest.raises(ValidationRepairFailedError):
            asyncio.run(exercise())
    else:
        outcome = asyncio.run(exercise())
        assert outcome.audit.initial_issues[0].code == "missing_structured_output"
        assert raw in json.loads(json.loads(outcome.audit.repair_request.messages[0].text)[
            "original_visible_response"])["visible_text"]
        assert outcome.audit.accepted_operation_id == _repair_id(batch.operation_id)
        assert outcome.validated == validator.validate_batch(batch, valid,
            guideline_aliases=knowledge.aliases(batch.operation_id) if knowledge is not None else frozenset())
    if knowledge is not None:
        assert len(knowledge.usages) == (0 if bad_repair else 1)
        if not bad_repair:
            assert knowledge.usages[0].operation_id == _repair_id(batch.operation_id)
    assert len(requests) == 2


@pytest.mark.parametrize(
    ("risk", "confidence"),
    (
        ("none", 0.0),
        ("low", 0.2),
        ("medium", 0.5),
        ("high", 0.8),
        ("indeterminate", 1.0),
    ),
)
def test_legal_risk_and_confidence_never_trigger_semantic_repair(
    risk: str,
    confidence: float,
) -> None:
    _skeleton, plan, catalog, validator = m7_fixture()
    batch = plan.batch_plan.batches[0]
    payload = batch_payload(batch, catalog)
    payload["cells"][0].update(
        risk_level=risk,
        confidence=confidence,
        risk_rationale="This is the Agent's evidence-linked risk interpretation.",
        confidence_rationale="This is the Agent's evidence-linked confidence judgment.",
    )
    fake = DeterministicWaterbodyModelPort(
        _completed_script(batch.operation_id, batch.model, payload),
    )

    outcome = asyncio.run(
        _exchange(fake, validator).run_batch(batch, cancellation=_token())
    )

    assert len(fake.calls) == 1
    assert outcome.validated.cells[0].risk_level.value == risk
    assert outcome.validated.cells[0].confidence == confidence
    assert outcome.audit.repair_request is None


def test_invalid_repair_fails_whole_exchange_without_copying_private_body(caplog) -> None:
    _skeleton, plan, catalog, validator = m7_fixture()
    batch = plan.batch_plan.batches[0]
    invalid = batch_payload(batch, catalog)
    invalid["scope_digest"] = "sha256:wrong"
    invalid["cells"][0]["explanation"] = "See https://private.invalid/raw-body"
    repair_invalid = batch_payload(batch, catalog)
    repair_invalid["cells"][0]["target_unit_id"] = "unknown-unit"
    repair_invalid["cells"][0]["privateSecretField"] = "bad"
    fake = DeterministicWaterbodyModelPort(
        _completed_script(batch.operation_id, batch.model, invalid, output_id="initial"),
        _completed_script(
            _repair_id(batch.operation_id),
            batch.model,
            repair_invalid,
            output_id="repair",
        ),
    )

    with (
        caplog.at_level("WARNING", logger="wateragents.reasoning"),
        pytest.raises(ValidationRepairFailedError) as raised,
    ):
        asyncio.run(
            _exchange(fake, validator).run_batch(batch, cancellation=_token())
        )

    error = raised.value
    assert error.code == "waterbody_model_repair_failed"
    assert error.cause_code == "reasoning_draft_invalid"
    assert len(fake.calls) == 2
    assert "https://private.invalid" in error.audit.initial_response.events[0].value
    assert "https://private.invalid" not in fake.calls[1].messages[0].text
    assert error.audit.repair_issues
    records = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None)
        == "waterbody_model_validation_repair_failed"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.run_id == batch.run_id
    assert record.session_id == batch.session_id
    assert record.initial_operation_id == batch.operation_id
    assert record.repair_operation_id == _repair_id(batch.operation_id)
    assert record.failure_code == "reasoning_draft_invalid"
    assert record.initial_issue_count == len(error.audit.initial_issues)
    assert record.repair_issue_count == len(error.audit.repair_issues)
    assert {issue["code"] for issue in record.initial_issues} >= {
        "invalid_value"
    }
    assert {issue["code"] for issue in record.repair_issues} == {"extra_field"}
    assert any(issue["path"] == "[redacted]" for issue in record.repair_issues)
    assert record.initial_output_chars > 0
    assert record.repair_output_chars > 0
    assert "https://private.invalid" not in json.dumps(
        (record.initial_issues, record.repair_issues, record.getMessage())
    )
    assert "privateSecretField" not in json.dumps(record.repair_issues)


@pytest.mark.parametrize("failure_kind", ("protocol", "timeout", "content_filter"))
def test_repair_protocol_timeout_and_content_filter_are_typed_whole_failures(
    failure_kind: str, caplog,
) -> None:
    _skeleton, plan, catalog, validator = m7_fixture()
    batch = plan.batch_plan.batches[0]
    invalid = batch_payload(batch, catalog)
    invalid["scope_digest"] = "sha256:wrong"
    repair_operation = _repair_id(batch.operation_id)
    if failure_kind == "protocol":
        valid = batch_payload(batch, catalog)
        repair_script = (
            *_completed_script(
                repair_operation,
                batch.model,
                valid,
                output_id="first",
            )[:1],
            *_completed_script(
                repair_operation,
                batch.model,
                valid,
                output_id="duplicate",
            ),
        )
        expected_code = "waterbody_model_response_invalid"
    elif failure_kind == "timeout":
        repair_script = (
            WaterbodyModelTimeoutError(
                provider=batch.model.provider,
                model=batch.model.model,
                operation_id=str(repair_operation),
            ),
        )
        expected_code = "waterbody_model_timeout"
    else:
        repair_script = (
            Terminal(
                operation_id=repair_operation,
                model=batch.model,
                provider_response_id="response:filtered",
                finish_reason=WaterbodyFinishReason.CONTENT_FILTERED,
            ),
        )
        expected_code = "waterbody_model_content_filtered"
    fake = DeterministicWaterbodyModelPort(
        _completed_script(batch.operation_id, batch.model, invalid),
        repair_script,
    )

    with (
        caplog.at_level("WARNING", logger="wateragents.reasoning"),
        pytest.raises(ValidationRepairFailedError) as raised,
    ):
        asyncio.run(
            _exchange(fake, validator).run_batch(batch, cancellation=_token())
        )

    assert raised.value.cause_code == expected_code
    assert raised.value.audit.repair_error_code == expected_code
    assert raised.value.audit.accepted_operation_id is None
    assert len(fake.calls) == 2
    records = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None)
        == "waterbody_model_validation_repair_failed"
    ]
    assert len(records) == 1
    assert records[0].failure_code == expected_code
    assert records[0].initial_issue_count == 1
    assert records[0].repair_issue_count == 0
    assert records[0].repair_issues == []
    assert records[0].repair_output_chars == 0


def test_synthesis_has_the_same_single_repair_boundary() -> None:
    skeleton, plan, catalog, validator = m7_fixture()
    reasoning = validated_reasoning(plan, validator, catalog)
    invalid = synthesis_payload(skeleton, plan, reasoning)
    invalid["scope_digest"] = "sha256:wrong"
    repaired = synthesis_payload(skeleton, plan, reasoning)
    operation = plan.synthesis_plan.operation_id
    model = plan.synthesis_plan.model
    fake = DeterministicWaterbodyModelPort(
        _completed_script(operation, model, invalid, output_id="initial"),
        _completed_script(
            _repair_id(operation),
            model,
            repaired,
            output_id="repair",
        ),
    )

    outcome = asyncio.run(
        _exchange(fake, validator).run_synthesis(
            skeleton,
            plan.batch_plan,
            plan.synthesis_plan,
            reasoning,
            cancellation=_token(),
        )
    )

    assert len(fake.calls) == 2
    assert fake.calls[1].tool_descriptors == ()
    initial_body = json.loads(fake.calls[0].messages[-1].text)
    repair_body = json.loads(fake.calls[1].messages[-1].text)
    assert repair_body["synthesis_scope"] == {
        key: initial_body[key]
        for key in (
            "requested_parameters",
            "window_ids",
            "overall_window_refs",
            "validated_cells",
            "fixed_cells",
        )
    }
    assert "bounded_reference_summaries" not in repair_body
    assert outcome.audit.accepted_operation_id == _repair_id(operation)
    assert outcome.validated.scope_digest == plan.synthesis_plan.scope_digest


def test_earlier_validated_batches_cannot_form_a_partial_reasoning_set() -> None:
    _skeleton, plan, catalog, validator = m7_fixture(max_cells=2)
    first, second = plan.batch_plan.batches
    second_invalid = batch_payload(second, catalog)
    second_invalid["scope_digest"] = "sha256:wrong"
    repair_invalid = copy.deepcopy(second_invalid)
    fake = DeterministicWaterbodyModelPort(
        _completed_script(
            first.operation_id,
            first.model,
            batch_payload(first, catalog),
            output_id="first",
        ),
        _completed_script(
            second.operation_id,
            second.model,
            second_invalid,
            output_id="second-invalid",
        ),
        _completed_script(
            _repair_id(second.operation_id),
            second.model,
            repair_invalid,
            output_id="second-repair-invalid",
        ),
    )
    exchange = _exchange(fake, validator)

    first_outcome = asyncio.run(exchange.run_batch(first, cancellation=_token()))
    with pytest.raises(ValidationRepairFailedError):
        asyncio.run(exchange.run_batch(second, cancellation=_token()))
    with pytest.raises(DraftValidationError) as raised:
        validator.complete_batches(
            plan.batch_plan,
            (first_outcome.validated,),
        )

    assert "missing_batch" in {issue.code for issue in raised.value.issues}
    assert first_outcome.audit.validated_digest is not None
    assert len(fake.calls) == 3

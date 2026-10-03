import asyncio
import copy
import json
from uuid import NAMESPACE_URL, uuid5

import pytest

from app.application.analysis_runs.workflow import CancellationToken
from app.application.reasoning import (
    DraftValidationError,
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
from app.application.reasoning.prompt_policy import DEFAULT_REASONING_PROMPT_POLICY
from app.ports import WaterbodyModelTimeoutError
from tests.application.reasoning.fakes import DeterministicWaterbodyModelPort
from tests.application.reasoning.m7_support import (
    batch_payload,
    m7_fixture,
    synthesis_payload,
    validated_reasoning,
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
    assert "evidence_summaries" not in repair_body
    assert "bounded_reference_summaries" not in repair_body
    assert repair_body["repair_of_operation_id"] == str(batch.operation_id)
    assert outcome.audit.initial_issues[0].code == "scope_digest_mismatch"
    assert outcome.audit.accepted_operation_id == repair_request.operation_id
    assert outcome.audit.validated_digest == outcome.validated.validation_digest


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


def test_invalid_repair_fails_whole_exchange_without_copying_private_body() -> None:
    _skeleton, plan, catalog, validator = m7_fixture()
    batch = plan.batch_plan.batches[0]
    invalid = batch_payload(batch, catalog)
    invalid["scope_digest"] = "sha256:wrong"
    invalid["cells"][0]["explanation"] = "See https://private.invalid/raw-body"
    repair_invalid = batch_payload(batch, catalog)
    repair_invalid["cells"][0]["target_unit_id"] = "unknown-unit"
    fake = DeterministicWaterbodyModelPort(
        _completed_script(batch.operation_id, batch.model, invalid, output_id="initial"),
        _completed_script(
            _repair_id(batch.operation_id),
            batch.model,
            repair_invalid,
            output_id="repair",
        ),
    )

    with pytest.raises(ValidationRepairFailedError) as raised:
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


@pytest.mark.parametrize("failure_kind", ("protocol", "timeout", "content_filter"))
def test_repair_protocol_timeout_and_content_filter_are_typed_whole_failures(
    failure_kind: str,
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

    with pytest.raises(ValidationRepairFailedError) as raised:
        asyncio.run(
            _exchange(fake, validator).run_batch(batch, cancellation=_token())
        )

    assert raised.value.cause_code == expected_code
    assert raised.value.audit.repair_error_code == expected_code
    assert raised.value.audit.accepted_operation_id is None
    assert len(fake.calls) == 2


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

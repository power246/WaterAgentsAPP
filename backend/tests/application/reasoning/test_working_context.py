from uuid import UUID

import pytest
from pydantic import ValidationError

from app.application.reasoning.model_models import (
    WaterbodyMessageRole,
    WaterbodyModelMessage,
)
from app.application.reasoning.working_context import (
    BoundedEvidenceSummary,
    WorkingContextFact,
    WorkingContextFactKind,
    WorkingContextProjection,
    build_working_context_projection,
)
from app.domain import ItemReference

RUN_ID = UUID("10000000-0000-4000-8000-000000000001")
SESSION_ID = UUID("20000000-0000-4000-8000-000000000001")


def fact(fact_id: str, *, summary: str = "A bounded gate fact.") -> WorkingContextFact:
    return WorkingContextFact(
        fact_id=fact_id,
        kind=WorkingContextFactKind.GATE,
        summary=summary,
        refs=("evidence-1",),
    )


def evidence(evidence_ref: str) -> BoundedEvidenceSummary:
    return BoundedEvidenceSummary(
        evidence_ref=evidence_ref,
        summary="A bounded evidence summary.",
        source_record_item_refs=("source-item-1",),
        series_refs=("series-1",),
        truncated=False,
    )


def build_context(
    *,
    facts: tuple[WorkingContextFact, ...] | None = None,
    summaries: tuple[BoundedEvidenceSummary, ...] | None = None,
) -> WorkingContextProjection:
    return build_working_context_projection(
        run_id=RUN_ID,
        session_id=SESSION_ID,
        target_comid="12345678",
        scope_digest="sha256:scope",
        bundle_ref=ItemReference(item_id="bundle-item-1"),
        bundle_digest="bundle:sha256:bundle",
        rule_plan_digest="rule:sha256:rules",
        capability_registry_digest="sha256:capabilities",
        reference_allowlist=(
            "source-item-1",
            "evidence-2",
            "evidence-1",
            "cell-1",
        ),
        recent_messages=(
            WaterbodyModelMessage(
                message_id="message-1",
                role=WaterbodyMessageRole.ASSISTANT,
                text="A bounded visible message.",
            ),
        ),
        facts=facts or (fact("fact-b"), fact("fact-a")),
        evidence_summaries=summaries or (evidence("evidence-2"), evidence("evidence-1")),
    )


def test_working_context_is_canonical_bounded_and_digest_stable() -> None:
    first = build_context()
    second = build_context(
        facts=tuple(reversed(first.facts)),
        summaries=tuple(reversed(first.evidence_summaries)),
    )

    assert first == second
    assert tuple(item.fact_id for item in first.facts) == ("fact-a", "fact-b")
    assert tuple(item.evidence_ref for item in first.evidence_summaries) == (
        "evidence-1",
        "evidence-2",
    )
    assert first.reference_allowlist == tuple(sorted(first.reference_allowlist))
    assert first.working_context_digest.startswith("sha256:")
    assert type(first).model_validate(first.model_dump(mode="json")) == first


def test_working_context_digest_rejects_tampering() -> None:
    payload = build_context().model_dump(mode="python")
    payload["bundle_digest"] = "bundle:sha256:changed"

    with pytest.raises(ValidationError, match="digest"):
        WorkingContextProjection.model_validate(payload)


@pytest.mark.parametrize(
    "unsafe_text",
    (
        "Authorization: Bearer private-value",
        "https://provider.invalid/raw?api_key=private",
        "back" + "end_bk archived field",
        "raw_provider_envelope payload",
        "encrypted_reasoning payload",
    ),
)
def test_working_context_rejects_secret_url_archive_and_provider_payload_markers(
    unsafe_text: str,
) -> None:
    with pytest.raises(ValidationError, match="safe prompt text"):
        fact("fact-unsafe", summary=unsafe_text)


def test_working_context_rejects_out_of_scope_refs_and_unbounded_collections() -> None:
    with pytest.raises(ValueError, match="reference allowlist"):
        build_context(
            summaries=(
                BoundedEvidenceSummary(
                    evidence_ref="outside-evidence",
                    summary="Outside scope.",
                    source_record_item_refs=(),
                    series_refs=(),
                    truncated=False,
                ),
            )
        )

    with pytest.raises(ValueError, match="at most 16"):
        build_working_context_projection(
            run_id=RUN_ID,
            session_id=SESSION_ID,
            target_comid="12345678",
            scope_digest="sha256:scope",
            bundle_ref=ItemReference(item_id="bundle-item-1"),
            bundle_digest="bundle:sha256:bundle",
            rule_plan_digest="rule:sha256:rules",
            capability_registry_digest="sha256:capabilities",
            reference_allowlist=(),
            recent_messages=tuple(
                WaterbodyModelMessage(
                    message_id=f"message-{index}",
                    role=WaterbodyMessageRole.ASSISTANT,
                    text="bounded",
                )
                for index in range(17)
            ),
            facts=(),
            evidence_summaries=(),
        )


def test_working_context_contains_only_projection_fields_not_raw_records() -> None:
    fields = set(WorkingContextProjection.model_fields)

    assert {
        "scope_digest",
        "bundle_ref",
        "bundle_digest",
        "rule_plan_digest",
        "capability_registry_digest",
        "recent_messages",
        "facts",
        "evidence_summaries",
        "working_context_digest",
    } <= fields
    assert not {"source_records", "provider_response", "raw_body", "ui_preview"} & fields

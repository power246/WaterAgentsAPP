from dataclasses import replace

import pytest

from app.application.transcript_memory import (
    RAGSourceClosureError,
    SourceClosureExpectation,
    build_prepared_projection,
    validate_source_closure,
)
from app.domain import InferenceMode, ParameterKey
from tests.application.transcript_memory.m3_support import (
    GENERATION_ID,
    PROFILE_ID,
    case_source,
)


def _expectation(bundle, **changes) -> SourceClosureExpectation:
    values = {
        "active_generation_id": GENERATION_ID,
        "case_ref": bundle.case.case_ref,
        "projection_digest": bundle.case.projection_digest,
        "authorized_comids": (bundle.case.case_ref.target_comid,),
        "authorized_parameter_keys": bundle.case.parameter_keys,
        "authorized_source_window_ids": bundle.case.window_ids,
    }
    values.update(changes)
    return SourceClosureExpectation(**values)


def test_source_closure_revalidates_active_projection_and_current_scope() -> None:
    source = case_source()
    bundle = build_prepared_projection(
        source,
        generation_id=GENERATION_ID,
        profile_id=PROFILE_ID,
    )

    validated = validate_source_closure(
        source,
        projection=bundle.case,
        expectation=_expectation(bundle),
    )

    assert validated.source is source
    assert validated.eligible_fragments == source.fragments


@pytest.mark.parametrize(
    ("changes", "expected"),
    (
        (
            {"active_generation_id": "81000000-0000-4000-8000-000000000099"},
            "source_active_projection_mismatch",
        ),
        (
            {"projection_digest": "sha256:not-the-active-projection"},
            "source_active_projection_mismatch",
        ),
        (
            {"authorized_comids": ("999",)},
            "source_scope_comid_mismatch",
        ),
        (
            {
                "authorized_parameter_keys": (
                    ParameterKey(code_system="usgs", code="00010"),
                )
            },
            "source_scope_parameter_mismatch",
        ),
        (
            {"authorized_source_window_ids": ("window-not-authorized",)},
            "source_scope_window_mismatch",
        ),
    ),
)
def test_source_closure_fails_closed_for_stale_identity_or_scope(
    changes,
    expected,
) -> None:
    source = case_source()
    bundle = build_prepared_projection(
        source,
        generation_id=GENERATION_ID,
        profile_id=PROFILE_ID,
    )

    with pytest.raises(RAGSourceClosureError, match=expected):
        validate_source_closure(
            source,
            projection=bundle.case,
            expectation=_expectation(bundle, **changes),
        )


def test_source_closure_does_not_trust_result_context_compatibility() -> None:
    source = case_source()
    incompatible_result = replace(
        source.result,
        body=source.result.body.model_copy(update={"mode": InferenceMode.GAP_FILLING}),
    )

    with pytest.raises(RAGSourceClosureError, match="source_result_context_mismatch"):
        validate_source_closure(replace(source, result=incompatible_result))


def test_source_closure_requires_referenced_fragment_to_be_loaded() -> None:
    source = case_source()

    with pytest.raises(RAGSourceClosureError, match="source_fragment_closure_incomplete"):
        validate_source_closure(replace(source, fragments=()))

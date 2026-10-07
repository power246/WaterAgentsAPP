import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.application.transcript_memory import (
    RAGSourceClosureError,
    build_prepared_projection,
)
from app.domain import CanonicalCaseQueryV1, InferenceMode
from tests.application.transcript_memory.m3_support import (
    GENERATION_ID,
    PROFILE_ID,
    case_source,
)


GOLDEN_CANONICAL_TEXT = (
    '{"mode":"prediction","normalized_intent_tokens":['
    '"analyze_current_state","predict","requires_discharge_support"],'
    '"projection_schema_version":1,"requested_parameter_keys":['
    '{"code":"00300","code_system":"usgs"}],"schema_version":1,'
    '"supporting_parameter_keys":[{"code":"00060","code_system":"usgs"}],'
    '"template_version":1,"window_descriptors":['
    '{"duration_days":"0.25","local_months":[9],"resolution":"6h",'
    '"season_buckets":["SON"],"window_kind":"prediction"},'
    '{"duration_days":"0.25","local_months":[9],"resolution":"6h",'
    '"season_buckets":["SON"],"window_kind":"prediction"},'
    '{"duration_days":"0.25","local_months":[9],"resolution":"6h",'
    '"season_buckets":["SON"],"window_kind":"prediction"},'
    '{"duration_days":"0.25","local_months":[9],"resolution":"6h",'
    '"season_buckets":["SON"],"window_kind":"prediction"}]}'
)
GOLDEN_CANONICAL_DIGEST = (
    "sha256:e6eb4684faad50a02b97c096602e9a096bc23e582fa0ee3cc87124570b1e738b"
)


def test_projection_replay_and_canonical_golden_are_stable() -> None:
    source = case_source()

    first = build_prepared_projection(
        source,
        generation_id=GENERATION_ID,
        profile_id=PROFILE_ID,
    )
    replay = build_prepared_projection(
        source,
        generation_id=GENERATION_ID,
        profile_id=PROFILE_ID,
    )

    assert first == replay
    assert first.case.canonical_query == GOLDEN_CANONICAL_TEXT
    assert first.case.canonical_query_digest == GOLDEN_CANONICAL_DIGEST
    assert first.case.case_ref.case_id == "0f74a476-eeb9-5506-819d-6004b80237de"
    assert len(first.case.windows) == 4
    assert len(first.fragments) == 1


def test_equivalent_query_input_order_and_hash_seed_do_not_change_bytes() -> None:
    script = """
from app.domain import CanonicalCaseQueryV1, CanonicalIntentToken, CanonicalWindowDescriptorV1, InferenceMode, MeteorologicalSeason, ParameterKey, TimeResolution
codes = {"00300", "00010"}
tokens = {CanonicalIntentToken.PREDICT, CanonicalIntentToken.ANALYZE_CURRENT_STATE}
query = CanonicalCaseQueryV1(
    mode=InferenceMode.PREDICTION,
    requested_parameter_keys=tuple(ParameterKey(code_system="usgs", code=code) for code in codes),
    supporting_parameter_keys=(ParameterKey(code_system="usgs", code="00060"),),
    window_descriptors=(CanonicalWindowDescriptorV1(
        window_kind=InferenceMode.PREDICTION,
        duration_days="0.25",
        local_months=(9,),
        season_buckets=(MeteorologicalSeason.SON,),
        resolution=TimeResolution.SIX_HOURS,
    ),),
    normalized_intent_tokens=tuple(tokens),
)
print(query.canonical_text)
print(query.canonical_digest)
"""
    backend_root = Path(__file__).parents[3]
    outputs = []
    for seed in ("1", "8675309"):
        completed = subprocess.run(
            [sys.executable, "-B", "-c", script],
            cwd=backend_root,
            env={**os.environ, "PYTHONHASHSEED": seed},
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(completed.stdout)

    assert outputs[0] == outputs[1]


def test_version_semantic_and_source_identity_changes_change_digests() -> None:
    source = case_source()
    original = build_prepared_projection(
        source,
        generation_id=GENERATION_ID,
        profile_id=PROFILE_ID,
    )
    canonical = CanonicalCaseQueryV1.model_validate_json(
        original.case.canonical_query
    )
    new_version = canonical.model_copy(update={"template_version": 2})
    new_semantics = canonical.model_copy(update={"mode": InferenceMode.GAP_FILLING})
    new_generation = build_prepared_projection(
        source,
        generation_id="81000000-0000-4000-8000-000000000021",
        profile_id=PROFILE_ID,
    )
    new_source = case_source(
        result_item_id=UUID("81000000-0000-4000-8000-000000000022")
    )
    new_source_projection = build_prepared_projection(
        new_source,
        generation_id=GENERATION_ID,
        profile_id=PROFILE_ID,
    )

    assert new_version.canonical_digest != canonical.canonical_digest
    assert new_semantics.canonical_digest != canonical.canonical_digest
    assert new_generation.case.case_ref.case_id != original.case.case_ref.case_id
    assert new_generation.case.projection_digest != original.case.projection_digest
    assert new_source_projection.case.case_ref.case_id != original.case.case_ref.case_id


def test_projection_rejects_canonical_semantics_that_do_not_match_source() -> None:
    bundle = build_prepared_projection(
        case_source(),
        generation_id=GENERATION_ID,
        profile_id=PROFILE_ID,
    )
    canonical = CanonicalCaseQueryV1.model_validate_json(
        bundle.case.canonical_query
    ).model_copy(update={"mode": InferenceMode.GAP_FILLING})
    values = bundle.case.model_dump(mode="python", exclude={"projection_digest"})
    values.update(
        canonical_query=canonical.canonical_text,
        canonical_query_digest=canonical.canonical_digest,
    )

    with pytest.raises(ValidationError, match="canonical query must match"):
        type(bundle.case)(**values)


def test_projection_never_contains_raw_or_internal_text() -> None:
    secret_query = (
        "RAW-QUERY-LEAK prompt-secret reasoning-secret provider-envelope-secret "
        "credential-secret error-body-secret"
    )
    source = case_source(query=secret_query)

    bundle = build_prepared_projection(
        source,
        generation_id=GENERATION_ID,
        profile_id=PROFILE_ID,
    )
    serialized = json.dumps(
        {
            "case": bundle.case.model_dump(mode="json"),
            "fragments": [item.model_dump(mode="json") for item in bundle.fragments],
        },
        sort_keys=True,
    )

    for forbidden in (
        secret_query,
        "RAW-QUERY-LEAK",
        "prompt-secret",
        "reasoning-secret",
        "provider-envelope-secret",
        "credential-secret",
        "error-body-secret",
        "private-provider-record",
        "Validated result fixture.",
    ):
        assert forbidden not in serialized
    assert "source_record_refs" not in serialized
    assert "analysis_detail" not in serialized
    assert len(bundle.case.windows) <= 60


def test_same_run_multi_waterbody_cases_keep_complete_identity_and_fragments() -> None:
    target = case_source()
    upstream = case_source(
        target_comid="200",
        session_id=UUID("81000000-0000-4000-8000-000000000030"),
        fragment_item_id=UUID("81000000-0000-4000-8000-000000000031"),
        result_item_id=UUID("81000000-0000-4000-8000-000000000032"),
        source_record_item_id=UUID("81000000-0000-4000-8000-000000000033"),
    )

    target_bundle = build_prepared_projection(
        target,
        generation_id=GENERATION_ID,
        profile_id=PROFILE_ID,
    )
    upstream_bundle = build_prepared_projection(
        upstream,
        generation_id=GENERATION_ID,
        profile_id=PROFILE_ID,
    )
    crossed = replace(upstream, fragments=target.fragments)
    with pytest.raises(
        RAGSourceClosureError,
        match="source_fragment_closure_incomplete",
    ):
        build_prepared_projection(
            crossed,
            generation_id=GENERATION_ID,
            profile_id=PROFILE_ID,
        )

    assert target_bundle.case.case_ref.run_id == upstream_bundle.case.case_ref.run_id
    assert target_bundle.case.case_ref.session_id != upstream_bundle.case.case_ref.session_id
    assert target_bundle.case.case_ref.result_item_id != upstream_bundle.case.case_ref.result_item_id
    assert target_bundle.case.case_ref.target_comid == "123"
    assert upstream_bundle.case.case_ref.target_comid == "200"
    assert target_bundle.case.case_ref.case_id != upstream_bundle.case.case_ref.case_id
    assert target_bundle.fragments[0].case_ref == target_bundle.case.case_ref
    assert upstream_bundle.fragments[0].case_ref == upstream_bundle.case.case_ref

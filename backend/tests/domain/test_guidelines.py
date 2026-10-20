import json
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.domain.guidelines import (
    CanonicalGuidelineDocumentV1, CanonicalGuidelineQueryV1, GuidelineCatalogSnapshot,
    GuidelineCitationV1, GuidelineDocumentSnapshot, GuidelineDraftV1, GuidelineEmbeddingBatch,
    GuidelineIdentity, GuidelineIndexGeneration, GuidelineModelViewV1, GuidelineMutation,
    GuidelineMutationReceipt, GuidelineOperation, GuidelineResultUsageBinding,
    GuidelineReview, GuidelineValidationError, GuidelineVersionV1, GuidelineUsageTarget,
    project_model_view,
)
from app.domain.guidelines.identity import bundled_guideline_id
from app.domain.hydrology.parameter_key import ParameterKey
from app.infrastructure.config import load_parameter_capability_registry
from tests.guideline_support import DO, PH, FIXTURE_TIME, content, identity, profile, query, source, version
from tests.infrastructure.embeddings.support import embedding_profile as case_profile, canonical_text as case_text
from app.application.transcript_memory.embedding_contract import (
    EmbeddingContractError, validate_canonical_embedding_texts,
)


@pytest.fixture(scope="module")
def registry():
    return load_parameter_capability_registry()


def test_incomplete_draft_and_partial_source_save_but_cannot_publish(registry):
    draft = GuidelineDraftV1(title="", body_markdown="", sources=({"source_id": "S1"},))
    assert GuidelineDraftV1.model_validate_json(draft.model_dump_json()) == draft
    assert draft.sources[0].title is None
    with pytest.raises(ValidationError):
        draft.publication_content(registry)
    populated = GuidelineDraftV1.model_validate(content().model_dump())
    published = populated.publication_content(registry)
    assert populated.content_digest == published.content_digest


@pytest.mark.parametrize("body", ["x" * 6000, "中" * 2000, "😀" * 1500], ids=["ascii", "cjk", "emoji"])
def test_publication_utf8_exact_limit(body, registry):
    assert len(content(body_markdown=body).body_markdown.encode("utf-8")) == 6000
    draft = GuidelineDraftV1.model_validate(content().model_dump() | {"body_markdown": body + "x"})
    with pytest.raises(ValidationError, match="6000"):
        draft.publication_content(registry)


@pytest.mark.parametrize("body", ["x" * 24000, "中" * 8000, "😀" * 6000], ids=["ascii", "cjk", "emoji"])
def test_draft_utf8_exact_limit(body):
    assert len(GuidelineDraftV1(body_markdown=body).body_markdown.encode("utf-8")) == 24000
    with pytest.raises(ValidationError, match="24000"):
        GuidelineDraftV1(body_markdown=body + "x")


@pytest.mark.parametrize("field,limit", [("title", 160), ("summary", 240)])
def test_text_character_limits_and_nonblank_publication(field, limit):
    assert len(getattr(content(**{field: "中" * limit}), field)) == limit
    for value in ("中" * (limit + 1), " \t\n"):
        with pytest.raises(ValidationError):
            content(**{field: value})


@pytest.mark.parametrize("field,count,width", [("scenario_tags", 12, 48), ("keywords", 24, 64)])
def test_metadata_limits(field, count, width):
    content(**{field: tuple(str(index) for index in range(count))})
    for values in (tuple(str(index) for index in range(count + 1)), ("x" * (width + 1),), (" ",)):
        with pytest.raises(ValidationError):
            content(**{field: values})


def test_sources_limits_and_conflicting_ids():
    content(sources=tuple(source(source_id=f"S{i}") for i in range(16)))
    with pytest.raises(ValidationError):
        content(sources=tuple(source(source_id=f"S{i}") for i in range(17)))
    with pytest.raises(ValidationError, match="conflicting"):
        content(sources=(source(), source(title="Different source")))
    with pytest.raises(ValidationError):
        source(source_id="S" * 65)
    with pytest.raises(ValidationError):
        source(url="https://source.invalid/" + "x" * 2048)


@pytest.mark.parametrize("url", [
    "ftp://source.invalid/a", "javascript:alert(1)", "data:text/plain,x", "not a URL",
    "https://user:secret@source.invalid/", "https://source.invalid/?api_key=secret",
    "https://source.invalid/with space", "https://source.invalid/\npath",
    "https://source.invalid:bad/a", "https:///missing-host", "https://source.invalid\\other",
    123,
    pytest.param("https://source.invalid/\ud800", id="invalid-utf8-url"),
])
def test_unsafe_source_urls_rejected_for_draft_and_publication(url):
    with pytest.raises(ValidationError):
        source(url=url)
    with pytest.raises(ValidationError):
        GuidelineDraftV1(sources=({"source_id": "S1", "url": url},))


def test_registered_parameters_are_checked_in_both_roles_and_query(registry):
    for field in ("parameter_keys", "related_parameter_keys"):
        values = content().model_dump() | {field: (ParameterKey(code_system="other", code="unknown"),)}
        with pytest.raises(ValueError, match="unregistered"):
            GuidelineDraftV1.model_validate(values).publication_content(registry)
    oxygen = GuidelineDraftV1.model_validate(content(parameter_keys=(DO,)).model_dump())
    assert oxygen.publication_content(registry).parameter_keys == (DO,)
    with pytest.raises(ValueError, match="unregistered"):
        query(parameter_keys=(ParameterKey(code_system="usgs", code="99999"),)).require_registered_parameters(registry)


def test_canonical_digest_normalizes_only_text_encoding_and_metadata():
    first = content(
        title="Cafe\u0301", body_markdown="If A:\r\n  retain B\rOtherwise C.",
        parameter_keys=(PH, DO, PH), related_parameter_keys=(DO, DO),
        scenario_tags=("b", "a", "b"), keywords=("e\u0301", "z", "é"),
        sources=(source(source_id="S2"), source(), source()),
    )
    second = content(
        title="Café", body_markdown="If A:\n  retain B\nOtherwise C.",
        parameter_keys=(DO, PH), related_parameter_keys=(DO,), scenario_tags=("a", "b"),
        keywords=("z", "é"), sources=(source(), source(source_id="S2")),
    )
    assert first == second
    assert first.content_digest == second.content_digest
    assert first.content_digest != content(**(first.model_dump() | {"body_markdown": "if A:\n  retain B\nOtherwise C."})).content_digest
    assert first.content_digest != content(**(first.model_dump() | {"body_markdown": "If A:\nretain B\nOtherwise C."})).content_digest
    assert first.content_digest != content(**(first.model_dump() | {"sources": (source(locator="different condition"),)})).content_digest


@pytest.mark.parametrize("text", ["\x00", "\ud800"])
def test_text_must_be_valid_utf8_without_control_characters(text):
    with pytest.raises(ValidationError):
        GuidelineDraftV1(body_markdown=text)


def test_source_kind_is_independent_from_origin_and_not_an_approval():
    original = content(source_kind="user_authored", sources=())
    assert project_model_view(original).source_kind.value == "user_authored"
    with pytest.raises(ValidationError, match="sources"):
        content(source_kind="reference_derived")
    derived = content(source_kind="reference_derived", sources=(source(),))
    assert derived.sources[0].source_id == "S1"
    with pytest.raises(ValidationError):
        GuidelineVersionV1(
            version_id=uuid4(), version_number=1, identity=identity(),
            content=derived, model_view=project_model_view(derived),
        )


@pytest.mark.parametrize("body", [
    "If [A](https://source.invalid/mechanism), consider B; otherwise retain C.",
    "If A, see <https://source.invalid/mechanism>; otherwise retain C.",
    "If A, see https://source.invalid/mechanism. Otherwise retain C.",
    'If A, see [condition](https://source.invalid/mechanism "Exception C"); retain B.',
    "If A, see [condition][s]. Otherwise C.\n\n[s]: https://source.invalid/mechanism",
    "满足条件 A 时参见https://source.invalid/mechanism；否则保留解释 C。",
])
def test_model_projection_replaces_only_urls_and_preserves_conditions(body):
    original = content(body_markdown=body, sources=(source(),))
    view = project_model_view(original)
    assert view.body == body.replace(source().url, "[source:S1]")
    assert "https://" not in view.model_dump_json()
    assert original.body_markdown == body
    assert original.sources[0].url == source().url
    assert view.content_digest == original.content_digest
    assert project_model_view(original) == view
    assert GuidelineModelViewV1.model_validate_json(view.model_dump_json()).view_digest == view.view_digest


def test_projection_handles_parentheses_longest_urls_and_stable_source_choice():
    refs = (source(source_id="B", url="https://source.invalid/a(x)"),
            source(source_id="A", url="https://source.invalid/a(x)"),
            source(source_id="C", url="https://source.invalid/a"))
    view = project_model_view(content(body_markdown="If [X](https://source.invalid/a(x)), retain Y.", sources=refs))
    assert view.body == "If [X]([source:A]), retain Y."


@pytest.mark.parametrize("field,text,code", [
    ("body_markdown", "If A, see https://unknown.invalid/otherwise.", "source_url_not_registered"),
    ("body_markdown", "If A, see https://source.invalid/mechanism/other.", "source_url_not_registered"),
    ("body_markdown", "If A, see ftp://source.invalid/otherwise.", "unsafe_model_text"),
    ("body_markdown", "<script>condition A</script>", "unsafe_model_text"),
    ("body_markdown", "[condition](javascript:alert(1))", "unsafe_model_text"),
    ("body_markdown", "[condition](data:text/html,example)", "unsafe_model_text"),
    ("body_markdown", "[condition](mailto:test@example.invalid)", "unsafe_model_text"),
    ("body_markdown", "api_key=private", "unsafe_model_text"),
    ("title", "Source https://source.invalid/mechanism", "unsafe_model_text"),
    ("summary", "authorization: bearer private", "unsafe_model_text"),
])
def test_unsafe_projection_is_a_field_error_without_silent_deletion(field, text, code, registry):
    draft = GuidelineDraftV1.model_validate(content(**{field: text}, sources=(source(),)).model_dump())
    with pytest.raises(GuidelineValidationError) as failure:
        draft.publication_content(registry)
    assert failure.value.field == field
    assert failure.value.code == code
    assert "private" not in str(failure.value)
    assert getattr(draft, field) == text


def test_projection_never_truncates_to_fit():
    url = "https://x.y"
    original = content(body_markdown=(url + " ") * 500, sources=(source(source_id="S" * 64, url=url),))
    with pytest.raises(GuidelineValidationError):
        project_model_view(original)
    assert original.body_markdown.endswith(url + " ")


def test_ordinary_scientific_data_labels_remain_readable():
    original = content(body_markdown="Observed data: retain condition A. Metadata: alternative B.")
    assert project_model_view(original).body == original.body_markdown


def test_family_identity_bundled_custom_and_local_override():
    base_id = bundled_guideline_id("fixture.mechanism")
    assert base_id == bundled_guideline_id("fixture.mechanism")
    base = GuidelineIdentity(guideline_id=base_id, family_root_id=base_id, origin="bundled", bundle_key="fixture.mechanism")
    local = GuidelineIdentity(
        guideline_id=uuid4(), family_root_id=base_id, origin="local_override",
        base_bundle_key=base.bundle_key, based_on_version_id=uuid4(), base_content_digest=content().content_digest,
    )
    assert local.family_root_id == base.family_root_id
    assert local.guideline_id != base.guideline_id
    with pytest.raises(ValidationError):
        GuidelineIdentity(**(local.model_dump() | {"base_content_digest": None}))
    with pytest.raises(ValidationError):
        GuidelineIdentity(**(base.model_dump() | {"guideline_id": uuid4()}))
    with pytest.raises(ValidationError):
        GuidelineIdentity(**(identity().model_dump() | {"family_root_id": uuid4()}))
    with pytest.raises(ValidationError):
        GuidelineDocumentSnapshot(identity=base, revision=0, activation_epoch=0, draft=GuidelineDraftV1())


def test_published_version_immutable_and_review_bound_to_document_and_content():
    approved = version()
    with pytest.raises(ValidationError, match="frozen"):
        approved.content.body_markdown = "changed"
    for replacement in (
        {"content": content(body_markdown="Changed condition")},
        {"identity": identity()},
        {"model_view": project_model_view(content(body_markdown="Different model view"))},
    ):
        with pytest.raises(ValidationError):
            GuidelineVersionV1.model_validate(approved.model_dump() | replacement)
    assert GuidelineVersionV1.model_validate_json(approved.model_dump_json()) == approved


@pytest.mark.parametrize("confirmation", [False, 1, "true", None])
def test_review_requires_explicit_confirmation(confirmation):
    with pytest.raises(ValidationError):
        GuidelineReview.model_validate(version().review.model_dump() | {"review_confirmed": confirmation})


def test_override_cannot_reuse_base_review_even_for_identical_content():
    base_id = bundled_guideline_id("fixture.topic")
    base = version(selected_identity=GuidelineIdentity(
        guideline_id=base_id, family_root_id=base_id, origin="bundled", bundle_key="fixture.topic",
    ))
    override = GuidelineIdentity(
        guideline_id=uuid4(), family_root_id=base_id, origin="local_override",
        base_bundle_key="fixture.topic", based_on_version_id=base.version_id,
        base_content_digest=base.content.content_digest,
    )
    with pytest.raises(ValidationError, match="review must bind"):
        GuidelineVersionV1.model_validate(base.model_dump() | {"identity": override})
    assert version(base.content, override).review.guideline_id == override.guideline_id


def test_embedding_inputs_have_separate_case_identity_and_no_source_urls(registry):
    selected = profile()
    doc = CanonicalGuidelineDocumentV1.from_content(content(sources=(source(),)), selected, registry)
    question = query(selected)
    assert selected.profile_id != case_profile().profile_id
    for value in (doc, question):
        value.require_profile(selected)
        assert "source.invalid" not in value.canonical_text
        assert value.canonical_digest.startswith("sha256:")
        with pytest.raises(EmbeddingContractError):
            validate_canonical_embedding_texts(case_profile(), (value.canonical_text,))
        with pytest.raises(ValueError):
            value.require_profile(profile(query_instructions="Changed role instruction."))
    with pytest.raises(ValidationError):
        CanonicalGuidelineQueryV1.model_validate_json(case_text())
    assert json.loads(doc.canonical_text)["kind"] == "guideline_document"
    assert CanonicalGuidelineDocumentV1.model_validate_json(doc.canonical_text) == doc
    assert CanonicalGuidelineQueryV1.model_validate_json(question.canonical_text) == question


@pytest.mark.parametrize("field,value", [
    ("provider", "other"), ("model", "new-model"), ("output_dimensions", 4),
    ("max_input_tokens", 1000), ("document_instructions", "A different document role."),
    ("query_instructions", "A different query role."),
])
def test_profile_identity_covers_all_configuration(field, value):
    assert profile(**{field: value}).profile_digest != profile().profile_digest


def test_profile_versions_and_forged_identity_rejected():
    for fields in ({"profile_id": case_profile().profile_id}, {"profile_digest": "forged"},
                   {"text_contract_version": 2}, {"query_template_version": 2},
                   {"document_template_version": 2}, {"distance_metric": "euclidean"}):
        with pytest.raises(ValidationError):
            profile(**fields)


def test_query_budget_metadata_dedup_and_no_arbitrary_truncation():
    first = query(parameter_keys=(PH, DO, PH), task_facts=("B", "A", "A"), question="Cafe\u0301\r\nQ")
    second = query(parameter_keys=(DO, PH), task_facts=("A", "B"), question="Café\nQ")
    assert first.canonical_digest == second.canonical_digest
    with pytest.raises(ValidationError, match="8192"):
        query(question="中" * 3000)
    with pytest.raises(ValidationError):
        query(question="https://untrusted.invalid/")


def test_query_exact_byte_boundary_and_document_metadata_canonicalization(registry):
    empty = query(question="")
    remaining = 8192 - len(empty.canonical_text.encode("utf-8"))
    exact = query(question="x" * remaining)
    assert len(exact.canonical_text.encode("utf-8")) == 8192
    with pytest.raises(ValidationError, match="8192"):
        query(question=exact.question + "x")
    document = CanonicalGuidelineDocumentV1.from_content(content(), profile(), registry)
    first = CanonicalGuidelineDocumentV1.model_validate(document.model_dump() | {
        "scenario_tags": ["b", "a", "b"], "parameter_names": ["B", "A", "A"],
    })
    second = CanonicalGuidelineDocumentV1.model_validate(document.model_dump() | {
        "scenario_tags": ["a", "b"], "parameter_names": ["A", "B"],
    })
    assert first.canonical_digest == second.canonical_digest


@pytest.mark.parametrize("kind", ["create", "save_draft", "local_edit", "publish", "set_enabled", "restore_bundled", "rebuild_index"])
def test_operation_receipts_do_not_change_with_mutable_work_state(kind):
    mutation = GuidelineMutation(idempotency_key=uuid4(), kind=kind, request_fingerprint=content().content_digest)
    status = 201 if kind in {"create", "local_edit"} else 202 if kind in {"publish", "rebuild_index"} else 200
    receipt = GuidelineMutationReceipt(
        mutation=mutation, status_code=status, operation_id=uuid4(),
        content_digest=content().content_digest,
    )
    pending = GuidelineOperation(receipt=receipt, state="pending")
    finished = GuidelineOperation(receipt=receipt, state="superseded")
    assert pending.receipt == finished.receipt
    assert pending.receipt.content_digest == content().content_digest
    with pytest.raises(ValidationError, match="frozen"):
        finished.receipt.status_code = 200


def test_mutation_key_rejects_non_v4_uuid():
    with pytest.raises(ValidationError):
        GuidelineMutation(
            idempotency_key=bundled_guideline_id("fixture.key"), kind="create",
            request_fingerprint=content().content_digest,
        )


@pytest.mark.parametrize("values", [(1.0, 2.0), (0.0, 0.0, 0.0), (float("nan"), 1.0, 1.0), (True, 1.0, 1.0)])
def test_embedding_output_rejects_wrong_dimensions_or_invalid_vectors(values):
    with pytest.raises(ValidationError):
        GuidelineEmbeddingBatch(profile=profile(), vectors=({"values": values},))


def test_catalog_generation_operation_and_receipt_snapshots():
    selected = profile()
    catalog = GuidelineCatalogSnapshot(revision=3, generation_id=uuid4(), profile=selected)
    assert GuidelineIndexGeneration(generation_id=catalog.generation_id, profile=selected, state="active", catalog_revision=3).profile == selected
    with pytest.raises(ValidationError):
        GuidelineCatalogSnapshot(revision=3, generation_id=uuid4())
    mutation = GuidelineMutation(idempotency_key=uuid4(), kind="publish", request_fingerprint=content().content_digest)
    receipt = GuidelineMutationReceipt(mutation=mutation, status_code=202, operation_id=uuid4(), document_id=uuid4(), accepted_revision=4)
    pending = GuidelineOperation(receipt=receipt, state="pending", activation_epoch=2)
    running = GuidelineOperation(receipt=receipt, state="running", attempts=1, lease_owner="fixture-worker", lease_until=FIXTURE_TIME + timedelta(seconds=30))
    assert running.receipt == pending.receipt
    with pytest.raises(ValidationError):
        GuidelineOperation(receipt=receipt, state="running")
    with pytest.raises(ValidationError):
        GuidelineOperation(receipt=receipt, state="failed", safe_error_code="raw provider details")
    with pytest.raises(ValidationError):
        GuidelineOperation(receipt=receipt, state="superseded", lease_owner="late-worker")


@pytest.mark.parametrize("fields", [
    {"kind": "numeric_task", "task_id": "numeric-1", "window_id": "window-1"},
    {"kind": "cell", "cell_id": "cell:usgs:00400:window-1", "window_id": "window-1"},
    {"kind": "window", "window_id": "window-1"},
    {"kind": "analysis_step", "step_index": 0},
    {"kind": "overall", "key": "overall"},
])
def test_usage_targets_preserve_server_identifiers(fields):
    target = GuidelineUsageTarget(**fields)
    binding = GuidelineResultUsageBinding(result_id="result-1", target=target, usage_item_ids=(uuid4(),))
    assert binding.target == target
    with pytest.raises(ValidationError):
        GuidelineUsageTarget(**(fields | {"task_id": "unrelated-task", "step_index": 1}))


@pytest.mark.parametrize("fields", [
    {"kind": "analysis_step", "step_index": -1}, {"kind": "analysis_step", "step_index": True},
    {"kind": "overall", "key": "anything"}, {"kind": "cell", "cell_id": "cell-1"},
])
def test_incomplete_or_invalid_usage_targets_rejected(fields):
    with pytest.raises(ValidationError):
        GuidelineUsageTarget(**fields)


def test_citation_is_a_durable_delivery_identity_not_an_alias():
    values = dict(
        delivery_ref={"run_id": uuid4(), "session_id": uuid4(), "item_id": uuid4()},
        guideline_id=uuid4(), family_root_id=uuid4(), version_id=uuid4(),
        origin="local_override", source_kind="user_authored", operation_id=uuid4(), phase="numeric_estimation",
    )
    citation = GuidelineCitationV1(**values)
    assert GuidelineCitationV1.model_validate_json(citation.model_dump_json()) == citation
    with pytest.raises(ValidationError):
        GuidelineCitationV1(**values, alias="G1")
    with pytest.raises(ValidationError):
        GuidelineCitationV1(**(values | {"delivery_ref": "G1"}))

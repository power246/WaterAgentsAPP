import asyncio
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.application.canonical_json import canonical_json_digest
from app.application.guidelines.transcript_models import GuidelineDeliveryV1
from app.application.reasoning.model_models import WaterbodyJsonSchema, WaterbodyModelMessage
from app.application.reasoning.transcript_models import WaterbodyModelRequestV1
from app.infrastructure.guidelines.bundled_source import PackageBundledGuidelineSource
from scripts.guideline_evaluation import reviewed_drafts, export_bundle, load_json, write_json, live_retrieval
from scripts.guideline_holdout import (EvidenceArtifact, HoldoutCase, PH_CODES, Prediction, SEALED_CHANNELS,
    audit_inputs, compact_dataset, load_cases, prepared_inputs, score_prediction)
from scripts.guideline_pair_review import RecordedArm, review_pair

ROOT = Path(__file__).resolve().parents[4]
DRAFTS = ROOT / 'docs/guidelines/ph_guideline_drafts.json'
DATASET = ROOT / 'backend/tests/fixtures/guidelines/ph_holdout_cases.json'


@pytest.fixture
def cases():
    return load_cases(load_json(DATASET))


def approval_for(path):
    packet, topics = reviewed_drafts(path)
    return {'packet_digest': packet, 'review_confirmed': True, 'reviewer': 'synthetic-fixture-reviewer',
        'review_note': 'Synthetic test approval only; not actual professional/human review.', 'reviewed_at': '2026-10-21T00:00:00Z',
        'topics': {t['bundle_key']: {'version': t['version'], 'content_digest': t['content_digest']} for t in topics}}


def reviewed_fixture(tmp_path):
    approval_path = tmp_path / 'approval.json'
    write_json(approval_path, approval_for(DRAFTS))
    export_bundle(DRAFTS, approval_path, tmp_path / 'bundle')
    return PackageBundledGuidelineSource(root=tmp_path / 'bundle').read_versions()


def test_four_real_drafts_are_valid_but_are_not_approved_production_resources():
    packet, topics = reviewed_drafts(DRAFTS)
    assert len(topics) == 4 and packet.startswith('sha256:')
    assert {t['bundle_key'] for t in topics} == {'ph.mixing-transport', 'ph.acid-buffering', 'ph.photosynthesis-respiration', 'ph.compare-causes'}
    for t in topics:
        c = t['content']
        assert c.source_kind.value == 'reference_derived' and c.sources
        assert len(c.body_markdown.encode()) <= 6000
        assert all(s.accessed_date is not None and s.locator for s in c.sources)
    assert load_json(DRAFTS)['status'] == 'awaiting_human_review'
    # An unreviewed seed packet alone never creates a production version.
    production = PackageBundledGuidelineSource().read_versions()
    assert all(v.review.content_digest == v.content.content_digest for v in production)


@pytest.mark.parametrize('change', ['missing_confirmation', 'body', 'source', 'version', 'review_digest'])
def test_export_requires_exact_confirmed_content_and_never_publishes_changed_drafts(tmp_path, change):
    drafts, approval = load_json(DRAFTS), approval_for(DRAFTS)
    if change == 'missing_confirmation': approval['review_confirmed'] = False
    elif change == 'body': drafts['drafts'][0]['content']['body_markdown'] += 'Changed boundary.'
    elif change == 'source': drafts['drafts'][0]['content']['sources'][0]['locator'] += ' changed'
    elif change == 'version': drafts['drafts'][0]['proposed_version'] = 2
    else: approval['topics']['ph.mixing-transport']['content_digest'] = 'sha256:' + '0' * 64
    draft_path, approval_path = tmp_path / 'drafts.json', tmp_path / 'approval.json'
    write_json(draft_path, drafts); write_json(approval_path, approval)
    with pytest.raises(ValueError): export_bundle(draft_path, approval_path, tmp_path / 'bundle')
    assert not (tmp_path / 'bundle/manifest.json').exists()


def test_packet_identity_is_stable_across_json_formatting_and_windows_line_endings(tmp_path):
    packet, _ = reviewed_drafts(DRAFTS)
    alternate = tmp_path / 'reformatted.json'
    alternate.write_text(json.dumps(load_json(DRAFTS), ensure_ascii=False, indent=4), encoding='utf-8', newline='\r\n')
    assert reviewed_drafts(alternate)[0] == packet
    versions = reviewed_fixture(tmp_path)
    assert len(versions) == 4
    assert all(v.review.review_confirmed and v.review.content_digest == v.content.content_digest for v in versions)
    approval_path = tmp_path / 'approval.json'
    before = (tmp_path / 'bundle/manifest.json').read_bytes()
    export_bundle(DRAFTS, approval_path, tmp_path / 'bundle')
    assert (tmp_path / 'bundle/manifest.json').read_bytes() == before


@pytest.mark.parametrize('key,number', [('ph/../../escape',1), ('ph.valid',True), ('ph.valid',0)])
def test_review_packet_rejects_unsafe_resource_keys_and_invalid_versions_before_writing(tmp_path, key, number):
    body = load_json(DRAFTS)
    body['drafts'][0].update(bundle_key=key, proposed_version=number)
    write_json(tmp_path / 'drafts.json', body)
    with pytest.raises(ValueError): reviewed_drafts(tmp_path / 'drafts.json')


def test_public_snapshot_contains_three_real_cases_and_distinguishes_missing_information(cases):
    assert len(cases) == 3
    assert [len(c.heldout) for c in cases] == [23, 24, 6]
    assert [c.truth for c in cases] == [Decimal('8.230435'), Decimal('8.791667'), Decimal('8.166667')]
    assert all(c.topology_source.startswith('https://api.water.usgs.gov/nldi/') and c.source_urls for c in cases)
    assert all(r.provenance.provider_record_id == r.record_id for c in cases for r in c.observations)
    assert any(r.parameter.code == '00400' for r in cases[0].visible if r.location.station_id in cases[0].upstream_stations)
    assert not any(r.parameter.code in PH_CODES for r in cases[-1].visible)


def test_target_raw_records_are_removed_and_compaction_preserves_identity_and_input_hash(cases):
    roundtrip = load_cases(compact_dataset(cases))
    for original, restored in zip(cases, roundtrip):
        assert original.truth == restored.truth
        assert prepared_inputs(original)['input_digest'] == prepared_inputs(restored)['input_digest']
        assert not {r.record_id for r in original.heldout} & {r.record_id for r in original.visible}
        assert any(r.location.station_id == original.target_station for r in original.visible)


@pytest.mark.parametrize('kind', ['raw', 'aggregate', 'other_waterbody', 'history_rag', 'cache', 'context', 'guideline_example'])
def test_target_duplicates_are_rejected_in_every_visible_evidence_channel(cases, kind):
    case = cases[0]
    leak = EvidenceArtifact(artifact_id='copied-into-another-waterbody', kind=kind, observation_refs=(case.heldout[0].record_id,))
    with pytest.raises(ValueError, match='heldout|unknown'):
        audit_inputs(case, (leak,), sealed_channels=SEALED_CHANNELS)


def test_unknown_derivations_cycles_free_text_aggregates_and_unsealed_sources_are_rejected(cases):
    case = cases[0]
    with pytest.raises(ValueError, match='unknown'):
        audit_inputs(case, (EvidenceArtifact(artifact_id='aggregate', kind='aggregate', input_refs=('unknown-source',)),), sealed_channels=SEALED_CHANNELS)
    with pytest.raises(ValueError, match='cyclic'):
        audit_inputs(case, (EvidenceArtifact(artifact_id='cycle', kind='aggregate', input_refs=('cycle',)),), sealed_channels=SEALED_CHANNELS)
    with pytest.raises(ValueError, match='free-text'):
        audit_inputs(case, (EvidenceArtifact(artifact_id='rag-summary', kind='context', text=f'Target mean pH was {case.truth}'),), sealed_channels=SEALED_CHANNELS)
    with pytest.raises(ValueError, match='question'):
        audit_inputs(case, (), sealed_channels=SEALED_CHANNELS, question=f'Can you reproduce pH {case.truth}?')
    with pytest.raises(ValueError, match='sealed'):
        audit_inputs(case, (), sealed_channels={'history_rag'})


def test_metrics_separate_points_ranges_failures_and_noncomparable_windows(cases):
    case = cases[0]
    prediction = Prediction(status='point', truth_kind=case.truth_kind, window=case.window, value=case.truth + Decimal('.25'), lower=case.truth - 1, upper=case.truth + 1)
    score = score_prediction(case, prediction, has_eligible_same_indicator_anchor=True)
    assert score['absolute_ph_error'] == '0.250000' and score['range_width'] == '2.000000' and score['range_covers_truth'] is True
    score = score_prediction(case, prediction.model_copy(update={'status': 'range', 'value': None}), has_eligible_same_indicator_anchor=False)
    assert score['absolute_ph_error'] is None and score['range_covers_truth'] is True
    score = score_prediction(case, Prediction(status='failed', truth_kind=case.truth_kind, window=case.window), has_eligible_same_indicator_anchor=False)
    assert score['absolute_ph_error'] is None and score['range_width'] is None
    mismatch = prediction.model_copy(update={'truth_kind': 'instant', 'instant': case.window.start})
    assert score_prediction(case, mismatch, has_eligible_same_indicator_anchor=True)['status'] == 'not_comparable'
    with pytest.raises(ValueError, match='anchor'):
        score_prediction(cases[-1], Prediction(status='point', truth_kind=case.truth_kind, window=cases[-1].window, value='8.2'), has_eligible_same_indicator_anchor=False)
    with pytest.raises(ValueError, match='anchor'):
        score_prediction(cases[-1], Prediction(status='point', truth_kind=case.truth_kind, window=cases[-1].window, value='8.2'), has_eligible_same_indicator_anchor=True)
    with pytest.raises(ValueError, match='boolean'):
        score_prediction(case, prediction, has_eligible_same_indicator_anchor='false')


def test_a_single_observed_instant_never_becomes_a_window_mean(cases):
    original = cases[0]
    case = original.model_copy(update={'truth_kind': 'instant', 'instant': original.heldout[0].observed_time,
        'observations': (original.heldout[0], *original.visible)})
    case = HoldoutCase.model_validate(case.model_dump())
    assert case.truth == Decimal(original.heldout[0].value)
    prediction = Prediction(status='range', truth_kind='available_readings_mean', window=case.window, lower='7', upper='9')
    assert score_prediction(case, prediction, has_eligible_same_indicator_anchor=False)['status'] == 'not_comparable'


def request(run_id, block, extra=None, operation_id=None):
    values = dict(schema_version=1, operation_id=str(operation_id or uuid4()), phase='reasoning_batch', model={'provider':'openai','model':'fixture'},
        scope_digest='scope', prompt_policy_digest='policy', evidence_bundle_ref={'run_id':str(run_id),'item_id':str(uuid4())},
        batch_id='fixture', system_prompt='Synthetic test only', tool_descriptors=[], deadline='2026-10-21T00:01:00Z',
        messages=[WaterbodyModelMessage(message_id='m1',role='user',text=json.dumps({'advisory_knowledge':block, 'context':extra})).model_dump(mode='json')],
        response_schema=WaterbodyJsonSchema(name='fixture',schema_text='{"type":"object"}').model_dump(mode='json'))
    return WaterbodyModelRequestV1.model_validate({**values,'request_digest':canonical_json_digest(values)})


def pair(case, versions):
    inputs = prepared_inputs(case)
    control_id, guided_id, session_id = uuid4(), uuid4(), uuid4()
    version = versions[0]
    block = [{'alias':'G1_fixture', **version.model_view.model_dump(mode='json')}]
    call = request(guided_id, block)
    delivery = GuidelineDeliveryV1(run_id=guided_id,session_id=session_id,operation_id=call.operation_id,phase='reasoning_batch',
        run_context_ref={'run_id':guided_id,'item_id':uuid4()},retrieval_ref={'run_id':guided_id,'item_id':uuid4()},
        request_ref={'run_id':guided_id,'item_id':uuid4()},selection_status='retrieved',advisory_digest=version.model_view.view_digest,
        entries=[{'alias':'G1_fixture','version':version}])
    common = dict(session_id=session_id,provider='openai',model='fixture',model_version='synthetic-v1',
        configuration={'memory_mode':'stateless','evidence_mode':'frozen','max_model_calls':8,'time_resolution':'6h'},
        input_digest=inputs['input_digest'],artifacts=inputs['artifacts'],sealed_channels=inputs['sealed_channels'],usages=[],
        prediction=Prediction(status='range',truth_kind=case.truth_kind,window=case.window,lower='7',upper='9'),
        eligible_anchor_refs=[],explanation='Synthetic explanation; no real provider dispatch.',explanation_review=None)
    return RecordedArm(**common,run_id=control_id,requests=[request(control_id,[])],deliveries={}), RecordedArm(**common,run_id=guided_id,requests=[call],deliveries={uuid4():delivery})


def test_saved_pair_review_distinguishes_provided_from_adopted_without_assuming_improvement(tmp_path, cases):
    versions = reviewed_fixture(tmp_path)
    control, guided = pair(cases[0], versions)
    report = review_pair(cases[0], control, guided, versions)
    assert not report['control']['provided'] and len(report['guided']['provided']) == 1
    assert report['guided']['explicitly_used'] == []
    assert report['control']['absolute_ph_error'] is None


@pytest.mark.parametrize('corruption', ['control_provided', 'budget', 'snapshot', 'unapproved_version', 'payload_without_delivery', 'scientific_context'])
def test_pair_review_rejects_false_controls_config_drift_and_uncounted_delivery(tmp_path, cases, corruption):
    versions = reviewed_fixture(tmp_path)
    control, guided = pair(cases[0], versions)
    if corruption == 'control_provided':
        control, guided = guided, control
    elif corruption == 'budget':
        guided = guided.model_copy(update={'configuration':{**guided.configuration,'max_model_calls':9}})
    elif corruption == 'snapshot':
        guided = guided.model_copy(update={'input_digest':'different'})
    elif corruption == 'unapproved_version':
        versions = ()
    elif corruption == 'payload_without_delivery':
        guided = guided.model_copy(update={'deliveries':{}})
    else:
        control = control.model_copy(update={'requests':(request(control.run_id, [], extra='different scientific evidence'),)})
    with pytest.raises(ValueError): review_pair(cases[0], control, guided, versions)


def test_live_evaluation_refuses_empty_unreviewed_resources_before_any_provider_dispatch(tmp_path):
    from app.infrastructure.config import AppSettings
    write_json(tmp_path / 'manifest.json', {'schema_version':1,'documents':[]})
    with pytest.raises(ValueError, match='reviewed'):
        asyncio.run(live_retrieval(AppSettings(_env_file=None), tmp_path, ROOT / 'backend/tests/fixtures/guidelines/ph_retrieval_cases.json'))


def test_hidden_target_aggregate_in_actual_model_message_is_rejected_even_with_a_clean_declared_snapshot(tmp_path, cases):
    versions = reviewed_fixture(tmp_path)
    control, guided = pair(cases[0], versions)
    leaked = request(control.run_id, [], extra={'historical_mean': str(cases[0].truth)})
    control = control.model_copy(update={'requests': (leaked,)})
    delivery = next(iter(guided.deliveries.values()))
    block = [{'alias': e.alias, **e.version.model_view.model_dump(mode='json')} for e in delivery.entries]
    leaked_guided = request(guided.run_id, block, extra={'historical_mean': str(cases[0].truth)}, operation_id=delivery.operation_id)
    guided = guided.model_copy(update={'requests': (leaked_guided,)})
    with pytest.raises(ValueError, match='heldout'):
        review_pair(cases[0], control, guided, versions)


@pytest.mark.parametrize('fail', [False, True])
def test_retrieval_runner_prepares_real_postgres_generation_and_reports_all_misses_and_failures(monkeypatch, tmp_path, fail):
    from pydantic import SecretStr
    from app.ports import PortAuthenticationError
    from scripts.test_database import temporary_migrated_schema
    from tests.application.guidelines.test_embedding import catalog
    from tests.application.guidelines.test_worker import Embedding
    import scripts.guideline_evaluation as evaluation
    reviewed_fixture(tmp_path)
    class Port(Embedding):
        async def embed(self, *args):
            if fail:
                raise PortAuthenticationError('synthetic private key must never enter the report')
            return await super().embed(*args)
    port = Port()
    monkeypatch.setattr(evaluation, 'OpenAIGuidelineEmbeddingAdapter', lambda *args, **kwargs: port)
    with temporary_migrated_schema() as database:
        selected = database.model_copy(update={'model_catalog': catalog(max_input_tokens=8192).capabilities,
            'openai_api_key': SecretStr('synthetic-fixture-key'), 'openai_base_url': 'https://fixture.invalid/v1'})
        result = asyncio.run(live_retrieval(selected, tmp_path / 'bundle', ROOT / 'backend/tests/fixtures/guidelines/ph_retrieval_cases.json'))
    if fail:
        assert result['status'] == 'failed' and result['reason'] == 'guideline_embedding_credentials_unavailable'
        assert 0 < result['provider_requests'] <= 2
        assert 'synthetic private key' not in json.dumps(result)
    else:
        assert result['status'] == 'completed' and len(result['cases']) == 12
        assert len(result['versions']) == 4 and result['generation_id']
        negative = next(c for c in result['cases'] if c['id'] == 'unrelated_precipitation')
        assert negative['metadata_candidates'] == 0 and negative['selected'] == []
        # Deliberately equal synthetic vectors expose, rather than hide, irrelevant matches.
        assert 'irrelevant_office' in result['negative_false_positive_ids']
        assert result['provider_requests'] == 15

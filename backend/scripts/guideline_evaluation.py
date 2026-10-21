"""M11 review, live retrieval, read-only data collection, holdout audit and score CLI.

Run from backend with ``python -m scripts.guideline_evaluation --help``.
No default command calls a provider or mutates a database. Live retrieval uses a
temporary schema in WATERAGENTS_TEST_DB_NAME and requires reviewed bundle input.
"""
import argparse
import asyncio
import json
import re
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

import httpx
from pydantic import TypeAdapter

from app.application.guidelines.embedding import desired_guideline_profile, guideline_embedding_failure
from app.application.guidelines.worker import GuidelinePreparationWorker
from app.domain import ModelCatalogSnapshot, ParameterKey
from app.domain.guidelines import CanonicalGuidelineQueryV1, GuidelineContentV1, GuidelineIdentity, GuidelineReview, project_model_view
from app.domain.guidelines.canonical import VersionNumber, digest
from app.domain.guidelines.identity import bundled_guideline_id, bundled_guideline_version_id
from app.domain.evidence import WaterObservationRecord, DischargeObservationRecord
from app.domain.evidence.provenance import Provenance
from app.infrastructure.adapters.openai_guideline_embeddings import OpenAIGuidelineEmbeddingAdapter
from app.infrastructure.config import AppSettings, load_parameter_capability_registry
from app.infrastructure.credentials.embedding import embedding_api_key, embedding_base_url
from app.infrastructure.guidelines.bundled_source import PackageBundledGuidelineSource
from app.infrastructure.http import ProviderHttpClient
from app.infrastructure.persistence.guideline_repository import PostgresGuidelineRepository
from app.infrastructure.runtime.event_loop import configure_asyncio_event_loop
from app.ports import PortAuthenticationError
from scripts.guideline_holdout import HoldoutCase, compact_dataset, load_cases, prepared_inputs, score_prediction, Prediction
from scripts.guideline_pair_review import RecordedArm, review_pair
from scripts.test_database import temporary_migrated_schema


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, body):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(body, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def reviewed_drafts(path):
    body = load_json(path)
    if body.get('schema_version') != 1 or not body.get('drafts'):
        raise ValueError('invalid draft packet')
    registry, topics, seen = load_parameter_capability_registry(), [], set()
    for entry in body['drafts']:
        key, number = entry['bundle_key'], TypeAdapter(VersionNumber).validate_python(entry['proposed_version'])
        root_id = bundled_guideline_id(key)
        GuidelineIdentity(guideline_id=root_id, family_root_id=root_id, origin='bundled', bundle_key=key)
        identifier = bundled_guideline_version_id(key, number)
        if (key, number) in seen:
            raise ValueError('duplicate proposed bundle key/version')
        seen.add((key, number))
        content = GuidelineContentV1.model_validate(entry['content'])
        content.require_registered_parameters(registry)
        view = project_model_view(content)
        topics.append({'bundle_key': key, 'version': number, 'version_id': str(identifier),
            'content_digest': content.content_digest, 'model_view_digest': view.view_digest, 'content': content})
    packet = [{'bundle_key': t['bundle_key'], 'version': t['version'], 'content': t['content'].model_dump(mode='json')} for t in topics]
    return digest({'schema_version': 1, 'topics': packet}), topics


def export_bundle(drafts_path, approval_path, output):
    packet_digest, topics = reviewed_drafts(drafts_path)
    approval = load_json(approval_path)
    if approval.get('review_confirmed') is not True or approval.get('packet_digest') != packet_digest:
        raise ValueError('explicit approval of this exact draft packet is required')
    approved = approval.get('topics', {})
    expected = {t['bundle_key']: {'version': t['version'], 'content_digest': t['content_digest']} for t in topics}
    if approved != expected:
        raise ValueError('approval topic versions/digests do not match')
    target = Path(output)
    manifest_path = target / 'manifest.json'
    if manifest_path.exists():
        PackageBundledGuidelineSource(root=target).read_versions()
    manifest = load_json(manifest_path) if manifest_path.exists() else {'schema_version': 1, 'documents': []}
    bodies, entries = [], list(manifest['documents'])
    for topic in topics:
        content = topic['content']
        review = GuidelineReview(guideline_id=bundled_guideline_id(topic['bundle_key']),
            content_digest=content.content_digest, review_confirmed=True, reviewer=approval['reviewer'],
            review_note=approval['review_note'], reviewed_at=approval['reviewed_at'])
        resource = f"topics/{topic['bundle_key']}/v{topic['version']}.md"
        entry = {'bundle_key': topic['bundle_key'], 'version': topic['version'], 'body_resource': resource,
            'body_digest': 'sha256:' + sha256(content.body_markdown.encode()).hexdigest(),
            'content_digest': content.content_digest, 'model_view_digest': topic['model_view_digest'],
            'metadata': {**content.model_dump(mode='json'), 'body_markdown': None}, 'review': review.model_dump(mode='json')}
        old = next((e for e in entries if (e['bundle_key'], e['version']) == (entry['bundle_key'], entry['version'])), None)
        if old is not None and old != entry:
            raise ValueError('existing immutable bundle version cannot be overwritten')
        if old is None:
            entries.append(entry)
        bodies.append((target / resource, content.body_markdown))
    for path, body in bodies:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding='utf-8', newline='')
    write_json(manifest_path, {'schema_version': 1, 'documents': entries})
    return {'status': 'exported', 'packet_digest': packet_digest,
            'versions': [str(v.version_id) for v in PackageBundledGuidelineSource(root=target).read_versions()]}


async def live_retrieval(settings, bundle_path, cases_path):
    versions = PackageBundledGuidelineSource(root=Path(bundle_path)).read_versions()
    if not versions:
        raise ValueError('retrieval evaluation requires a nonempty reviewed bundle')
    profile = desired_guideline_profile(ModelCatalogSnapshot(capabilities=settings.model_catalog), settings.guideline_policy)
    report = {'status': 'not_run', 'profile': profile.model_dump(mode='json'), 'cutoff': settings.guideline_policy.semantic_min_similarity,
        'versions': [str(v.version_id) for v in versions], 'cases': [], 'provider_requests': 0}
    try:
        key = embedding_api_key(settings).get_secret_value()
    except PortAuthenticationError:
        return {**report, 'reason': 'guideline_embedding_credentials_unavailable'}
    repository = PostgresGuidelineRepository(settings)
    try:
        async with ProviderHttpClient(timeout_seconds=20, max_response_bytes=4000000, retry_delay_seconds=0, attempts=1) as http:
            port = OpenAIGuidelineEmbeddingAdapter(http, api_key=key,
                base_url=embedding_base_url(settings), timeout_seconds=20)
            query_port = OpenAIGuidelineEmbeddingAdapter(http, api_key=key,
                base_url=embedding_base_url(settings), timeout_seconds=settings.guideline_policy.online_timeout_seconds)
            class Counted:
                def __init__(self, selected):
                    self.port = selected
                async def embed(self, *args):
                    report['provider_requests'] += 1
                    return await self.port.embed(*args)
            counted, query_counted = Counted(port), Counted(query_port)
            async def factory():
                return counted
            worker = GuidelinePreparationWorker(repository, load_parameter_capability_registry(), profile, settings.guideline_policy, factory)
            for version in versions:
                await repository.import_bundled_version(version)
            for _ in range(24):
                await worker.run_once()
                if worker.last_error_code:
                    return {**report, 'status': 'failed', 'reason': worker.last_error_code}
                catalog = await repository.read_catalog()
                documents = [await repository.read_document_details(v.identity.guideline_id) for v in versions]
                if catalog.profile == profile and all(d is not None and d.state == 'available' for d in documents):
                    break
            else:
                return {**report, 'status': 'failed', 'reason': 'guideline_preparation_incomplete'}
            for case in load_json(cases_path):
                keys = tuple(ParameterKey.model_validate(k) for k in case['parameter_keys'])
                query = CanonicalGuidelineQueryV1(profile_id=profile.profile_id, instructions=profile.query_instructions,
                    parameter_keys=keys, mode=case.get('mode', 'prediction'), question=case['question'],
                    scenario_tags=tuple(case.get('scenario_tags', ())), task_facts=tuple(case.get('task_facts', ())))
                query.require_registered_parameters(load_parameter_capability_registry())
                queries = (query,)
                if case.get('aspects'):
                    from app.application.guidelines.search_skill import GuidelineSearchArguments
                    arguments = GuidelineSearchArguments(query=case['question'], aspects=case['aspects'])
                    queries = tuple(CanonicalGuidelineQueryV1.model_validate(
                        query.model_dump() | {'question': aspect}) for aspect in arguments.aspects)
                library, candidates = await repository.retrieval_counts(catalog, keys)
                ranked = ()
                if candidates:
                    from app.application.guidelines.embedding import distinct_query_vectors
                    result = await query_counted.embed(profile, queries, asyncio.get_running_loop().time() + settings.guideline_policy.online_timeout_seconds, None)
                    vectors = distinct_query_vectors(result.vectors)
                    ranked = await repository.rank_guidelines(catalog, keys,
                        vectors[0] if len(vectors) == 1 else vectors, cutoff=0, limit=3)
                selected = [m.version.identity.bundle_key for m in ranked if m.similarity >= settings.guideline_policy.semantic_min_similarity]
                expected = set(case['expected'])
                report['cases'].append({'id': case['id'], 'question': case['question'], 'expected': sorted(expected),
                    'library_count': library, 'metadata_candidates': candidates, 'query_digest': query.canonical_digest,
                    'embedding_inputs': len(queries),
                    'ranking': [{'key': m.version.identity.bundle_key, 'similarity': m.similarity} for m in ranked],
                    'selected': selected, 'missed': sorted(expected - set(selected)), 'unexpected': sorted(set(selected) - expected)})
            report['status'] = 'completed'
            report['missed_query_ids'] = [c['id'] for c in report['cases'] if c['missed']]
            report['negative_false_positive_ids'] = [c['id'] for c in report['cases'] if not c['expected'] and c['selected']]
            report['annotation_limit'] = 'Extra positive-query hits are unlabelled, not automatically irrelevant; review their conditions.'
            report['catalog_revision'] = catalog.revision
            report['generation_id'] = str(catalog.generation_id)
            return report
    except Exception as error:
        return {**report, 'status': 'failed', 'reason': guideline_embedding_failure(error)[0]}
    finally:
        await repository.aclose()


async def discover_cases(plan_path):
    """Bounded public OGC/NLDI GETs; snapshot output is input data, not model results."""
    report, fetched = {'schema_version': 1, 'cases': []}, {}
    async with httpx.AsyncClient(timeout=25, follow_redirects=False, trust_env=False) as client:
        async def fetch(url, params=None):
            request = client.build_request('GET', url, params=params)
            key = str(request.url)
            if key not in fetched:
                response = await client.send(request)
                response.raise_for_status()
                if len(response.content) > 5000000:
                    raise ValueError('survey response exceeds limit')
                body = response.json()
                if any(link.get('rel') == 'next' for link in body.get('links', [])):
                    raise ValueError('survey collection incomplete; reduce the explicit window')
                fetched[key] = body
            return key, fetched[key]
        plans = load_json(plan_path)
        if not 1 <= len(plans) <= 5:
            raise ValueError('survey requires one to five explicit cases')
        for plan in plans:
            from app.domain.time.time_range import TimeRange
            window = TimeRange.model_validate(plan['window'])
            if not 0 < (window.end - window.start).total_seconds() <= 86400:
                raise ValueError('survey window must be at most one day')
            stations = [plan['target_station'], *plan['upstream_stations']]
            if len(stations) > 4 or any(not re.fullmatch(r'USGS-\d{8,15}', s) for s in stations):
                raise ValueError('survey requires at most four explicit USGS station IDs')
            if type(plan['topology_distance_km']) not in {int, float} or not 0 < plan['topology_distance_km'] <= 100:
                raise ValueError('survey topology distance must be at most 100 km')
            topology_url = f"https://api.water.usgs.gov/nldi/linked-data/nwissite/{stations[0]}/navigation/UT/nwissite"
            topology_url, topology = await fetch(topology_url, {'distance': plan['topology_distance_km'], 'f': 'json'})
            upstream = {f['properties']['identifier'] for f in topology['features']}
            if not set(plan['upstream_stations']) <= upstream:
                raise ValueError('requested upstream relationship was not verified by NLDI')
            records, sources, observed_at = [], [], datetime.now(timezone.utc)
            for station in stations:
                url, body = await fetch('https://api.waterdata.usgs.gov/ogcapi/v1/collections/continuous/items', {
                    'f': 'json', 'monitoring_location_id': station, 'parameter_code': '00400,00060,00300,00010,00095',
                    'datetime': f'{window.start.isoformat()}/{window.end.isoformat()}', 'limit': 2000})
                sources.append(url)
                for feature in body['features']:
                    p = feature['properties']
                    time = datetime.fromisoformat(p['time'])
                    if p['value'] is None or not window.contains(time):
                        continue
                    if p['monitoring_location_id'] != station:
                        raise ValueError('survey returned an unexpected site')
                    record_type = DischargeObservationRecord if p['parameter_code'] == '00060' else WaterObservationRecord
                    qualifier = p.get('qualifier')
                    if qualifier is None:
                        qualifiers = ()
                    elif isinstance(qualifier, str):
                        qualifiers = (qualifier.strip(),) if qualifier.strip() else ()
                    elif isinstance(qualifier, list) and all(isinstance(q, str) and q.strip() for q in qualifier):
                        qualifiers = tuple(q.strip() for q in qualifier)
                    else:
                        raise ValueError('invalid USGS qualifier shape')
                    records.append(record_type(provider='usgs_waterdata', channel='continuous', source_uri=url,
                        retrieved_at=observed_at, observed_time=time,
                        location={'point': None, 'comid': None, 'station_id': station, 'distance_to_target_km': None},
                        provider_quality={'approval': p.get('approval_status'), 'qualifiers': qualifiers},
                        raw_unit=p['unit_of_measure'], provenance=Provenance(provider_series_id=p['time_series_id'],
                            provider_record_id='usgs-wdfn:v1:continuous:' + feature['id'], request_identity=digest({'url': url})),
                        parameter=ParameterKey(code_system='usgs', code=p['parameter_code']), value=p['value'],
                        medium='surface water', method=p.get('method_category'), statistic=p['statistic_id']))
            case = HoldoutCase(case_id=plan['case_id'], target_station=stations[0], upstream_stations=tuple(stations[1:]),
                topology_source=topology_url, topology_digest=digest(topology), window=window, truth_kind='available_readings_mean',
                expected_cadence_seconds=plan['expected_cadence_seconds'], collections_complete=True,
                retrieved_at=observed_at, source_urls=tuple(sources), observations=tuple(records), limitations=tuple(plan['limitations']))
            # Verify the reference statistic using the existing scientific aggregation policy.
            case.truth
            report['cases'].append(case.model_dump(mode='json'))
    return compact_dataset([HoldoutCase.model_validate(case) for case in report['cases']])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ['drafts', 'bundle', 'retrieval', 'discover', 'audit', 'score', 'verify-pairs']:
        command = commands.add_parser(name)
        command.add_argument('--output', required=True)
        if name in {'drafts', 'bundle'}:
            command.add_argument('--drafts', required=True)
        if name == 'bundle':
            command.add_argument('--approval', required=True)
            command.add_argument('--bundle-root', required=True)
        if name == 'retrieval':
            command.add_argument('--bundle-root', required=True)
            command.add_argument('--cases', required=True)
        if name == 'discover':
            command.add_argument('--plan', required=True)
        if name in {'audit', 'score', 'verify-pairs'}:
            command.add_argument('--dataset', required=True)
        if name == 'verify-pairs':
            command.add_argument('--runs', required=True)
            command.add_argument('--bundle-root', required=True)
        if name == 'score':
            command.add_argument('--predictions', required=True)
    args = parser.parse_args(argv)
    if args.command == 'drafts':
        packet_digest, topics = reviewed_drafts(args.drafts)
        result = {'status': 'awaiting_human_review', 'packet_digest': packet_digest,
                  'topics': [{k: v for k, v in topic.items() if k != 'content'} for topic in topics]}
    elif args.command == 'bundle':
        result = export_bundle(args.drafts, args.approval, args.bundle_root)
    elif args.command == 'retrieval':
        # Helper refuses the business DB name and creates/drops only its task schema.
        with temporary_migrated_schema() as database:
            settings = AppSettings().model_copy(update={'db_name': database.db_name})
            result = asyncio.run(live_retrieval(settings, args.bundle_root, args.cases))
    elif args.command == 'discover':
        result = asyncio.run(discover_cases(args.plan))
    else:
        cases = load_cases(load_json(args.dataset))
        if args.command == 'audit':
            result = {'status': 'audited', 'inputs': [prepared_inputs(c) for c in cases]}
        elif args.command == 'verify-pairs':
            versions = PackageBundledGuidelineSource(root=Path(args.bundle_root)).read_versions()
            if not versions:
                raise ValueError('pair review requires reviewed content')
            by_id = {c.case_id: c for c in cases}
            results = []
            for row in load_json(args.runs):
                try:
                    results.append(review_pair(by_id[row['case_id']], RecordedArm.model_validate(row['control']),
                        RecordedArm.model_validate(row['guided']), versions))
                except (ValueError, KeyError):
                    results.append({'case_id': row.get('case_id'), 'status': 'rejected', 'reason': 'paired_contract_invalid'})
            rejected = sum(row.get('status') == 'rejected' for row in results)
            result = {'status': 'needs_review' if rejected else 'audited', 'pairs': results, 'rejected_count': rejected,
                      'note': 'No model dispatch; saved-pair verification only. All rejected pairs retained.'}
        else:
            by_id, scored = {c.case_id: c for c in cases}, []
            for row in load_json(args.predictions):
                try:
                    case = by_id[row['case_id']]
                    scored.append({'case_id': case.case_id, **score_prediction(case, Prediction.model_validate(row['prediction']),
                        has_eligible_same_indicator_anchor=row['has_eligible_same_indicator_anchor'])})
                except (ValueError, KeyError):
                    scored.append({'case_id': row.get('case_id'), 'status': 'rejected', 'reason': 'prediction_contract_invalid'})
            rejected = sum(row['status'] == 'rejected' for row in scored)
            result = {'status': 'needs_review' if rejected else 'scored', 'results': scored, 'rejected_count': rejected,
                      'note': 'Metrics only; this command does not dispatch or certify paired model runs.'}
    write_json(args.output, result)
    print(json.dumps({'status': result['status'] if 'status' in result else 'collected', 'output': str(args.output)}, ensure_ascii=False))
    return 1 if result.get('status') in {'failed', 'not_run', 'needs_review'} else 0


if __name__ == '__main__':
    configure_asyncio_event_loop()
    try:
        exit_code = main()
    except Exception:
        # Invalid drafts/provider/config exceptions can contain input text; the CLI prints no raw diagnostics.
        print(json.dumps({'status': 'failed', 'reason': 'invalid_input_or_unavailable_service'}))
        exit_code = 1
    raise SystemExit(exit_code)

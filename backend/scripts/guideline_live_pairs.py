"""Explicit live-provider holdout runs in a temporary PostgreSQL test schema.

Query inputs and the first production topology are frozen per case. Observation
ports can return only the visible public fixture; all other acquisition fails
closed. The normal production scheduler, numerical gates, model adapters,
transcripts and HTTP/history APIs execute unchanged.
"""
import argparse
import asyncio
import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import UUID
from sqlalchemy import text

from app.application.analysis_runs.models import AgentRole
from app.application.query_understanding import AnalysisContext, DateResolver, ParsedIntentV1, ParameterConcept
from app.application.query_understanding.context_models import (
    LocalDateRange, ParameterIntentSource, ResolvedParameterIntent, ResolvedTarget,
    SupportingParameterIntent, SupportingIntentReason, TargetSource,
)
from app.application.transcripts.read_models import ReadView
from app.domain import (CollectionPageManifest, ModelRole, TimeRange, ParameterKey,
    TimeResolutionPreference, TimeResolutionPreferenceSource, MonitoringStation, StationAvailability,
    SeriesRef, ParameterProvider, AvailabilityCoverageSource)
from app.domain.evidence.collection_completion_contract import CollectionCompletionContract
from app.domain.guidelines.canonical import digest
from app.infrastructure.adapters._evidence_collection import build_batch
from app.infrastructure.config import AppSettings, load_parameter_capability_registry
from app.infrastructure.guidelines.bundled_source import PackageBundledGuidelineSource
from app.infrastructure.credentials.embedding import embedding_base_url
from app.infrastructure.runtime.event_loop import configure_asyncio_event_loop
from app.infrastructure.runtime.guidelines import GuidelineRuntime
from app.infrastructure.runtime.m5 import TrustedGeometryCatalog, compose_m5_runtime
from app.main import create_app
from app.ports import PortUnavailableError, PortTimeoutError
from scripts.guideline_evaluation import load_json, write_json
from scripts.guideline_holdout import load_cases, prepared_inputs
from scripts.run_sse_smoke import running_server, read_event, stream_headers
from scripts.test_database import temporary_migrated_schema


class ReplayClock:
    def __init__(self, instant):
        self.instant, self.started = instant, time.monotonic()

    def now(self):
        return self.instant + timedelta(seconds=time.monotonic() - self.started)


def replay_deadline(deadline, clock):
    return asyncio.get_running_loop().time() + (deadline-clock.now()).total_seconds()


async def lightweight_status(engine,runs):
    ids=json.dumps([str(value) for value in runs.values()])
    predicate='run_id IN (SELECT jsonb_array_elements_text(CAST(:ids AS jsonb))::uuid)'
    async with engine.connect() as connection:
        states=dict((await connection.execute(text('SELECT run_id,status FROM analysis_runs WHERE '+predicate),{'ids':ids})).tuples().all())
        counts=dict((await connection.execute(text('SELECT run_id,count(*) FROM transcript_items WHERE '+predicate+' GROUP BY run_id'),{'ids':ids})).tuples().all())
    return {'/'.join(key):{'status':states.get(value),'items':counts.get(value,0)} for key,value in runs.items()}


class FrozenObservations:
    def __init__(self, cases):
        self.cases = cases
        self.calls = []
        self.forbidden = {r.record_id for c in cases for r in c.heldout}

    async def collect(self, request, *, deadline=None):
        if deadline is not None and asyncio.get_running_loop().time() >= deadline:
            raise PortUnavailableError('The frozen collection deadline expired.')
        relevant = [c for c in self.cases if request.station_id in {c.target_station, *c.upstream_stations}
                    and c.input_window.start < request.request_range.end and request.request_range.start < c.input_window.end]
        if not relevant:
            raise PortUnavailableError('No frozen observation coverage exists for this request.')
        known_series = {r.provenance.provider_series_id for c in relevant for r in c.observations
                        if r.location.station_id == request.station_id and r.parameter in request.parameter_keys}
        if not set(request.series_refs) <= known_series:
            raise PortUnavailableError('The requested series is outside the frozen snapshot.')
        records = {r.record_id: r for c in relevant for r in c.visible
                   if r.location.station_id == request.station_id and r.parameter in request.parameter_keys
                   and request.request_range.contains(r.observed_time)
                   and r.provenance.provider_series_id in request.series_refs}
        if self.forbidden.intersection(records):
            raise ValueError('heldout identity reached the frozen observation port')
        withheld = any(request.station_id == c.target_station and any(k.code in {'00400','00403'}
                       for k in request.parameter_keys) for c in relevant)
        ranges = [] if withheld else [TimeRange(start=max(c.input_window.start, request.request_range.start),
                    end=min(c.input_window.end, request.request_range.end)) for c in relevant if c.collections_complete]
        ranges = tuple(sorted(set(ranges), key=lambda r:r.start))
        complete = bool(ranges) and ranges[0].start == request.request_range.start and ranges[-1].end == request.request_range.end
        values = tuple(records[k] for k in sorted(records))
        self.calls.append({'request':request.model_dump(mode='json'), 'returned_ids':sorted(records),
                           'withheld':withheld, 'complete':complete})
        return build_batch(operation_id=request.operation_id, records=values,
            pages=(CollectionPageManifest(page_identity='frozen:' + digest([r.record_id for r in values]),
                record_count=len(values), response_bytes=len(json.dumps([r.model_dump(mode='json') for r in values]).encode()),
                page_complete=True),), request_count=0, complete=complete, completed_ranges=ranges,
            contract=CollectionCompletionContract.SINGLE_RESPONSE,
            incomplete_reason=None if complete else 'frozen_holdout_or_partial_time_coverage')


class SealedAcquisition:
    async def collect(self, request, *, deadline=None):
        raise PortUnavailableError('This acquisition channel is sealed for the frozen holdout.')


class FrozenInventory:
    def __init__(self, case, features):
        self.case, self.features = case, features

    async def check_availability(self, request, *, deadline=None):
        kept = []
        ranges = {(r.segment_index,r.window_index):r for r in request.historical_ranges}
        for feature in self.features:
            properties=feature['properties']
            if str(properties['comid']) != request.comid:continue
            station=properties['identifier']
            groups={}
            for record in self.case.visible:
                if record.location.station_id!=station:continue
                units=tuple(u for u in request.coverage_units if u.parameter==record.parameter
                    and (u.segment_index,u.window_index) in ranges
                    and ranges[(u.segment_index,u.window_index)].start <= record.observed_time
                    < ranges[(u.segment_index,u.window_index)].end)
                if units:
                    key=(record.parameter,record.provenance.provider_series_id,record.channel)
                    groups.setdefault(key,set()).update(units)
            refs=tuple(SeriesRef(provider=ParameterProvider.WDFN_TIME_SERIES,station_id=station,
                parameter=key[0],series_identity=key[1],channel=key[2],
                coverage_source=AvailabilityCoverageSource.HISTORICAL_TIME_SERIES_OBSERVATION,
                covered_units=tuple(sorted(units,key=lambda u:(u.segment_index,u.window_index))))
                for key,units in sorted(groups.items(),key=lambda item:(item[0][0].code_system,item[0][0].code,item[0][1])))
            if refs:
                units = tuple(sorted({u for r in refs for u in r.covered_units},
                    key=lambda u:(u.segment_index,-1 if u.window_index is None else u.window_index,
                                  u.parameter.code_system,u.parameter.code)))
                lon,lat=feature['geometry']['coordinates']
                kept.append(StationAvailability(station=MonitoringStation(station_id=station,source='nwissite',
                    name=properties['name'],location={'latitude':lat,'longitude':lon},associated_comid=request.comid),
                    comid=request.comid,providers=(ParameterProvider.WDFN_TIME_SERIES,),series_refs=refs,
                    covered_units=units,checked_at=self.case.retrieved_at))
        return tuple(kept)


def case_question(case):
    return f'Estimate the mean river pH in each six-hour window for {case.target_station} on {case.window.start.date().isoformat()}; retain uncertainty and explain missing support.'


def frozen_context(case, feature, configuration, instant):
    registry = load_parameter_capability_registry()
    ph = registry.get(ParameterKey(code_system='usgs',code='00400'))
    flow = registry.get(ParameterKey(code_system='usgs',code='00060'))
    date = case.window.start.date()
    parsed = ParsedIntentV1(start_date=date, end_date=date,
        parameters=(ParameterConcept(concept='pH', rationale='Explicitly requested frozen holdout.', explicitly_named=True),))
    temporal = DateResolver().resolve(None, parsed, decision_instant=instant, analysis_timezone='UTC',
        resolution_preference=TimeResolutionPreference.SIX_HOURS,
        resolution_preference_source=TimeResolutionPreferenceSource.FRONTEND).temporal
    longitude, latitude = feature['geometry']['coordinates']
    return AnalysisContext(query=case_question(case), decision_instant=instant, analysis_timezone='UTC',
        target=ResolvedTarget(comid=str(feature['properties']['comid']), point={'latitude':latitude,'longitude':longitude},
            display_name=feature['properties']['name'], country_code='US',
            subdivision_code='US-MD' if case.target_station=='USGS-01646500' else 'US-NJ', source=TargetSource.FRONTEND),
        requested_date_range=LocalDateRange(start_date=date,end_date=date), time_range=temporal.time_range,
        time_segments=temporal.time_segments, mode=temporal.mode, time_resolution=temporal.time_resolution,
        time_resolution_source=temporal.time_resolution_source,
        params_used=(ResolvedParameterIntent(key=ph.key,canonical_name=ph.canonical_name,canonical_unit=ph.canonical_unit,
            source=ParameterIntentSource.EXPLICIT,rationale='Explicit frozen holdout target.'),),
        supporting_params=(SupportingParameterIntent(key=flow.key,canonical_name=flow.canonical_name,
            canonical_unit=flow.canonical_unit,reason=SupportingIntentReason.CAPABILITY_REQUIRED),),
        parser_model=configuration.parser_model,waterbody_model=configuration.waterbody_model,
        memory_mode=configuration.memory_mode)


class FrozenQuery:
    def __init__(self, case, feature, instant):
        self.case, self.feature, self.instant = case, feature, instant

    async def understand(self, context):
        value = frozen_context(self.case,self.feature,context.run.configuration,self.instant)
        value.validate_configuration(context.run.configuration)
        session = await context.operations.prepare_agent_session(context.run.run_id,agent_role=AgentRole.QUERY_PARSER)
        await context.operations.mark_agent_session_running(context.run.run_id,session.session_id)
        item = await context.operations.reserve_transcript_item(context.run.run_id,session_id=session.session_id)
        await context.operations.finalize_transcript_item(context.run.run_id,item.item_id,'query_understood',1,value)
        await context.operations.complete_agent_session(context.run.run_id,session.session_id)
        return value


class FrozenTopology:
    def __init__(self, key, builder, graphs, output=None):
        self.key, self.builder, self.graphs, self.output = key,builder,graphs,output

    async def prepare(self,context):
        value=await self.builder.build(context)
        if self.output is not None:
            await asyncio.to_thread(lambda:write_json(self.output/f'{self.key}-topology.json',value.model_dump(mode='json')))
        return value

    async def build(self, context):
        if self.key not in self.graphs:
            self.graphs[self.key] = asyncio.create_task(self.prepare(context))
        return await asyncio.shield(self.graphs[self.key])


async def evaluate(settings, cases, metadata_root, output):
    settings = settings.model_copy(update={'memory_enabled':False,'analysis_timezone':'UTC','log_level':'warning',
        'topology_max_distance_km':100.0})
    if settings.openai_base_url.rstrip('/') != 'https://api.deepseek.com':
        raise ValueError('unexpected analysis service')
    if embedding_base_url(settings).rstrip('/') != 'https://generativelanguage.googleapis.com/v1beta/openai':
        raise ValueError('unexpected embedding service')
    if settings.db_name != __import__('os').environ.get('WATERAGENTS_TEST_DB_NAME'):
        raise ValueError('dedicated test database required')
    latest=max(case.window.end for case in cases)
    clock = ReplayClock(latest.replace(hour=6,minute=0,second=0,microsecond=0)+timedelta(days=1))
    observations, graphs, runs, streams, early_pins = FrozenObservations(cases), {}, {}, {}, {}
    by_query = {case_question(c):c for c in cases}
    class EmptySource:
        def read_versions(self): return ()
    original_start = GuidelineRuntime.start
    async def start_empty(runtime, repository, registry, **kwargs):
        await original_start(runtime,repository,registry,source=EmptySource())
    @asynccontextmanager
    async def frozen_m5(active_settings, **options):
        geometry = TrustedGeometryCatalog()
        yield compose_m5_runtime(active_settings,water_port=observations,weather_port=SealedAcquisition(),
            facility_port=SealedAcquisition(),geometry_catalog=geometry,scope_preparer=geometry.register_scope,
            upstream_port=options.get('upstream_results'),**options)
    def event(kind, **values):
        item={'at':datetime.now(timezone.utc).isoformat(),'event':kind,**values}
        with (output/'progress.jsonl').open('a',encoding='utf-8') as stream: stream.write(json.dumps(item)+'\n')
        print(json.dumps(item),flush=True)
    async def watch(client,run_id):
        counts={}
        try:
            async with client.stream('GET',f'/api/analysis/runs/{run_id}/events',timeout=None) as response:
                stream_headers(response)
                lines=response.aiter_lines()
                while True:
                    name,_,_=await read_event(lines)
                    counts[name]=counts.get(name,0)+1
                    if name=='stream_end':return counts
        except Exception as error:
            counts['error_type']=type(error).__name__
            return counts
    @asynccontextmanager
    async def preserve(app):
        try:
            yield
        finally:
            write_json(output/'acquisition-audit.json',observations.calls)
            for key,run_id in runs.items():
                try:
                    raw=await app.state.transcript_store.read_run(run_id)
                    if raw is not None:
                        await asyncio.to_thread(lambda raw=raw,key=key:write_json(
                            output/f'{key[0]}-{key[1]}-raw.json',raw.model_dump(mode='json')))
                except Exception as error:event('snapshot_error',case_id=key[0],arm=key[1],error_type=type(error).__name__)
            for task in streams.values():
                if not task.done():task.cancel()
            await asyncio.gather(*streams.values(),return_exceptions=True)
            for task in graphs.values():
                if not task.done():task.cancel()
            await asyncio.gather(*graphs.values(),return_exceptions=True)
    safe_fields=('waterbody_workflow_max_concurrency','reasoning_max_planning_turns','reasoning_max_tool_calls_per_turn',
        'reasoning_max_cells_per_batch','reasoning_max_batches','reasoning_model_timeout_seconds','reasoning_session_deadline_seconds',
        'topology_max_distance_km','topology_max_hops','topology_max_nodes','topology_stage_timeout_seconds')
    write_json(output/'evaluation-config.json',{'configuration_shared_in_one_app':True,
        'settings':{key:getattr(settings,key) for key in safe_fields},'policy':settings.guideline_policy.model_dump(mode='json'),
        'historical_clock':clock.instant.isoformat(),'analysis_timezone':'UTC','memory_mode':'stateless',
        'deadline_clock':'same replay clock in application and HTTP deadline conversion; original durations unchanged'})
    with patch('app.main.build_m5_runtime',frozen_m5), patch.object(GuidelineRuntime,'start',start_empty), patch(
            'app.infrastructure.adapters.openai_responses_waterbody._monotonic_deadline',lambda deadline:replay_deadline(deadline,clock)):
        app=create_app(settings,clock=clock)
        async with running_server(app) as client, preserve(app):
            write_json(output/'server.json',{'base_url':str(client.base_url)})
            original_get=app.state.provider_http.get_bytes
            cache_root=output.parent/'topology-http'
            cache_root.mkdir(exist_ok=True)
            cache_locks={}
            async def metadata_get(url,**options):
                if not url.startswith('https://api.water.usgs.gov/nldi/'):
                    raise PortUnavailableError('Online acquisition outside NLDI topology is sealed.')
                if options.get('deadline') is not None and asyncio.get_running_loop().time()>=options['deadline']:
                    raise PortTimeoutError('The frozen topology request deadline expired.')
                identity=digest({'url':url,'params':options.get('params')})[7:]
                path=cache_root/(identity+'.bin')
                async with cache_locks.setdefault(identity,asyncio.Lock()):
                    if path.exists():
                        return await asyncio.to_thread(path.read_bytes)
                    value=await original_get(url,**options)
                    await asyncio.to_thread(path.write_bytes,value)
                    with (cache_root/'index.jsonl').open('a',encoding='utf-8') as stream:
                        stream.write(json.dumps({'identity':identity,'url':url,'params':options.get('params'),
                            'fetched_at':datetime.now(timezone.utc).isoformat(),'bytes':len(value)})+'\n')
                    return value
            app.state.provider_http.get_bytes=metadata_get
            original_build=app.state.provider_bundle_factory.build
            repository=app.state.guideline_runtime._repository
            for _ in range(60):
                if (await repository.read_catalog()).profile == app.state.guideline_runtime.desired_profile:break
                await asyncio.sleep(.1)
            else:raise ValueError('empty control catalog profile did not become ready')
            async def build(configuration):
                bundle=await original_build(configuration)
                original_run=bundle.workflow.run
                async def run(context):
                    case=by_query[context.request.query]
                    if not installed:
                        pin=await bundle.workflow._guideline_pinner(context.run.run_id,context.cancellation)
                        library,_=await repository.retrieval_counts(pin.pin.catalog,(ParameterKey(code_system='usgs',code='00400'),))
                        if pin.pin.status!='ready' or library:
                            raise ValueError('control must pin an actually empty catalog')
                        early_pins[context.run.run_id]=pin
                        async def pinned(run_id,cancellation):
                            return early_pins[run_id]
                        bundle.workflow._guideline_pinner=pinned
                    feature=load_json(metadata_root/f'{case.target_station}.json')['features'][0]
                    bundle.workflow._query_understanding=FrozenQuery(case,feature,clock.instant)
                    builder=bundle.workflow._topology_builder
                    features=[load_json(metadata_root/f'{s}.json')['features'][0]
                              for s in (case.target_station,*case.upstream_stations)]
                    builder._station_inventory=FrozenInventory(case,features)
                    bundle.workflow._topology_builder=FrozenTopology(case.case_id,builder,graphs,output)
                    await original_run(context)
                bundle.workflow.run=run
                return bundle
            app.state.provider_bundle_factory.build=build
            async def start(case,arm):
                capability=next(c for c in settings.model_catalog if ModelRole.WATERBODY in c.roles)
                model={'provider':capability.provider,'model':capability.model}
                parser=next(c for c in settings.model_catalog if ModelRole.QUERY_PARSER in c.roles)
                feature=load_json(metadata_root/f'{case.target_station}.json')['features'][0]
                lon,lat=feature['geometry']['coordinates']
                request={'query':case_question(case),'target':{'coordinates':{'latitude':lat,'longitude':lon}},
                    'date_range':{'start_date':str(case.window.start.date()),'end_date':str(case.window.start.date())},
                    'time_resolution':'6h','memory_mode':'stateless',
                    'parser_model':{'provider':parser.provider,'model':parser.model},'waterbody_model':model}
                response=await client.post('/api/analysis/runs',json=request,timeout=30)
                if response.status_code!=201:
                    write_json(output/f'{case.case_id}-{arm}-creation.json',response.json())
                    raise ValueError('holdout run creation failed')
                run_id=UUID(response.json()['run_id'])
                runs[(case.case_id,arm)]=run_id
                streams[(case.case_id,arm)]=asyncio.create_task(watch(client,run_id))
                write_json(output/'runs.json',[{'case_id':k[0],'arm':k[1],'run_id':str(v)} for k,v in runs.items()])
                event('run_started',case_id=case.case_id,arm=arm,run_id=str(run_id))
            installed=False
            for case in cases: await start(case,'control')
            last={}
            async with asyncio.timeout(7200):
                while True:
                    statuses=await lightweight_status(app.state.transcript_store._engine,runs)
                    if not installed and len(early_pins)==len(cases):
                        versions=PackageBundledGuidelineSource().read_versions()
                        for version in versions: await repository.import_bundled_version(version)
                        for _ in range(180):
                            docs=[await repository.read_document_details(v.identity.guideline_id) for v in versions]
                            if all(d and d.state=='available' for d in docs):break
                            await asyncio.sleep(1)
                        else:raise ValueError('approved guideline preparation failed')
                        installed=True
                        for case in cases:await start(case,'guided')
                        event('approved_library_ready',versions=len(versions))
                    if statuses!=last:
                        event('progress',runs=statuses)
                        write_json(output/'status.json',statuses)
                        last=statuses
                    terminal={'completed','failed','cancelled'}
                    if installed and len(statuses)==2*len(cases) and all(s['status'] in terminal for s in statuses.values()):break
                    if not installed and all(s['status'] in terminal for s in statuses.values()):
                        raise ValueError('control ended before all frozen topologies were ready')
                    await asyncio.sleep(3)
            for key,run_id in runs.items():
                stem=f'{key[0]}-{key[1]}'
                view=await app.state.transcript_reader.read_registered_run(run_id,ReadView.HISTORY)
                write_json(output/f'{stem}-transcript.json',view.model_dump(mode='json'))
                history=await client.get(f'/api/analysis/runs/{run_id}',params={'view':'history'},timeout=30)
                write_json(output/f'{stem}-history.json',history.json())
                write_json(output/f'{stem}-sse.json',await streams[key])
            write_json(output/'acquisition-audit.json',observations.calls)
            for key,task in graphs.items():
                if task.done() and not task.cancelled() and task.exception() is None:
                    write_json(output/f'{key}-topology.json',task.result().model_dump(mode='json'))
            event('all_pairs_finished')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset',required=True)
    parser.add_argument('--metadata-root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--allow-live',action='store_true')
    args=parser.parse_args()
    if not args.allow_live:parser.error('--allow-live is required for real provider dispatch')
    cases=load_cases(load_json(args.dataset))
    if not 1<=len(cases)<=5:
        parser.error('live evaluation requires one to five explicit cases')
    if any(case.window.end-case.window.start!=timedelta(hours=6) or case.window.start.hour%6
           or case.window.start.minute or case.window.start.second or case.window.start.microsecond for case in cases):
        parser.error('each truth window must match a complete six-hour UTC output window')
    if any(case.input_window.start!=case.window.start.replace(hour=0,minute=0,second=0,microsecond=0)
           or case.input_window.end-case.input_window.start!=timedelta(days=1) for case in cases):
        parser.error('freeze the complete UTC acquisition day before running a six-hour holdout')
    args.output.mkdir(parents=True,exist_ok=False)
    for case in cases:write_json(args.output/f'{case.case_id}-input-audit.json',prepared_inputs(case))
    with temporary_migrated_schema() as settings:
        asyncio.run(evaluate(settings,cases,args.metadata_root,args.output))


if __name__=='__main__':
    configure_asyncio_event_loop()
    try:
        main()
    except Exception:
        print(json.dumps({'status':'failed','reason':'invalid_input_or_unavailable_evaluation_service'}))
        raise SystemExit(1)

"""Audit and score saved live holdout workflows; never dispatches a provider."""
import argparse
import json
from collections import Counter
from pathlib import Path
from uuid import UUID

from app.application.guidelines.transcript_models import GuidelineDeliveryV1, GuidelineUsageV1, ResultGuidelineUsageV1
from app.application.query_understanding import AnalysisContext
from app.application.reasoning.transcript_models import WaterbodyModelRequestV1
from app.domain import HydrologyGraph, InferenceResult, ParameterKey
from app.domain.evidence.evidence_source_records import EvidenceSourceRecords
from app.domain.guidelines.canonical import digest
from app.infrastructure.guidelines.bundled_source import PackageBundledGuidelineSource
from scripts.guideline_evaluation import load_json, write_json
from scripts.guideline_holdout import load_cases, prepared_inputs, Prediction, score_prediction


def validate_submitted_records(case,records):
    visible={r.record_id:r for r in case.visible}
    observed=set()
    for record in records:
        if visible.get(record.record_id)!=record:
            raise ValueError('submitted source record is heldout, unfrozen or changed')
        observed.add(record.record_id)
    return observed


def source_records(case, items):
    observed=set()
    for item in items:
        if item['item']['content_type']!='evidence_source_records':continue
        value=EvidenceSourceRecords.model_validate(item['body'])
        observed.update(validate_submitted_records(case,value.records))
    return observed


def eligible_anchor_ids(case,point,source_ids):
    visible={r.record_id:r for r in case.visible}
    eligible=set()
    ph=ParameterKey(code_system='usgs',code='00400')
    for anchor in point.measured_anchors:
        for ref in anchor.source_record_refs:
            record=visible.get(ref.record_id)
            if record is None or record.parameter!=anchor.parameter or ref.record_id not in source_ids or record.location.station_id!=anchor.station_id:
                raise ValueError('measured anchor does not resolve to its actually collected visible record')
            if record.parameter==ph and record.location.station_id in case.upstream_stations:
                eligible.add(ref.record_id)
    return eligible


def review_arm(case, raw, versions, *, guided):
    if any(item['parse_error'] is not None or item['body'] is None for item in raw['items']):
        raise ValueError('saved run contains an unreadable transcript item')
    items=raw['items']
    source_ids=source_records(case,items)
    forbidden={r.record_id for r in case.heldout}
    by_type={}
    for item in items:by_type.setdefault(item['item']['content_type'],[]).append(item)
    context=AnalysisContext.model_validate(by_type['query_understood'][0]['body'])
    graph=HydrologyGraph.model_validate(by_type['topology_ready'][0]['body'])
    run_id=UUID(raw['run']['run_id'])
    requests={}
    for item in by_type.get('waterbody_model_request',[]):
        request=WaterbodyModelRequestV1.model_validate(item['body'])
        if any(key in request.model_dump_json() for key in forbidden):
            raise ValueError('heldout observation identity reappeared in a model request')
        if request.evidence_bundle_ref.run_id!=run_id:
            raise ValueError('cross-run model request')
        requests[request.operation_id]=(item,request)
    approved={v.version_id:v for v in versions}
    deliveries={}
    sent_bodies=0
    for item in by_type.get('guideline_delivery',[]):
        delivery=GuidelineDeliveryV1.model_validate(item['body'])
        operation=requests.get(delivery.operation_id)
        if delivery.run_id!=run_id or operation is None or str(delivery.request_ref.item_id)!=operation[0]['item']['item_id']:
            raise ValueError('delivery does not match an actual request in this run')
        if delivery.session_id!=UUID(operation[0]['item']['session_id']):
            raise ValueError('delivery session mismatch')
        if delivery.phase!=operation[1].phase.value:
            raise ValueError('delivery phase mismatch')
        entries=[]
        for message in operation[1].messages:
            try:value=json.loads(message.text)
            except ValueError:continue
            if isinstance(value,dict):entries.extend(value.get('advisory_knowledge',[]))
        expected=[{'alias':e.alias,**e.version.model_view.model_dump(mode='json')} for e in delivery.entries]
        if entries!=expected or any(approved.get(e.version.version_id)!=e.version for e in delivery.entries):
            raise ValueError('actual advisory body differs from its approved saved delivery')
        sent_bodies+=len(delivery.entries)
        deliveries[UUID(item['item']['item_id'])]=delivery
    delivered_operations={d.operation_id for d in deliveries.values()}
    for operation,(_,request) in requests.items():
        if operation in delivered_operations:continue
        for message in request.messages:
            try:value=json.loads(message.text)
            except ValueError:continue
            if isinstance(value,dict) and value.get('advisory_knowledge'):
                raise ValueError('advisory was submitted without a matching delivery record')
    if not guided and sent_bodies:
        raise ValueError('control contains advisory knowledge')
    if not guided:
        for _,request in requests.values():
            if any(v.model_view.body in request.system_prompt or any(v.model_view.body in m.text for m in request.messages)
                   for v in versions):
                raise ValueError('control contains advisory text outside a delivery')
    usages={}
    citations=0
    for item in by_type.get('guideline_usage',[]):
        usage=GuidelineUsageV1.model_validate(item['body'])
        if usage.run_id!=run_id:raise ValueError('cross-run usage')
        for citation in usage.citations:
            delivery=deliveries.get(citation.delivery_ref.item_id)
            if delivery is None or delivery.operation_id!=usage.operation_id or delivery.session_id!=usage.session_id or not any(
                e.version.version_id==citation.version_id and e.version.identity.guideline_id==citation.guideline_id for e in delivery.entries):
                raise ValueError('usage is not bound to its actual approved delivery')
            citations+=1
        usages[UUID(item['item']['item_id'])]=usage
    result_ids=set()
    target=None
    for item in by_type.get('inference_result',[]):
        result=InferenceResult.model_validate(item['body'])
        result_ids.add(result.result_id)
        if result.target_comid==context.target.comid:target=result
    result_bindings=0
    for item in by_type.get('result_guideline_usage',[]):
        value=ResultGuidelineUsageV1.model_validate(item['body'])
        if value.run_id!=run_id or value.result_id not in result_ids:
            raise ValueError('result usage is not owned by a result in this run')
        for binding in value.bindings:
            if not all(key in usages for key in binding.usage_item_ids):raise ValueError('result usage has a missing operation usage')
            result_bindings+=len(binding.usage_item_ids)
    prediction=Prediction(status='failed',truth_kind=case.truth_kind,window=case.window)
    point=None
    anchor_ids=set()
    if target is not None:
        point=next((point for series in target.series if series.parameter.key.code=='00400' for point in series.points
                    if point.start_time==case.window.start and point.end_time==case.window.end),None)
    if point is not None:
        anchor_ids=eligible_anchor_ids(case,point,source_ids)
        status=('point' if point.representative_value is not None else 'range' if point.value_lower is not None else 'indeterminate')
        prediction=Prediction(status=status,truth_kind=case.truth_kind,window=case.window,
            value=point.representative_value,lower=point.value_lower,upper=point.value_upper)
    metrics=score_prediction(case,prediction,has_eligible_same_indicator_anchor=bool(anchor_ids))
    # Representative model windows without an explicit mean remain descriptive,
    # rather than silently being labelled the observed-readings mean estimand.
    if point is not None and prediction.status=='point' and (point.aggregation_method is None or point.aggregation_method.value!='mean'):
        metrics={'status':'not_comparable','reason':'model_window_statistic_not_explicitly_mean',
                 'descriptive_window_value':point.representative_value,'observed_readings_mean':str(case.truth)}
    return {
        'run_id':str(run_id),'status':raw['run']['status'],'input_digest':prepared_inputs(case)['input_digest'],
        'context_digest':digest(context.model_dump(mode='json')),'topology_digest':digest(graph.model_dump(mode='json')),
        'model_selections':sorted({(r.model.provider,r.model.model) for _,r in requests.values()}),
        'phases':dict(Counter(r.phase.value for _,r in requests.values())),
        'retrieval_statuses':dict(Counter(i['body']['selection']['status'] for i in by_type.get('guideline_retrieval',[]))),
        'source_records':len(source_ids),'nonempty_deliveries':sum(bool(d.entries) for d in deliveries.values()),
        'whole_document_submissions':sent_bodies,'usage_records':len(usages),'citations':citations,'result_bindings':result_bindings,
        'guided_knowledge_actually_provided':bool(sent_bodies),'prediction':prediction.model_dump(mode='json'),
        'measured_anchor_records':sorted(anchor_ids),'metrics':metrics,
        'target_window':point.model_dump(mode='json') if point is not None else None,
        'all_target_ph_windows':[p.model_dump(mode='json') for s in target.series
            if s.parameter.key.code=='00400' for p in s.points] if target else [],
        'attribution':target.attribution_summary.model_dump(mode='json') if target else None,
        'analysis_detail':target.analysis_detail.model_dump(mode='json') if target else None,
        'waterbody_errors':[i['body']['payload']['error'] for i in by_type.get('lifecycle_event',[])
            if i['body']['type'] in {'session_failed','run_failed'} and i['body']['payload'].get('error')],
    }


def review(case,control,guided,versions):
    arms=[review_arm(case,raw,versions,guided=enabled) for raw,enabled in ((control,False),(guided,True))]
    for key in ('input_digest','context_digest','topology_digest','model_selections'):
        if arms[0][key]!=arms[1][key]:raise ValueError('paired immutable input, topology or configured models differ')
    if arms[0]['run_id']==arms[1]['run_id']:raise ValueError('pair needs distinct actual runs')
    return {'case_id':case.case_id,'truth':str(case.truth),'truth_kind':case.truth_kind,
        'truth_samples':len(case.truth_observations),'heldout_samples':len(case.heldout),
        'control':arms[0],'guided':arms[1],
        'limits':[*case.limitations,'Adaptive model replies and tool decisions can differ; no claim of identical call sequences.',
            'One pair; provider weights and random sampling are not version-pinned.',
            'The historical clock replays date contracts; this is retrospective estimation, not a historical deployment.',
            'Ranges describe a comparison to the available-readings mean, not calibrated interval coverage.']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset',required=True)
    parser.add_argument('--runs-root',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    versions=PackageBundledGuidelineSource().read_versions()
    rows=[]
    for case in load_cases(load_json(args.dataset)):
        try:
            arms=[load_json(args.runs_root/f'{case.case_id}-{arm}-transcript.json') for arm in ('control','guided')]
            rows.append(review(case,*arms,versions))
        except (ValueError,KeyError,FileNotFoundError) as error:
            rows.append({'case_id':case.case_id,'status':'rejected','reason':str(error) if type(error) is ValueError else type(error).__name__})
    write_json(args.output,{'configuration':load_json(args.runs_root/'evaluation-config.json'),
        'pairs':rows,'rejected':sum(r.get('status')=='rejected' for r in rows)})


if __name__=='__main__':main()

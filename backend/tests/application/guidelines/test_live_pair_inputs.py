"""Offline checks for the live evaluation's acquisition and topology boundaries."""
import asyncio
from pathlib import Path
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace

import pytest

from app.domain import ParameterKey, WaterObservationRequest, ParameterProvider, CoverageUnit, StationAvailabilityRequest
from app.ports import PortUnavailableError
from scripts.guideline_evaluation import load_json
from scripts.guideline_holdout import load_cases, HoldoutCase, EvidenceArtifact, audit_inputs, SEALED_CHANNELS
from scripts.guideline_live_pairs import FrozenObservations, FrozenInventory, FrozenTopology, ReplayClock, replay_deadline
from scripts.guideline_live_review import validate_submitted_records, eligible_anchor_ids
from app.domain.evidence.measured_anchor import MeasuredAnchor

ROOT=Path(__file__).resolve().parents[4]
CASES=load_cases(load_json(ROOT/'backend/tests/fixtures/guidelines/ph_holdout_cases.json'))


def request_for(case, record, **changes):
    return WaterObservationRequest.model_validate({
        'operation_id':'holdout-test', 'provider':ParameterProvider.WDFN_TIME_SERIES,
        'channel':'continuous','station_id':record.location.station_id,
        'series_refs':(record.provenance.provider_series_id,), 'parameter_keys':(record.parameter,),
        'request_range':case.window, 'analysis_timezone':'UTC','affected_window_ids':('window-2',),
        **changes})


@pytest.mark.parametrize('case',CASES,ids=lambda c:c.case_id)
def test_target_holdout_cannot_be_returned_as_observed_or_complete_empty(case):
    async def exercise():
        port=FrozenObservations([case])
        result=await port.collect(request_for(case,case.heldout[0]))
        assert not result.records
        assert not result.completion_proof.complete
        assert not result.completed_ranges
        assert port.calls[0]['withheld']
    asyncio.run(exercise())


def test_partial_frozen_window_and_unknown_series_cannot_claim_complete_collection():
    async def exercise():
        case=CASES[0]
        record=next(r for r in case.visible if r.parameter.code=='00400')
        request=request_for(case,record,request_range={'start':'2024-07-01T00:00:00Z','end':'2024-07-02T00:00:00Z'})
        port=FrozenObservations(CASES)
        result=await port.collect(request)
        assert result.records and not result.completion_proof.complete
        assert result.completed_ranges==(case.window,)
        assert {r.record_id for r in result.records}.isdisjoint(port.forbidden)
        with pytest.raises(PortUnavailableError):
            await port.collect(request_for(case,record,series_refs=('unfrozen-series',)))
        with pytest.raises(PortUnavailableError):
            await port.collect(request_for(case,record,station_id='USGS-00000000'))
    asyncio.run(exercise())


@pytest.mark.parametrize('case',CASES,ids=lambda c:c.case_id)
def test_topology_inventory_uses_visible_observations_and_never_qualifies_withheld_target_ph(case):
    async def exercise():
        feature=load_json(ROOT/f'backend/tests/fixtures/guidelines/ph_sites/{case.target_station}.json')['features'][0]
        request=StationAvailabilityRequest(comid=str(feature['properties']['comid']),
            coverage_units=tuple(CoverageUnit(parameter=ParameterKey(code_system='usgs',code=code),segment_index=0,window_index=2)
                                 for code in ('00060','00400')),
            historical_ranges=({'segment_index':0,'window_index':2,**case.window.model_dump()},),prediction_baseline_ranges=())
        values=await FrozenInventory(case,[feature]).check_availability(request)
        assert values
        assert all(ref.parameter.code!='00400' for value in values for ref in value.series_refs)
        assert all(unit.window_index==2 for value in values for unit in value.covered_units)
    asyncio.run(exercise())


def test_guided_arm_reuses_the_exact_completed_control_topology_without_new_provider_lookup():
    async def exercise():
        class Builder:
            calls=0
            async def build(self,context):
                self.calls+=1
                return object()
        first,second=Builder(),Builder()
        graphs={}
        control=await FrozenTopology('same-case',first,graphs).build(None)
        guided=await FrozenTopology('same-case',second,graphs).build(None)
        assert control is guided
        assert first.calls==1 and second.calls==0
    asyncio.run(exercise())


def test_historical_clock_preserves_real_request_budget_and_expired_deadlines():
    async def exercise():
        clock=ReplayClock(datetime(2024,7,3,tzinfo=timezone.utc))
        deadline=clock.now()+timedelta(seconds=10)
        now=asyncio.get_running_loop().time()
        assert now+9.9 < replay_deadline(deadline,clock) <= now+10.1
        assert replay_deadline(clock.now()-timedelta(seconds=1),clock)<now
    asyncio.run(exercise())


def test_saved_source_audit_rejects_heldout_and_changed_values_even_with_an_existing_record_id():
    case=CASES[0]
    record=case.visible[0]
    assert validate_submitted_records(case,[record])=={record.record_id}
    for unsafe in (case.heldout[0],record.model_copy(update={'value':'999'})):
        with pytest.raises(ValueError,match='heldout, unfrozen or changed'):
            validate_submitted_records(case,[unsafe])


def test_related_flow_or_uncollected_ph_cannot_be_declared_a_measured_ph_anchor():
    case=CASES[0]
    ph=next(r for r in case.visible if r.parameter.code=='00400')
    flow=next(r for r in case.visible if r.parameter.code=='00060')
    def point(record):
        return SimpleNamespace(measured_anchors=(MeasuredAnchor(parameter=record.parameter,
            source_record_refs=({'item_id':'synthetic-test-source','record_id':record.record_id},),
            origin_comid='100',station_id=record.location.station_id,evidence_time_range=case.window,
            path_comids=('100','200')),))
    assert not eligible_anchor_ids(case,point(flow),{flow.record_id})
    with pytest.raises(ValueError,match='actually collected'):
        eligible_anchor_ids(case,point(ph),set())
    assert eligible_anchor_ids(case,point(ph),{ph.record_id})=={ph.record_id}


@pytest.mark.parametrize('case_id',[c.case_id for c in CASES])
def test_full_acquisition_day_retains_the_original_score_window_and_masks_adjacent_target_ph(case_id):
    cases=load_cases(load_json(ROOT/'backend/tests/fixtures/guidelines/ph_holdout_acquisition_cases.json'))
    case=next(c for c in cases if c.case_id==case_id)
    assert case.input_window.end-case.input_window.start==timedelta(days=1)
    assert len(case.heldout)>len(case.truth_observations)>0
    trimmed=HoldoutCase.model_validate(case.model_dump() | {'acquisition_window':None,
        'observations':tuple(r for r in case.observations if case.window.contains(r.observed_time))})
    assert case.truth==trimmed.truth
    adjacent=next(r for r in case.heldout if not case.window.contains(r.observed_time))
    assert adjacent.record_id not in {r.record_id for r in case.visible}
    with pytest.raises(ValueError,match='heldout'):
        audit_inputs(case,(EvidenceArtifact(artifact_id='synthetic-adjacent-copy',kind='raw',
            observation_refs=(adjacent.record_id,)),),sealed_channels=SEALED_CHANNELS)
    async def exercise():
        flow=next(r for r in case.visible if r.location.station_id==case.target_station and r.parameter.code=='00060')
        result=await FrozenObservations([case]).collect(request_for(case,flow,request_range=case.input_window))
        assert result.completion_proof.complete and result.completed_ranges==(case.input_window,)
        assert all(r.parameter.code=='00060' for r in result.records)
    asyncio.run(exercise())


def test_live_cli_rejects_six_hour_only_acquisition_before_database_or_provider_start(monkeypatch,tmp_path):
    from scripts import guideline_live_pairs as module
    def unexpected():raise AssertionError('short acquisition started the database')
    monkeypatch.setattr(module,'temporary_migrated_schema',unexpected)
    output=tmp_path/'never-created'
    monkeypatch.setattr('sys.argv',['guideline_live_pairs','--dataset',str(ROOT/'backend/tests/fixtures/guidelines/ph_holdout_cases.json'),
        '--metadata-root',str(ROOT/'backend/tests/fixtures/guidelines/ph_sites'),'--output',str(output),'--allow-live'])
    with pytest.raises(SystemExit) as error:module.main()
    assert error.value.code==2 and not output.exists()

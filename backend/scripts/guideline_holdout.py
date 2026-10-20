"""Offline pH holdout inputs, lineage auditing and scoring; no provider dispatch."""
import re
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, model_validator

from app.application.evidence_policy.aggregation import aggregate_observations
from app.application.evidence_tools.collection_service import build_policy_observation
from app.domain.common import DomainModel, NonEmptyString
from app.domain.evidence import WaterObservationRecord, DischargeObservationRecord
from app.domain.evidence.source_record_reference import SourceRecordReference
from app.domain.hydrology.evidence_aggregation_method import EvidenceAggregationMethod
from app.domain.hydrology.parameter_provider import ParameterProvider
from app.domain.time.time_range import TimeRange
from app.domain.guidelines.canonical import digest
from app.infrastructure.config import load_parameter_capability_registry

PH_CODES = {'00400', '00403'}
SEALED_CHANNELS = {'history_rag', 'evidence_cache', 'online_collection', 'target_context'}


def compact_dataset(cases):
    """Lossless shared series metadata keeps the public input fixture small."""
    output = []
    for case in cases:
        item = case.model_dump(mode='json', exclude={'observations'})
        groups = {}
        for record in case.observations:
            value = record.model_dump(mode='json', exclude_defaults=True)
            value['record_type'] = record.record_type
            value.pop('record_id')
            time, measurement = value.pop('observed_time'), value.pop('value')
            quality = value.pop('provider_quality')
            value['provenance'].pop('provider_record_id')
            identity = digest(value)
            if identity not in groups:
                groups[identity] = {'metadata': value, 'samples': []}
            groups[identity]['samples'].append({'record_id': record.record_id, 'time': time, 'value': measurement, 'quality': quality})
        item['series'] = list(groups.values())
        output.append(item)
    return {'schema_version': 1, 'format': 'shared-series-v1', 'cases': output}


def load_cases(body):
    if body.get('schema_version') != 1:
        raise ValueError('unknown holdout input schema')
    cases = []
    for raw in body['cases']:
        if body.get('format') == 'shared-series-v1':
            item, records = {k: v for k, v in raw.items() if k != 'series'}, []
            for series in raw['series']:
                for sample in series['samples']:
                    metadata = series['metadata']
                    records.append({**metadata, 'record_id': sample['record_id'], 'observed_time': sample['time'],
                        'value': sample['value'], 'provider_quality': sample['quality'],
                        'provenance': {**metadata['provenance'], 'provider_record_id': sample['record_id']}})
            item['observations'] = records
        elif 'format' not in body:
            item = raw
        else:
            raise ValueError('unknown holdout input format')
        cases.append(HoldoutCase.model_validate(item))
    if len({c.case_id for c in cases}) != len(cases):
        raise ValueError('duplicate holdout case ID')
    return cases


class HoldoutCase(DomainModel):
    case_id: NonEmptyString
    target_station: NonEmptyString
    upstream_stations: tuple[NonEmptyString, ...]
    topology_source: NonEmptyString
    topology_digest: NonEmptyString
    window: TimeRange
    truth_kind: Literal['instant', 'available_readings_mean']
    instant: datetime | None = None
    expected_cadence_seconds: int | None = None
    collections_complete: bool
    retrieved_at: datetime
    source_urls: tuple[NonEmptyString, ...]
    observations: tuple[WaterObservationRecord | DischargeObservationRecord, ...]
    limitations: tuple[NonEmptyString, ...]

    @model_validator(mode='after')
    def validate_case(self):
        if not self.upstream_stations or self.target_station in self.upstream_stations:
            raise ValueError('holdout needs distinct, verified upstream stations')
        if self.retrieved_at.tzinfo is None or self.window.end <= self.window.start:
            raise ValueError('holdout requires aware timestamps and a nonempty window')
        if len({r.record_id for r in self.observations}) != len(self.observations):
            raise ValueError('duplicate observation identity')
        if not self.heldout:
            raise ValueError('holdout has no target observations')
        if self.truth_kind == 'instant' and (self.instant is None or self.instant.tzinfo is None
                or len(self.heldout) != 1 or self.heldout[0].observed_time != self.instant):
            raise ValueError('instant truth requires exactly one matching measurement')
        if self.truth_kind == 'available_readings_mean' and self.instant is not None:
            raise ValueError('window truth cannot be labelled an instant')
        if any(r.location.station_id not in {self.target_station, *self.upstream_stations}
               or r.observed_time is None or not self.window.contains(r.observed_time) for r in self.observations):
            raise ValueError('observation is outside the declared site/window')
        return self

    @property
    def heldout(self):
        return tuple(r for r in self.observations if r.location.station_id == self.target_station
                     and r.parameter.code_system == 'usgs' and r.parameter.code in PH_CODES)

    @property
    def visible(self):
        removed = {r.record_id for r in self.heldout}
        return tuple(r for r in self.observations if r.record_id not in removed)

    @property
    def truth(self):
        if self.truth_kind == 'instant':
            return Decimal(self.heldout[0].value)
        registry = load_parameter_capability_registry()
        observations = tuple(build_policy_observation(registry, r,
            SourceRecordReference(item_id='holdout-source', record_id=r.record_id),
            provider_name=ParameterProvider.WDFN_TIME_SERIES.value) for r in self.heldout)
        if any(r is None for r in observations):
            raise ValueError('target observations are not supported by the production policy')
        outcome = aggregate_observations(registry, registry.get(self.heldout[0].parameter), observations,
            self.window, EvidenceAggregationMethod.MEAN, collection_complete=self.collections_complete,
            expected_cadence_seconds=self.expected_cadence_seconds)
        return Decimal(outcome.payload.value)


class EvidenceArtifact(DomainModel):
    artifact_id: NonEmptyString
    kind: Literal['raw', 'aggregate', 'other_waterbody', 'history_rag', 'cache', 'context', 'guideline_example']
    observation_refs: tuple[NonEmptyString, ...] = ()
    input_refs: tuple[NonEmptyString, ...] = ()
    text: str = ''


def audit_inputs(case: HoldoutCase, artifacts: tuple[EvidenceArtifact, ...], *, sealed_channels, question=''):
    if set(sealed_channels) != SEALED_CHANNELS:
        raise ValueError('all unbounded acquisition/history/cache/context channels must be sealed')
    known = {r.record_id: r for r in case.visible}
    forbidden = {r.record_id for r in case.heldout}
    nodes = {a.artifact_id: a for a in artifacts}
    if len(nodes) != len(artifacts) or forbidden & set(nodes):
        raise ValueError('duplicate artifact or heldout identity')
    visited, active = {}, set()
    target_values = {Decimal(r.value) for r in case.heldout} | {case.truth}

    def origins(key):
        if key in active:
            raise ValueError('cyclic evidence lineage')
        if key in visited:
            return visited[key]
        if key not in nodes:
            raise ValueError('unknown evidence lineage')
        active.add(key)
        node = nodes[key]
        refs = set(node.observation_refs)
        if forbidden & refs or not refs <= known.keys():
            raise ValueError('heldout or unknown observation in evidence lineage')
        for parent in node.input_refs:
            refs.update(origins(parent))
        if not refs and node.kind not in {'context', 'guideline_example'}:
            raise ValueError('numeric evidence requires observation lineage')
        permitted_values = {Decimal(known[ref].value) for ref in refs}
        forbidden_values = target_values - permitted_values
        if any(Decimal(n) in forbidden_values for n in re.findall(r'(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])', node.text)):
            raise ValueError('possible target value in free-text context or example')
        active.remove(key)
        visited[key] = refs
        return refs

    for key in nodes:
        origins(key)
    if any(Decimal(n) in target_values
           for n in re.findall(r'(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])', question)):
        raise ValueError('question contains a heldout value')
    return digest({'observations': [r.model_dump(mode='json') for r in sorted(case.visible, key=lambda r: r.record_id)],
                   'artifacts': [a.model_dump(mode='json') for a in sorted(artifacts, key=lambda a: a.artifact_id)], 'question': question,
                   'sealed_channels': sorted(sealed_channels), 'window': case.window.model_dump(mode='json')})


def prepared_inputs(case: HoldoutCase):
    visible = sorted(case.visible, key=lambda r: r.record_id)
    artifacts = tuple(EvidenceArtifact(artifact_id=r.record_id, kind='raw', observation_refs=(r.record_id,)) for r in visible)
    fingerprint = audit_inputs(case, artifacts, sealed_channels=SEALED_CHANNELS)
    return {'case_id': case.case_id, 'input_digest': fingerprint, 'window': case.window.model_dump(mode='json'),
            'observations': [r.model_dump(mode='json') for r in visible],
            'artifacts': [a.model_dump(mode='json') for a in artifacts], 'sealed_channels': sorted(SEALED_CHANNELS)}


class Prediction(DomainModel):
    status: Literal['point', 'range', 'indeterminate', 'failed']
    truth_kind: Literal['instant', 'available_readings_mean']
    window: TimeRange
    instant: datetime | None = None
    value: Decimal | None = None
    lower: Decimal | None = None
    upper: Decimal | None = None

    @model_validator(mode='after')
    def finite_and_consistent(self):
        if any(v is not None and not v.is_finite() for v in (self.value, self.lower, self.upper)):
            raise ValueError('predictions must be finite')
        if (self.status == 'point') != (self.value is not None):
            raise ValueError('only a point prediction has a point value')
        if (self.lower is None) != (self.upper is None) or (self.lower is not None and self.lower > self.upper):
            raise ValueError('invalid range bounds')
        if self.status == 'range' and self.lower is None:
            raise ValueError('range prediction requires bounds')
        if self.status in {'failed', 'indeterminate'} and self.lower is not None:
            raise ValueError('failed or indeterminate prediction cannot carry a range')
        if self.value is not None and self.lower is not None and not self.lower <= self.value <= self.upper:
            raise ValueError('point is outside its bounds')
        return self


def score_prediction(case: HoldoutCase, prediction: Prediction, *, has_eligible_same_indicator_anchor: bool):
    if type(has_eligible_same_indicator_anchor) is not bool:
        raise ValueError('anchor eligibility must be an explicit boolean')
    if has_eligible_same_indicator_anchor and not any(r.parameter == case.heldout[0].parameter and
            r.location.station_id in case.upstream_stations for r in case.visible):
        raise ValueError('declared anchor is absent from the frozen visible observations')
    if prediction.window != case.window or prediction.truth_kind != case.truth_kind or prediction.instant != case.instant:
        return {'status': 'not_comparable', 'reason': 'time_or_statistic_mismatch'}
    if prediction.status == 'point' and not has_eligible_same_indicator_anchor:
        raise ValueError('point estimate cannot bypass the production same-indicator anchor gate')
    truth = case.truth
    return {'status': prediction.status, 'truth_kind': case.truth_kind, 'truth': str(truth),
            'absolute_ph_error': None if prediction.value is None else str(abs(prediction.value - truth)),
            'range_width': None if prediction.lower is None else str(prediction.upper - prediction.lower),
            'range_covers_truth': None if prediction.lower is None else prediction.lower <= truth <= prediction.upper,
            'comparison_limit': 'available sampled readings only; not continuous truth throughout the window'
            if case.truth_kind == 'available_readings_mean' else 'one observed instant only'}

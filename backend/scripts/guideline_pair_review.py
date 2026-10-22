"""Audit saved pair evidence; this never manufactures runs or model citations."""
import json
import re
from decimal import Decimal
from uuid import UUID

from pydantic import Field

from app.application.guidelines.transcript_models import GuidelineDeliveryV1, GuidelineUsageV1
from app.application.reasoning.transcript_models import WaterbodyModelRequestV1
from app.domain.common import DomainModel, NonEmptyString
from app.domain.guidelines.canonical import digest
from scripts.guideline_holdout import EvidenceArtifact, HoldoutCase, Prediction, SEALED_CHANNELS, audit_inputs, score_prediction
from app.application.evidence_tools.collection_service import build_policy_observation
from app.domain.evidence.source_record_reference import SourceRecordReference
from app.domain.hydrology.parameter_provider import ParameterProvider
from app.infrastructure.config import load_parameter_capability_registry


class RecordedArm(DomainModel):
    run_id: UUID
    session_id: UUID
    provider: NonEmptyString
    model: NonEmptyString
    model_version: NonEmptyString
    configuration: dict
    input_digest: NonEmptyString
    artifacts: tuple[EvidenceArtifact, ...]
    sealed_channels: tuple[str, ...]
    requests: tuple[WaterbodyModelRequestV1, ...] = Field(min_length=1)
    deliveries: dict[UUID, GuidelineDeliveryV1]
    usages: tuple[GuidelineUsageV1, ...]
    prediction: Prediction
    eligible_anchor_refs: tuple[str, ...]
    explanation: str
    explanation_review: str | None = None


def scientific_request(request):
    def normalize(value):
        if isinstance(value, dict):
            return {k: normalize(v) for k, v in value.items() if k != 'advisory_knowledge'}
        if isinstance(value, list):
            return [normalize(v) for v in value]
        if isinstance(value, str):
            try:
                UUID(value)
                return '<local-reference>'
            except ValueError:
                pass
            try:
                decoded = json.loads(value)
            except ValueError:
                return value
            if isinstance(decoded, (dict, list)):
                return normalize(decoded)
        return value
    messages = [normalize(message.text) for message in request.messages]
    return {'phase': request.phase.value, 'system_prompt': request.system_prompt,
            'messages': [m for m in messages if m != {}]}


def review_pair(case: HoldoutCase, control: RecordedArm, guided: RecordedArm, approved_versions):
    if control.run_id == guided.run_id:
        raise ValueError('paired arms require distinct actual run IDs')
    for name in ('provider', 'model', 'model_version', 'configuration'):
        if getattr(control, name) != getattr(guided, name):
            raise ValueError('paired model/version/budget configuration differs')
    if control.configuration.get('memory_mode') != 'stateless' or control.configuration.get('evidence_mode') != 'frozen':
        raise ValueError('pair inputs require stateless, frozen evidence')
    if [scientific_request(r) for r in control.requests] != [scientific_request(r) for r in guided.requests]:
        raise ValueError('submitted scientific context or call sequence differs; cannot isolate guideline effect')
    scores = []
    forbidden = {r.record_id for r in case.heldout}
    visible = {r.record_id: r for r in case.visible}
    approved = {v.version_id: v for v in approved_versions}
    forbidden_values = ({Decimal(r.value) for r in case.heldout} | {case.truth}) - {Decimal(r.value) for r in case.visible}
    for arm, with_guideline in ((control, False), (guided, True)):
        fingerprint = audit_inputs(case, arm.artifacts, sealed_channels=arm.sealed_channels)
        if fingerprint != arm.input_digest or arm.input_digest != control.input_digest:
            raise ValueError('paired evidence snapshot differs or contains leakage')
        if set(arm.sealed_channels) != SEALED_CHANNELS:
            raise ValueError('unsealed input channel')
        for ref in arm.eligible_anchor_refs:
            if ref not in visible or visible[ref].parameter != case.heldout[0].parameter or visible[ref].location.station_id not in case.upstream_stations:
                raise ValueError('anchor is not a visible upstream same-indicator observation')
            checked = build_policy_observation(load_parameter_capability_registry(), visible[ref],
                SourceRecordReference(item_id='pair-source', record_id=ref), provider_name=ParameterProvider.WDFN_TIME_SERIES.value)
            if checked is None or checked.quality.quality_class.value == 'invalid':
                raise ValueError('invalid observation cannot be declared an eligible anchor')
        requests = {r.operation_id: r for r in arm.requests}
        if len(requests) != len(arm.requests):
            raise ValueError('duplicate model operation in saved pair')
        by_operation = {}
        provided, used = set(), set()
        for item_id, delivery in arm.deliveries.items():
            if delivery.operation_id in by_operation or delivery.run_id != arm.run_id or delivery.session_id != arm.session_id:
                raise ValueError('delivery ownership/operation mismatch')
            by_operation[delivery.operation_id] = delivery
            request = requests.get(delivery.operation_id)
            if request is None or request.phase.value != delivery.phase:
                raise ValueError('delivery has no matching actual model request')
            for entry in delivery.entries:
                if approved.get(entry.version.version_id) != entry.version:
                    raise ValueError('provided version is not exactly the approved saved version')
                provided.add(str(entry.version.version_id))
        for request in arm.requests:
            if request.model.provider != arm.provider or request.model.model != arm.model or request.evidence_bundle_ref.run_id != arm.run_id:
                raise ValueError('request model/run differs from pair declaration')
            serialized = request.model_dump_json()
            if any(ref in serialized for ref in forbidden):
                raise ValueError('target observation reappeared in the submitted model payload')
            for message in request.messages:
                numbers = re.findall(r'(?<![\w.])-?\d+(?:\.\d+)?(?:e[+-]?\d+)?(?![\w.])', message.text, re.IGNORECASE)
                if any(Decimal(number) in forbidden_values for number in numbers):
                    raise ValueError('possible heldout value/aggregate in the submitted payload; inspect provenance')
            entries = []
            for message in request.messages:
                try:
                    value = json.loads(message.text)
                except ValueError:
                    continue
                if isinstance(value, dict) and 'advisory_knowledge' in value:
                    if not isinstance(value['advisory_knowledge'], list):
                        raise ValueError('malformed advisory block')
                    entries.extend(value['advisory_knowledge'])
            delivery = by_operation.get(request.operation_id)
            expected = [] if delivery is None else [{'alias': e.alias, **e.version.model_view.model_dump(mode='json')} for e in delivery.entries]
            if entries != expected:
                raise ValueError('constructed advisory and saved actual delivery differ')
            if not with_guideline and any(v.model_view.body in request.system_prompt or any(v.model_view.body in m.text for m in request.messages) for v in approved.values()):
                raise ValueError('control contains approved guideline text outside the advisory block')
        for usage in arm.usages:
            if usage.run_id != arm.run_id or usage.session_id != arm.session_id:
                raise ValueError('usage ownership mismatch')
            for citation in usage.citations:
                delivery = arm.deliveries.get(citation.delivery_ref.item_id)
                if delivery is None or delivery.operation_id != usage.operation_id or delivery.phase != usage.phase or not any(
                        e.version.version_id == citation.version_id and e.version.identity.guideline_id == citation.guideline_id
                        for e in delivery.entries):
                    raise ValueError('usage has no valid saved delivery')
                used.add(str(citation.version_id))
        if with_guideline != bool(provided):
            raise ValueError('control must provide no guideline; guided arm must provide approved knowledge')
        scores.append({'run_id': str(arm.run_id), 'provided': sorted(provided), 'explicitly_used': sorted(used),
            'explanation': arm.explanation, 'explanation_review': arm.explanation_review,
            **score_prediction(case, arm.prediction, has_eligible_same_indicator_anchor=bool(arm.eligible_anchor_refs))})
    return {'case_id': case.case_id, 'configuration_digest': digest(control.configuration), 'input_digest': control.input_digest,
            'control': scores[0], 'guided': scores[1],
            'limit': 'saved manifest audit only; collection/replay and explanation quality still require recorded evidence/human review'}

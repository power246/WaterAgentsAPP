import json
from uuid import UUID

from app.application.canonical_json import canonical_json_digest
from app.application.guidelines.transcript_models import (
    GuidelineDeliveryV1, GuidelineRetrievalV1, GuidelineRunContextV1,
    GuidelineUsageV1, ResultGuidelineUsageV1, validate_guideline_identity,
)
from app.application.reasoning.transcript_models import WaterbodyModelRequestV1, WaterbodyModelResponseV1
from app.domain import InferenceResult
from app.domain.guidelines import GuidelineUsageTarget


def result_targets(result):
    targets = {GuidelineUsageTarget(kind="overall", key="overall")}
    for series in result.series:
        prefix = f"cell:{series.parameter.key.code_system}:{series.parameter.key.code}:"
        for point in series.points:
            if not point.cell_id.startswith(prefix):
                raise ValueError("result cell identity mismatch")
            window_id = point.cell_id[len(prefix):]
            targets.add(GuidelineUsageTarget(kind="cell", cell_id=point.cell_id, window_id=window_id))
            targets.add(GuidelineUsageTarget(kind="window", window_id=window_id))
    targets.update(GuidelineUsageTarget(kind="analysis_step", step_index=index)
                   for index in range(len(result.analysis_detail.steps)))
    return targets


def validate_guideline_links(body, item, lookup, *, strict_missing=True):
    """All knowledge edges point backwards to typed records in the same run."""
    validate_guideline_identity(body, item)

    def resolve(item_id, expected, session_id):
        linked = lookup(item_id)
        if linked is None:
            if strict_missing:
                raise ValueError("guideline reference is missing")
            return None
        if (not isinstance(linked.body, expected) or linked.item.run_id != item.run_id
            or linked.item.session_id != session_id or linked.item.transcript_sequence >= item.transcript_sequence):
            raise ValueError("guideline reference ownership/type/order mismatch")
        if isinstance(linked.body, (WaterbodyModelRequestV1, WaterbodyModelResponseV1)) and linked.item.operation_id != linked.body.operation_id:
            raise ValueError("guideline model record operation mismatch")
        validate_guideline_links(linked.body, linked.item, lookup, strict_missing=strict_missing)
        return linked.body

    if isinstance(body, GuidelineRetrievalV1):
        pin = resolve(body.run_context_ref.item_id, GuidelineRunContextV1, None)
        if pin is not None and pin.status != "ready" and body.selection.matches:
            raise ValueError("unavailable guideline run cannot retrieve versions")
    elif isinstance(body, GuidelineDeliveryV1):
        resolve(body.run_context_ref.item_id, GuidelineRunContextV1, None)
        retrieval = resolve(body.retrieval_ref.item_id, GuidelineRetrievalV1, body.session_id)
        request = resolve(body.request_ref.item_id, WaterbodyModelRequestV1, body.session_id)
        if retrieval is not None:
            selected = {match.version.version_id: match.version for match in retrieval.selection.matches}
            if retrieval.run_context_ref != body.run_context_ref or any(
                selected.get(entry.version.version_id) != entry.version for entry in body.entries):
                raise ValueError("delivery version is outside the pinned retrieval")
        expected = [{"alias": entry.alias, **entry.version.model_view.model_dump(mode="json")} for entry in body.entries]
        if canonical_json_digest(expected) != body.advisory_digest:
            raise ValueError("guideline delivery digest mismatch")
        if request is not None:
            blocks = []
            for message in request.messages:
                if message.role.value != "user":
                    continue
                try:
                    payload = json.loads(message.text)
                except ValueError:
                    continue
                if isinstance(payload, dict) and "advisory_knowledge" in payload:
                    blocks.append(payload["advisory_knowledge"])
            if (request.operation_id != body.operation_id or request.phase.value != body.phase
                or blocks != ([expected] if expected else [])):
                raise ValueError("guideline delivery differs from the actual request")
    elif isinstance(body, GuidelineUsageV1):
        response = resolve(body.model_response_ref.item_id, WaterbodyModelResponseV1, body.session_id)
        if response is not None and (response.operation_id != body.operation_id or response.phase.value != body.phase
                                     or response.finish_reason.value != "completed"):
            raise ValueError("guideline usage requires its completed response")
        for citation in body.citations:
            delivery = resolve(citation.delivery_ref.item_id, GuidelineDeliveryV1, body.session_id)
            if delivery is None:
                continue
            version = next((entry.version for entry in delivery.entries if entry.version.version_id == citation.version_id), None)
            if (delivery.operation_id != citation.operation_id or delivery.phase != citation.phase or version is None
                or version.identity.guideline_id != citation.guideline_id or version.identity.family_root_id != citation.family_root_id
                or version.identity.origin != citation.origin or version.content.source_kind != citation.source_kind):
                raise ValueError("guideline citation was not supplied to this operation")
            if body.target.kind == "numeric_task":
                request = resolve(delivery.request_ref.item_id, WaterbodyModelRequestV1, body.session_id)
                if request is not None:
                    task = json.loads(request.messages[0].text)["task"]
                    if task["task_id"] != body.target.task_id or task["window_id"] != body.target.window_id:
                        raise ValueError("guideline numeric task/window mismatch")
    elif isinstance(body, ResultGuidelineUsageV1):
        for binding in body.bindings:
            for item_id in binding.usage_item_ids:
                usage = resolve(item_id, GuidelineUsageV1, body.session_id)
                if usage is not None and (usage.target != binding.target
                    and not (usage.target.kind == "numeric_task" and binding.target.kind == "cell")):
                    raise ValueError("result guideline target differs from operation usage")
    elif isinstance(body, InferenceResult):
        linked_items = [lookup(UUID(ref)) for ref in body.analysis_detail.transcript_item_refs]
        if not any(entry is not None and isinstance(entry.body, ResultGuidelineUsageV1) for entry in linked_items):
            return
        allowed = result_targets(body)
        fragments = {entry.body.evidence_id: entry.body for entry in linked_items
                     if entry is not None and hasattr(entry.body, "evidence_id") and hasattr(entry.body, "payload")}
        points = {point.cell_id: point for series in body.series for point in series.points}
        for entry in linked_items:
            if entry is None or not isinstance(entry.body, ResultGuidelineUsageV1):
                continue
            usage = resolve(entry.item.item_id, ResultGuidelineUsageV1, body.session_id)
            if usage.result_id != body.result_id or any(binding.target not in allowed for binding in usage.bindings):
                raise ValueError("guideline result identity/target mismatch")
            for binding in usage.bindings:
                for usage_id in binding.usage_item_ids:
                    operation = resolve(usage_id, GuidelineUsageV1, body.session_id)
                    if operation is None or operation.target.kind != "numeric_task":
                        continue
                    pending, seen, origins = list(points[binding.target.cell_id].evidence_refs), set(), set()
                    while pending:
                        ref = pending.pop()
                        if ref in seen or ref not in fragments:
                            continue
                        seen.add(ref)
                        payload = fragments[ref].payload
                        origins.add((getattr(payload, "model_operation_id", None), getattr(payload, "task_id", None)))
                        pending.extend(ref.evidence_id for ref in getattr(payload, "input_evidence_refs", ()))
                        converted = getattr(payload, "input_evidence_ref", None)
                        if converted is not None:
                            pending.append(converted.evidence_id)
                    if (str(operation.operation_id), operation.target.task_id) not in origins:
                        raise ValueError("guideline numeric usage is outside selected result lineage")

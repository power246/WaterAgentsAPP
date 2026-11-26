import asyncio

from app.application.analysis_runs.workflow import WorkflowFailed, WorkflowStopped
from app.application.canonical_json import canonical_json_digest
from app.application.guidelines.transcript_models import (
    GuidelineDeliveredVersion, GuidelineDeliveryV1, GuidelineRetrievalV1,
    GuidelineRunContextV1, GuidelineUsageV1, ResultGuidelineUsageV1,
)
from app.application.transcripts.models import TranscriptContentRef
from app.domain.guidelines import GuidelineCitationV1, GuidelineDeliveryRef


class GuidelineTranscriptWriteError(WorkflowFailed):
    def __init__(self):
        super().__init__("Guideline transcript could not be recorded.", code="guideline_transcript_write_failed",
            public_message="The analysis reference records could not be saved.", failed_stage="transcript_write", retryable=False)


async def write_run_context(operations, pin):
    try:
        identity = await operations.reserve_transcript_item(pin.run_id)
        await operations.finalize_transcript_item(pin.run_id, identity.item_id, "guideline_run_context", 1,
                                                 GuidelineRunContextV1.from_pin(pin))
        return TranscriptContentRef(run_id=pin.run_id, item_id=identity.item_id)
    except (asyncio.CancelledError, WorkflowStopped):
        raise
    except Exception as error:
        raise GuidelineTranscriptWriteError() from error


class GuidelineTranscript:
    """Knowledge records use the existing reasoning store and workflow writer."""

    def __init__(self, context, store):
        self.context = context
        self.store = store
        self.retrieval_ref = None
        self.run_context_ref = None
        self.deliveries = {}
        self.usage_refs = []
        self._retrieval_lock = asyncio.Lock()

    async def _write(self, content_type, body):
        try:
            return await self.store.write_guideline_record(content_type, body)
        except (asyncio.CancelledError, WorkflowStopped):
            raise
        except Exception as error:
            raise GuidelineTranscriptWriteError() from error

    async def retrieved(self, selection):
        async with self._retrieval_lock:
            if self.retrieval_ref is not None:
                return
            session = self.context.session
            self.run_context_ref = await session.run_context.save(self.store._operations)
            query = session._query
            self.retrieval_ref = await self._write("guideline_retrieval", GuidelineRetrievalV1(
                run_id=session.pin.run_id, session_id=session.session_id, run_context_ref=self.run_context_ref,
                parameter_keys=query.parameter_keys if query is not None else (), selection=selection))

    async def submitted(self, request, request_ref):
        operation = self.context.operations[request.operation_id]
        entries = tuple(GuidelineDeliveredVersion(alias=alias, version=version) for alias, version in operation.aliases)
        block = [{"alias": entry.alias, **entry.version.model_view.model_dump(mode="json")} for entry in entries]
        reference = await self._write("guideline_delivery", GuidelineDeliveryV1(
            run_id=self.store.run_id, session_id=self.store.session_id, operation_id=request.operation_id,
            phase=request.phase.value, run_context_ref=self.run_context_ref, retrieval_ref=self.retrieval_ref,
            request_ref=request_ref, selection_status=operation.status, advisory_digest=canonical_json_digest(block), entries=entries))
        self.deliveries[request.operation_id] = reference

    async def used(self, usage):
        delivery = self.deliveries[usage.operation_id]
        response = self.store.response_reference(usage.operation_id)
        citations = tuple(GuidelineCitationV1(delivery_ref=GuidelineDeliveryRef(
            run_id=usage.run_id, session_id=usage.session_id, item_id=delivery.item_id),
            guideline_id=version.identity.guideline_id, family_root_id=version.identity.family_root_id,
            version_id=version.version_id, origin=version.identity.origin, source_kind=version.content.source_kind,
            operation_id=usage.operation_id, phase=usage.phase) for version in usage.versions)
        ref = await self._write("guideline_usage", GuidelineUsageV1(run_id=usage.run_id, session_id=usage.session_id,
            operation_id=usage.operation_id, phase=usage.phase, model_response_ref=response, target=usage.target, citations=citations))
        self.usage_refs.append((usage, ref))

    async def result_usage(self, result, bindings):
        if not bindings:
            return None
        return await self._write("result_guideline_usage", ResultGuidelineUsageV1(
            run_id=result.run_id, session_id=result.session_id, result_id=result.result_id, bindings=bindings))

import json
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError

from app.application.analysis_runs.models import (
    LifecycleEvent,
    PublicError,
    RunCompletedEvent,
    RunFailedEvent,
    SessionCompletedEvent,
    SessionFailedEvent,
)
from app.application.query_understanding.context_models import AnalysisContext
from app.application.query_understanding.parser_models import (
    QueryParserInputV1,
    QueryParserMessageV1,
    finalize_query_parser_message,
    query_parser_message_projection,
)
from app.application.transcripts.models import (
    AnalysisRequestV1,
    ContentVisibility,
    GenerationStatus,
    LateMessageOutputFact,
    LegacyLifecycleEventV1,
    MessageEndReason,
    MessageProjection,
    ParseStatus,
    ReadTranscriptItem,
    ReferenceResolution,
    ReferenceResolutionStatus,
    RegisteredContent,
    RuntimeFact,
    StoredTranscriptItem,
    SuppressedTranscriptItemFact,
    TranscriptContentRef,
    TranscriptMetadata,
)
from app.application.transcripts.redaction import redact, redact_tags
from app.domain.attribution.analysis_detail import AnalysisDetail
from app.domain.common import JsonValue
from app.domain.domain_model import DomainModel
from app.domain.evidence.evidence_bundle import EvidenceBundle
from app.domain.evidence.evidence_fragment import EvidenceFragment
from app.domain.evidence.evidence_source_records import EvidenceSourceRecords
from app.domain.evidence.evidence_tool_call import EvidenceToolCall
from app.domain.evidence.evidence_tool_result import EvidenceToolResult
from app.domain.hydrology import HydrologyGraph
from app.domain.inference import InferenceResult


class UnsupportedTranscriptContentError(ValueError):
    pass


ReferenceExtractor = Callable[[object], tuple[UUID, ...]]
MetadataExtractor = Callable[[object], TranscriptMetadata]
VisibilityExtractor = Callable[[object], ContentVisibility]
MessageAdapter = Callable[[object], MessageProjection]
MessageFinalizer = Callable[..., object]
ErrorExtractor = Callable[[object], tuple[UUID, ...]]
PublicProjection = Callable[[object], JsonValue]


def _serialize_safe_json(
    value: object,
    private_values: tuple[str, ...] = (),
) -> str:
    return json.dumps(
        redact(value, private_values),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def _no_references(body: object) -> tuple[UUID, ...]:
    del body
    return ()


def _default_metadata(body: object) -> TranscriptMetadata:
    del body
    return TranscriptMetadata()


def _auto_visibility(body: object) -> ContentVisibility:
    del body
    return ContentVisibility.AUTO


@dataclass(frozen=True, slots=True)
class ContentRegistration:
    content_type: str
    content_schema_version: int
    adapter: TypeAdapter
    references: ReferenceExtractor = _no_references
    metadata: MetadataExtractor = _default_metadata
    visibility: VisibilityExtractor = _auto_visibility
    message_adapter: MessageAdapter | None = None
    message_finalizer: MessageFinalizer | None = None
    errors: ErrorExtractor = _no_references
    public_projection: PublicProjection | None = None
    history_only_possible: bool = False
    project_full_content: bool = False
    writable: bool = True
    preserve_unset: bool = False

    @property
    def key(self) -> tuple[str, int]:
        return self.content_type, self.content_schema_version


_SENSITIVE_SUMMARY_CONTENT_TYPES = frozenset((
    "run_summary_model_request",
    "run_summary_model_response",
))


def _summary_public_body(
    content_type: str,
    registration: ContentRegistration | None,
    safe_body: str,
) -> str:
    if registration is not None and registration.public_projection is not None:
        try:
            body = registration.adapter.validate_json(safe_body)
            return _serialize_safe_json(registration.public_projection(body))
        except (ValidationError, TypeError, ValueError):
            pass

    # A damaged or future schema must not expose prompts or provider output.
    raw = json.loads(safe_body)
    value: dict[str, JsonValue] = {}
    if isinstance(raw, dict):
        version = raw.get("schema_version")
        if type(version) is int and version > 0:
            value["schema_version"] = version
        operation_id = raw.get("operation_id")
        if isinstance(operation_id, str):
            try:
                value["operation_id"] = str(UUID(operation_id))
            except ValueError:
                pass
        if content_type == "run_summary_model_response":
            value["structured_output_present"] = raw.get("structured_output") is not None
            value["visible_text_present"] = bool(raw.get("visible_text"))
    return _serialize_safe_json(value)


class TranscriptRegistry:
    def __init__(self) -> None:
        self._registrations: dict[tuple[str, int], ContentRegistration] = {}

    @property
    def registered_keys(self) -> tuple[tuple[str, int], ...]:
        return tuple(self._registrations)

    @property
    def writable_keys(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            key for key, registration in self._registrations.items()
            if registration.writable
        )

    def register(self, registration: ContentRegistration) -> None:
        if not registration.content_type.strip():
            raise ValueError("content_type must not be blank")
        if (
            type(registration.content_schema_version) is not int
            or registration.content_schema_version <= 0
        ):
            raise ValueError("content_schema_version must be positive")
        if registration.key in self._registrations:
            raise ValueError(f"duplicate transcript registration: {registration.key!r}")
        if (registration.message_adapter is None) != (
            registration.message_finalizer is None
        ):
            raise ValueError(
                "message registrations require both an adapter and a finalizer"
            )
        self._registrations[registration.key] = registration

    def copy_with(self, registration: ContentRegistration) -> "TranscriptRegistry":
        copied = TranscriptRegistry()
        copied._registrations.update(self._registrations)
        copied.register(registration)
        return copied

    def encode(
        self,
        content_type: str,
        content_schema_version: int,
        body: object,
        *,
        private_values: tuple[str, ...] = (),
    ) -> RegisteredContent:
        registration = self._registrations.get((content_type, content_schema_version))
        if registration is None or not registration.writable:
            raise UnsupportedTranscriptContentError(
                f"unsupported transcript write: {content_type} v{content_schema_version}"
            )
        validated = registration.adapter.validate_python(body)
        serialized = registration.adapter.dump_python(
            validated,
            mode="json",
            exclude_unset=registration.preserve_unset,
        )
        safe_body = _serialize_safe_json(serialized, private_values)
        safe_model = registration.adapter.validate_json(safe_body)
        references = registration.references(safe_model)
        if len(references) != len(set(references)):
            raise ValueError("registered reference extractor returned duplicates")
        metadata = registration.metadata(safe_model)
        metadata = metadata.model_copy(
            update={"tags": redact_tags(metadata.tags, private_values)},
        )
        return RegisteredContent(
            content_type=content_type,
            content_schema_version=content_schema_version,
            safe_body=safe_body,
            content_bytes=len(safe_body.encode("utf-8")),
            references=references,
            metadata=metadata,
            visibility=registration.visibility(safe_model),
        )

    def safe_read_body(
        self,
        _content_type: str,
        _content_schema_version: int,
        body: str,
    ) -> str:
        return _serialize_safe_json(json.loads(body))

    def parse(
        self,
        item: StoredTranscriptItem,
        safe_body: str,
        *,
        error_id_factory: Callable[[], UUID] = uuid4,
    ) -> ReadTranscriptItem:
        registration = self._registrations.get(
            (item.content_type, item.content_schema_version)
        )
        content_ref = TranscriptContentRef(run_id=item.run_id, item_id=item.item_id)
        if registration is None:
            return ReadTranscriptItem(
                item=item,
                content_ref=content_ref,
                parse_status=ParseStatus.UNSUPPORTED,
                parse_error=PublicError(
                    error_id=error_id_factory(),
                    code="transcript_content_unsupported",
                    message="This transcript content type or version is not supported.",
                    failed_stage="transcript_read",
                    retryable=False,
                ),
            )
        if len(safe_body.encode("utf-8")) != item.content_bytes:
            return ReadTranscriptItem(
                item=item,
                content_ref=content_ref,
                parse_status=ParseStatus.CORRUPT,
                parse_error=PublicError(
                    error_id=error_id_factory(),
                    code="transcript_content_corrupt",
                    message="This transcript item contains invalid content.",
                    failed_stage="transcript_read",
                    retryable=False,
                ),
            )
        try:
            body = registration.adapter.validate_json(safe_body)
        except (ValidationError, ValueError, TypeError):
            return ReadTranscriptItem(
                item=item,
                content_ref=content_ref,
                parse_status=ParseStatus.CORRUPT,
                parse_error=PublicError(
                    error_id=error_id_factory(),
                    code="transcript_content_corrupt",
                    message="This transcript item contains invalid content.",
                    failed_stage="transcript_read",
                    retryable=False,
                ),
            )
        if not isinstance(body, DomainModel):
            raise TypeError("registered transcript adapters must return DomainModel values")
        return ReadTranscriptItem(
            item=item,
            content_ref=content_ref,
            parse_status=ParseStatus.OK,
            body=body,
        )

    def recheck(
        self,
        content: RegisteredContent,
        *,
        private_values: tuple[str, ...] = (),
    ) -> RegisteredContent:
        registration = self._registrations.get(
            (content.content_type, content.content_schema_version)
        )
        if registration is None or not registration.writable:
            raise UnsupportedTranscriptContentError(
                "stored transcript content is not writable by this registry"
            )
        body = registration.adapter.validate_json(content.safe_body)
        checked = self.encode(
            content.content_type,
            content.content_schema_version,
            body,
            private_values=private_values,
        )
        if checked.safe_body != content.safe_body:
            raise ValueError("persistence redaction differs from the readable live body")
        metadata = content.metadata.model_copy(
            update={
                "tags": redact_tags(content.metadata.tags, private_values),
            },
        )
        if metadata != content.metadata:
            raise ValueError(
                "persistence redaction differs from the readable live metadata"
            )
        return checked.model_copy(update={"metadata": metadata})

    def message_projection(
        self,
        content_type: str,
        content_schema_version: int,
        safe_body: str,
    ) -> MessageProjection:
        registration = self._registrations.get((content_type, content_schema_version))
        if registration is None or registration.message_adapter is None:
            raise UnsupportedTranscriptContentError(
                f"transcript type is not a message: {content_type} v{content_schema_version}"
            )
        body = registration.adapter.validate_json(safe_body)
        return registration.message_adapter(body)

    def finalized_message_body(
        self,
        content_type: str,
        content_schema_version: int,
        text: str,
        generation_status: GenerationStatus,
        end_reason: MessageEndReason | None,
        base_safe_body: str | None = None,
    ) -> object:
        registration = self._registrations.get((content_type, content_schema_version))
        if registration is None or registration.message_finalizer is None:
            raise UnsupportedTranscriptContentError(
                f"transcript type is not a message: {content_type} v{content_schema_version}"
            )
        base_body = (
            registration.adapter.validate_json(base_safe_body)
            if base_safe_body is not None
            else None
        )
        if base_body is None:
            return registration.message_finalizer(
                text,
                generation_status,
                end_reason,
            )
        return registration.message_finalizer(
            text,
            generation_status,
            end_reason,
            base_body,
        )

    def is_message_type(
        self,
        content_type: str,
        content_schema_version: int,
    ) -> bool:
        registration = self._registrations.get((content_type, content_schema_version))
        return registration is not None and registration.message_adapter is not None

    def error_ids(self, content: RegisteredContent) -> tuple[UUID, ...]:
        registration = self._registrations.get(
            (content.content_type, content.content_schema_version)
        )
        if registration is None:
            raise UnsupportedTranscriptContentError(
                "stored transcript content is not registered"
            )
        body = registration.adapter.validate_json(content.safe_body)
        return registration.errors(body)

    def public_body(self, item: ReadTranscriptItem) -> JsonValue:
        registration = self._registrations.get(
            (item.item.content_type, item.item.content_schema_version)
        )
        if registration is None or item.body is None:
            raise UnsupportedTranscriptContentError(
                "unparsed transcript content has no public typed body"
            )
        body = registration.adapter.dump_python(
            item.body,
            mode="json",
            exclude_unset=registration.preserve_unset,
        )
        if registration.public_projection is None:
            return body
        return registration.public_projection(item.body)

    def public_safe_body(
        self,
        content_type: str,
        content_schema_version: int,
        safe_body: str,
    ) -> str:
        registration = self._registrations.get(
            (content_type, content_schema_version)
        )
        if content_type in _SENSITIVE_SUMMARY_CONTENT_TYPES:
            return _summary_public_body(content_type, registration, safe_body)
        if registration is None or registration.public_projection is None:
            return safe_body
        body = registration.adapter.validate_json(safe_body)
        return _serialize_safe_json(registration.public_projection(body))

    def public_content_body(
        self,
        content_type: str,
        content_schema_version: int,
        safe_body: str,
    ) -> str:
        registration = self._registrations.get(
            (content_type, content_schema_version)
        )
        if content_type in _SENSITIVE_SUMMARY_CONTENT_TYPES:
            return _summary_public_body(content_type, registration, safe_body)
        if registration is None or not registration.project_full_content:
            return safe_body
        if registration.public_projection is None:
            raise UnsupportedTranscriptContentError(
                "full-content projection requires a public projection"
            )
        body = registration.adapter.validate_json(safe_body)
        return _serialize_safe_json(registration.public_projection(body))

    def references_from_read(self, item: ReadTranscriptItem) -> tuple[UUID, ...]:
        registration = self._registrations.get(
            (item.item.content_type, item.item.content_schema_version)
        )
        if registration is None or item.body is None:
            return ()
        return registration.references(item.body)

    def metadata_from_read(self, item: ReadTranscriptItem) -> TranscriptMetadata:
        registration = self._registrations.get(
            (item.item.content_type, item.item.content_schema_version)
        )
        if registration is None or item.body is None:
            return TranscriptMetadata(
                tags=item.item.tags,
                rag_eligible=item.item.rag_eligible,
            )
        derived = registration.metadata(item.body)
        return TranscriptMetadata(
            tags=item.item.tags,
            rag_eligible=item.item.rag_eligible,
            confidence=derived.confidence,
            provenance=derived.provenance,
        )

    def visibility_from_read(self, item: ReadTranscriptItem) -> ContentVisibility:
        registration = self._registrations.get(
            (item.item.content_type, item.item.content_schema_version)
        )
        if registration is None:
            return ContentVisibility.HISTORY_ONLY
        if item.body is None:
            return (
                ContentVisibility.HISTORY_ONLY
                if registration.history_only_possible
                else ContentVisibility.AUTO
            )
        return registration.visibility(item.body)

    def error_ids_from_read(self, item: ReadTranscriptItem) -> tuple[UUID, ...]:
        registration = self._registrations.get(
            (item.item.content_type, item.item.content_schema_version)
        )
        if registration is None or item.body is None:
            return ()
        return registration.errors(item.body)


def _runtime_references(body: object) -> tuple[UUID, ...]:
    return (body.original_item_id,) if isinstance(body, LateMessageOutputFact) else ()


def _runtime_visibility(body: object) -> ContentVisibility:
    return (
        ContentVisibility.HISTORY_ONLY
        if isinstance(body, (LateMessageOutputFact, SuppressedTranscriptItemFact))
        else ContentVisibility.AUTO
    )


def _history_only_visibility(body: object) -> ContentVisibility:
    del body
    return ContentVisibility.HISTORY_ONLY


def _lifecycle_errors(body: object) -> tuple[UUID, ...]:
    if isinstance(body, (RunFailedEvent, SessionFailedEvent)):
        return (body.payload.error.error_id,)
    if isinstance(body, RunCompletedEvent) and body.payload.warning is not None:
        return (body.payload.warning.error_id,)
    return ()


def _lifecycle_references(body: object) -> tuple[UUID, ...]:
    if isinstance(body, (RunCompletedEvent, SessionCompletedEvent)):
        reference = body.payload.result_ref
        return (reference.item_id,) if reference is not None else ()
    return ()


def _standalone_error(body: object) -> tuple[UUID, ...]:
    if not isinstance(body, PublicError):
        raise TypeError("error registration received another content type")
    return (body.error_id,)


def _evidence_references(body: object) -> tuple[UUID, ...]:
    if not isinstance(
        body,
        (EvidenceToolCall, EvidenceFragment, EvidenceToolResult, EvidenceBundle),
    ):
        raise TypeError("evidence reference extractor received another content type")
    return tuple(UUID(item_id) for item_id in body.referenced_item_ids())


def _evidence_fragment_metadata(body: object) -> TranscriptMetadata:
    if not isinstance(body, EvidenceFragment):
        raise TypeError("evidence metadata extractor received another content type")
    return TranscriptMetadata(rag_eligible=body.rag_eligible)


def _unique_uuid_references(values: tuple[UUID, ...]) -> tuple[UUID, ...]:
    return tuple(dict.fromkeys(values))


def _waterbody_input_references(body: object) -> tuple[UUID, ...]:
    from app.application.reasoning.transcript_models import WaterbodyAgentInputV1

    if not isinstance(body, WaterbodyAgentInputV1):
        raise TypeError("waterbody input reference extractor received another type")
    return _unique_uuid_references(tuple(
        reference.item_id
        for reference in (
            body.analysis_context_ref,
            body.topology_ref,
            *(
                (body.initial_evidence_bundle_ref,)
                if body.initial_evidence_bundle_ref is not None
                else ()
            ),
        )
    ))


def _waterbody_request_references(body: object) -> tuple[UUID, ...]:
    from app.application.reasoning.transcript_models import WaterbodyModelRequestV1

    if not isinstance(body, WaterbodyModelRequestV1):
        raise TypeError("waterbody request reference extractor received another type")
    return (body.evidence_bundle_ref.item_id,)


def _waterbody_response_references(body: object) -> tuple[UUID, ...]:
    from app.application.reasoning.transcript_models import WaterbodyModelResponseV1

    if not isinstance(body, WaterbodyModelResponseV1):
        raise TypeError("waterbody response reference extractor received another type")
    return _unique_uuid_references(tuple(
        reference.item_id for reference in body.message_refs
    ))


def _reasoning_decision_references(body: object) -> tuple[UUID, ...]:
    from app.application.reasoning.transcript_models import ReasoningDecisionV1

    if not isinstance(body, ReasoningDecisionV1):
        raise TypeError("reasoning decision reference extractor received another type")
    return _unique_uuid_references(tuple(
        reference.item_id
        for reference in (body.model_response_ref, body.bundle_ref)
        if reference is not None
    ))


def _inference_result_references(body: object) -> tuple[UUID, ...]:
    if not isinstance(body, InferenceResult):
        raise TypeError("inference result reference extractor received another type")
    return extract_analysis_detail_references(body.analysis_detail)


def _static_metadata(*tags: str) -> MetadataExtractor:
    def metadata(body: object) -> TranscriptMetadata:
        del body
        return TranscriptMetadata(tags=tags)

    return metadata


def _inference_result_metadata(body: object) -> TranscriptMetadata:
    if not isinstance(body, InferenceResult):
        raise TypeError("inference result metadata extractor received another type")
    return TranscriptMetadata(
        tags=("waterbody", "result"),
        rag_eligible=True,
        confidence=body.overall_confidence,
        provenance=tuple(
            reference.evidence_id for reference in body.evidence_chain.references
        ),
    )


def _waterbody_manifest_references(body: object) -> tuple[UUID, ...]:
    from app.application.analysis_runs.topology_result_models import (
        WaterbodyDependencyManifest,
    )

    if not isinstance(body, WaterbodyDependencyManifest):
        raise TypeError("dependency manifest reference extractor received another type")
    return _unique_uuid_references((
        body.topology_ref.item_id,
        *(
            dependency.result_ref.item_id
            for dependency in body.dependencies
            if dependency.result_ref is not None
        ),
        *(
            reference.item_id
            for dependency in body.dependencies
            for reference in dependency.usable_cell_refs
        ),
    ))


def _run_summary_input_references(body: object) -> tuple[UUID, ...]:
    from app.application.run_summary.models import RunSummaryInput

    if not isinstance(body, RunSummaryInput):
        raise TypeError("run summary input reference extractor received another type")
    return _unique_uuid_references((
        body.target_result_projection.result_ref.item_id,
        *(
            card.result_ref.item_id
            for card in body.report_cards
            if card.result_ref is not None
        ),
        *(reference.item_id for reference in body.allowed_source_refs),
    ))


def _run_summary_request_references(body: object) -> tuple[UUID, ...]:
    from app.application.run_summary.models import RunSummaryModelRequest

    if not isinstance(body, RunSummaryModelRequest):
        raise TypeError("run summary request reference extractor received another type")
    return (body.input_ref.item_id,)


def _run_summary_response_references(body: object) -> tuple[UUID, ...]:
    from app.application.run_summary.models import RunSummaryModelResponse

    if not isinstance(body, RunSummaryModelResponse):
        raise TypeError("run summary response reference extractor received another type")
    return _unique_uuid_references(tuple(
        reference.item_id for reference in body.message_refs
    ))


def _run_summary_result_references(body: object) -> tuple[UUID, ...]:
    from app.application.run_summary.models import RunSummaryResult

    if not isinstance(body, RunSummaryResult):
        raise TypeError("run summary result reference extractor received another type")
    return _unique_uuid_references(tuple(
        highlight.source_ref.item_id for highlight in body.highlights
    ))


def _topology_run_result_references(body: object) -> tuple[UUID, ...]:
    from app.application.analysis_runs.topology_result_models import TopologyRunResult

    if not isinstance(body, TopologyRunResult):
        raise TypeError("topology result reference extractor received another type")
    return _unique_uuid_references((
        body.analysis_context_ref.item_id,
        body.topology_ref.item_id,
        body.target_result_ref.item_id,
        *(
            entry.result_ref.item_id
            for entry in body.node_outcomes
            if entry.result_ref is not None
        ),
        *(
            (body.summary.result_ref.item_id,)
            if body.summary.result_ref is not None
            else ()
        ),
    ))


def _waterbody_response_projection(body: object) -> JsonValue:
    from app.application.reasoning.transcript_models import WaterbodyModelResponseV1

    if not isinstance(body, WaterbodyModelResponseV1):
        raise TypeError("waterbody response projection received another type")
    return {
        "schema_version": body.schema_version,
        "operation_id": str(body.operation_id),
        "phase": body.phase.value,
        "model": body.model.model_dump(mode="json"),
        "finish_reason": body.finish_reason.value,
        "message_refs": [
            reference.model_dump(mode="json") for reference in body.message_refs
        ],
        "tool_calls": [
            {
                "provider_call_id": call.provider_call_id,
                "name": call.name,
            }
            for call in body.tool_calls
        ],
        "structured_outputs": [
            {"output_id": output.output_id, "present": True}
            for output in body.structured_outputs
        ],
        "refusal": (
            {"message_id": body.refusal.message_id, "text": body.refusal.text}
            if body.refusal is not None
            else None
        ),
        "usage": (
            body.usage.model_dump(mode="json") if body.usage is not None else None
        ),
        "provider_response_id": body.provider_response_id,
    }


def _run_summary_request_projection(body: object) -> JsonValue:
    from app.application.run_summary.models import RunSummaryModelRequest

    if not isinstance(body, RunSummaryModelRequest):
        raise TypeError("run summary request projection received another type")
    value = body.model_dump(mode="json")
    return {
        key: value[key]
        for key in (
            "schema_version",
            "operation_id",
            "model",
            "input_ref",
            "input_digest",
            "deadline",
            "request_digest",
        )
    }


def _run_summary_response_projection(body: object) -> JsonValue:
    from app.application.run_summary.models import RunSummaryModelResponse

    if not isinstance(body, RunSummaryModelResponse):
        raise TypeError("run summary response projection received another type")
    value = body.model_dump(mode="json")
    return {
        "schema_version": value["schema_version"],
        "operation_id": value["operation_id"],
        "model": value["model"],
        "finish_reason": value["finish_reason"],
        "message_refs": value["message_refs"],
        "usage": value["usage"],
        "provider_response_id": value["provider_response_id"],
        "structured_output_present": body.structured_output is not None,
        "visible_text_present": bool(body.visible_text),
    }


_PUBLIC_SOURCE_HOST_SUFFIXES = (
    ".epa.gov",
    ".usgs.gov",
    ".weather.gov",
    ".canada.ca",
)
_PUBLIC_SOURCE_PREVIEW_RECORDS = 32
_PUBLIC_SOURCE_PREVIEW_BYTES = 16 * 1024


def _public_source_uri(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not any(
        host == suffix[1:] or host.endswith(suffix)
        for suffix in _PUBLIC_SOURCE_HOST_SUFFIXES
    ):
        return None
    return urlunsplit(("https", host, parsed.path, "", ""))


def _public_source_record(record) -> JsonValue:
    projected = record.model_dump(mode="json")
    projected["source_uri"] = _public_source_uri(record.source_uri)
    provenance = dict(projected["provenance"])
    provenance.pop("facility_sample_centers", None)
    provenance.pop("facility_request_identities", None)
    projected["provenance"] = provenance
    return projected


def _evidence_source_records_projection(body: object) -> JsonValue:
    if not isinstance(body, EvidenceSourceRecords):
        raise TypeError("source-record projection received another content type")
    records: list[JsonValue] = []
    for record in body.records[:_PUBLIC_SOURCE_PREVIEW_RECORDS]:
        candidate = [*records, _public_source_record(record)]
        projection = {
            "schema_version": body.schema_version,
            "operation_id": body.operation_id,
            "batch_id": body.batch_id,
            "manifest": body.manifest.model_dump(mode="json"),
            "record_count": len(body.records),
            "records": candidate,
            "records_truncated": len(candidate) < len(body.records),
        }
        if len(_serialize_safe_json(projection).encode("utf-8")) > (
            _PUBLIC_SOURCE_PREVIEW_BYTES
        ):
            break
        records = candidate
    return {
        "schema_version": body.schema_version,
        "operation_id": body.operation_id,
        "batch_id": body.batch_id,
        "manifest": body.manifest.model_dump(mode="json"),
        "record_count": len(body.records),
        "records": records,
        "records_truncated": len(records) < len(body.records),
    }


def create_production_registry() -> TranscriptRegistry:
    from app.application.analysis_runs.topology_result_models import (
        TopologyRunResult,
        WaterbodyDependencyManifest,
    )
    from app.application.reasoning.transcript_models import (
        ReasoningDecisionV1,
        WaterbodyAgentInputV1,
        WaterbodyAgentMessageV1,
        WaterbodyModelRequestV1,
        WaterbodyModelResponseV1,
        finalize_waterbody_agent_message,
        waterbody_agent_message_projection,
    )
    from app.application.run_summary.models import (
        RunSummaryAgentMessage,
        RunSummaryInput,
        RunSummaryModelRequest,
        RunSummaryModelResponse,
        RunSummaryResult,
        finalize_run_summary_agent_message,
        run_summary_agent_message_projection,
    )

    registry = TranscriptRegistry()
    registry.register(ContentRegistration(
        content_type="analysis_request",
        content_schema_version=1,
        adapter=TypeAdapter(AnalysisRequestV1),
        preserve_unset=True,
    ))
    registry.register(ContentRegistration(
        content_type="lifecycle_event",
        content_schema_version=2,
        adapter=TypeAdapter(LifecycleEvent),
        references=_lifecycle_references,
        errors=_lifecycle_errors,
    ))
    registry.register(ContentRegistration(
        content_type="error",
        content_schema_version=1,
        adapter=TypeAdapter(PublicError),
        errors=_standalone_error,
    ))
    registry.register(ContentRegistration(
        content_type="runtime_fact",
        content_schema_version=1,
        adapter=TypeAdapter(RuntimeFact),
        references=_runtime_references,
        visibility=_runtime_visibility,
        history_only_possible=True,
    ))
    registry.register(ContentRegistration(
        content_type="query_parser_input",
        content_schema_version=1,
        adapter=TypeAdapter(QueryParserInputV1),
    ))
    registry.register(ContentRegistration(
        content_type="query_parser_message",
        content_schema_version=1,
        adapter=TypeAdapter(QueryParserMessageV1),
        message_adapter=query_parser_message_projection,
        message_finalizer=finalize_query_parser_message,
    ))
    registry.register(ContentRegistration(
        content_type="query_understood",
        content_schema_version=1,
        adapter=TypeAdapter(AnalysisContext),
    ))
    registry.register(ContentRegistration(
        content_type="topology_ready",
        content_schema_version=1,
        adapter=TypeAdapter(HydrologyGraph),
    ))
    registry.register(ContentRegistration(
        content_type="evidence_tool_call",
        content_schema_version=1,
        adapter=TypeAdapter(EvidenceToolCall),
        references=_evidence_references,
    ))
    registry.register(ContentRegistration(
        content_type="evidence_source_records",
        content_schema_version=1,
        adapter=TypeAdapter(EvidenceSourceRecords),
        public_projection=_evidence_source_records_projection,
    ))
    registry.register(ContentRegistration(
        content_type="evidence_fragment",
        content_schema_version=1,
        adapter=TypeAdapter(EvidenceFragment),
        references=_evidence_references,
        metadata=_evidence_fragment_metadata,
    ))
    registry.register(ContentRegistration(
        content_type="evidence_tool_result",
        content_schema_version=1,
        adapter=TypeAdapter(EvidenceToolResult),
        references=_evidence_references,
    ))
    registry.register(ContentRegistration(
        content_type="evidence_bundle",
        content_schema_version=1,
        adapter=TypeAdapter(EvidenceBundle),
        references=_evidence_references,
    ))
    registry.register(ContentRegistration(
        content_type="waterbody_agent_input",
        content_schema_version=1,
        adapter=TypeAdapter(WaterbodyAgentInputV1),
        references=_waterbody_input_references,
        metadata=_static_metadata("waterbody", "input"),
    ))
    registry.register(ContentRegistration(
        content_type="waterbody_model_request",
        content_schema_version=1,
        adapter=TypeAdapter(WaterbodyModelRequestV1),
        references=_waterbody_request_references,
        metadata=_static_metadata("waterbody", "model_request"),
    ))
    registry.register(ContentRegistration(
        content_type="waterbody_agent_message",
        content_schema_version=1,
        adapter=TypeAdapter(WaterbodyAgentMessageV1),
        metadata=_static_metadata("waterbody", "agent_message"),
        message_adapter=waterbody_agent_message_projection,
        message_finalizer=finalize_waterbody_agent_message,
    ))
    registry.register(ContentRegistration(
        content_type="waterbody_model_response",
        content_schema_version=1,
        adapter=TypeAdapter(WaterbodyModelResponseV1),
        references=_waterbody_response_references,
        metadata=_static_metadata("waterbody", "model_response"),
        public_projection=_waterbody_response_projection,
    ))
    registry.register(ContentRegistration(
        content_type="reasoning_decision",
        content_schema_version=1,
        adapter=TypeAdapter(ReasoningDecisionV1),
        references=_reasoning_decision_references,
        metadata=_static_metadata("waterbody", "reasoning_decision"),
    ))
    registry.register(ContentRegistration(
        content_type="inference_result",
        content_schema_version=1,
        adapter=TypeAdapter(InferenceResult),
        references=_inference_result_references,
        metadata=_inference_result_metadata,
    ))
    registry.register(ContentRegistration(
        content_type="waterbody_dependency_manifest",
        content_schema_version=1,
        adapter=TypeAdapter(WaterbodyDependencyManifest),
        references=_waterbody_manifest_references,
        metadata=_static_metadata("waterbody", "dependency_manifest"),
    ))
    registry.register(ContentRegistration(
        content_type="run_summary_input",
        content_schema_version=1,
        adapter=TypeAdapter(RunSummaryInput),
        references=_run_summary_input_references,
        metadata=_static_metadata("run_summary", "input"),
    ))
    registry.register(ContentRegistration(
        content_type="run_summary_model_request",
        content_schema_version=1,
        adapter=TypeAdapter(RunSummaryModelRequest),
        references=_run_summary_request_references,
        metadata=_static_metadata("run_summary", "model_request"),
        visibility=_history_only_visibility,
        public_projection=_run_summary_request_projection,
        history_only_possible=True,
        project_full_content=True,
    ))
    registry.register(ContentRegistration(
        content_type="run_summary_agent_message",
        content_schema_version=1,
        adapter=TypeAdapter(RunSummaryAgentMessage),
        metadata=_static_metadata("run_summary", "agent_message"),
        message_adapter=run_summary_agent_message_projection,
        message_finalizer=finalize_run_summary_agent_message,
    ))
    registry.register(ContentRegistration(
        content_type="run_summary_model_response",
        content_schema_version=1,
        adapter=TypeAdapter(RunSummaryModelResponse),
        references=_run_summary_response_references,
        metadata=_static_metadata("run_summary", "model_response"),
        visibility=_history_only_visibility,
        public_projection=_run_summary_response_projection,
        history_only_possible=True,
        project_full_content=True,
    ))
    registry.register(ContentRegistration(
        content_type="run_summary_result",
        content_schema_version=1,
        adapter=TypeAdapter(RunSummaryResult),
        references=_run_summary_result_references,
        metadata=_static_metadata("run_summary", "result"),
    ))
    registry.register(ContentRegistration(
        content_type="topology_run_result",
        content_schema_version=1,
        adapter=TypeAdapter(TopologyRunResult),
        references=_topology_run_result_references,
        metadata=_static_metadata("run", "result"),
    ))
    registry.register(ContentRegistration(
        content_type="lifecycle_event",
        content_schema_version=1,
        adapter=TypeAdapter(LegacyLifecycleEventV1),
        writable=False,
    ))
    return registry


def extract_analysis_detail_references(detail: AnalysisDetail) -> tuple[UUID, ...]:
    original = (
        *detail.transcript_item_refs,
        *(reference for step in detail.steps for reference in step.transcript_item_refs),
    )
    references: list[UUID] = []
    for value in original:
        reference = UUID(value)
        if reference not in references:
            references.append(reference)
    return tuple(references)


def validate_references(
    source_run_id: UUID,
    reference_ids: tuple[UUID, ...],
    *,
    persisted: tuple[TranscriptContentRef, ...] = (),
    same_batch: tuple[TranscriptContentRef, ...] = (),
    generating: tuple[TranscriptContentRef, ...] = (),
) -> ReferenceResolution:
    known = {*persisted, *same_batch}
    pending = set(generating)
    waiting: list[TranscriptContentRef] = []
    for item_id in reference_ids:
        target = TranscriptContentRef(run_id=source_run_id, item_id=item_id)
        if target in known:
            continue
        if target in pending:
            waiting.append(target)
            continue
        foreign = next(
            (
                candidate for candidate in (*persisted, *same_batch, *generating)
                if candidate.item_id == item_id and candidate.run_id != source_run_id
            ),
            None,
        )
        if foreign is not None:
            raise ValueError("transcript references must remain in the same run")
        raise ValueError("transcript reference target does not exist")
    if waiting:
        return ReferenceResolution(
            status=ReferenceResolutionStatus.WAITING,
            waiting_for=tuple(waiting),
        )
    return ReferenceResolution(status=ReferenceResolutionStatus.READY)

import json

from pydantic import TypeAdapter, ValidationError

from app.domain.domain_model import DomainModel
from app.domain.evidence.collection_manifest import CollectionManifest
from app.domain.evidence.evidence_bundle import EvidenceBundle
from app.domain.evidence.evidence_content_error import EvidenceContentError
from app.domain.evidence.evidence_fragment import EvidenceFragment
from app.domain.evidence.evidence_payload import EvidencePayload
from app.domain.evidence.evidence_tool_result import EvidenceToolResult
from app.domain.evidence.source_record import SourceRecord

_SOURCE_RECORD_ADAPTER = TypeAdapter(SourceRecord)
_EVIDENCE_PAYLOAD_ADAPTER = TypeAdapter(EvidencePayload)


def canonical_model_bytes(model: DomainModel) -> bytes:
    return json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _validation_error(exc: ValidationError) -> EvidenceContentError:
    error_types = {error["type"] for error in exc.errors()}
    code = (
        "unknown_discriminator"
        if error_types & {"union_tag_invalid", "union_tag_not_found"}
        else "corrupt_content"
    )
    return EvidenceContentError(code, "Evidence content failed schema validation.")


def load_source_record_json(data: str | bytes) -> SourceRecord:
    try:
        return _SOURCE_RECORD_ADAPTER.validate_json(data)
    except ValidationError as exc:
        raise _validation_error(exc) from exc


def load_evidence_payload_json(data: str | bytes) -> EvidencePayload:
    try:
        return _EVIDENCE_PAYLOAD_ADAPTER.validate_json(data)
    except ValidationError as exc:
        raise _validation_error(exc) from exc


def load_evidence_fragment_json(data: str | bytes) -> EvidenceFragment:
    try:
        return EvidenceFragment.model_validate_json(data)
    except ValidationError as exc:
        raise _validation_error(exc) from exc


def load_evidence_bundle_json(data: str | bytes) -> EvidenceBundle:
    try:
        return EvidenceBundle.model_validate_json(data)
    except ValidationError as exc:
        raise _validation_error(exc) from exc


def load_collection_manifest_json(data: str | bytes) -> CollectionManifest:
    try:
        return CollectionManifest.model_validate_json(data)
    except ValidationError as exc:
        raise _validation_error(exc) from exc


def load_evidence_tool_result_json(data: str | bytes) -> EvidenceToolResult:
    try:
        return EvidenceToolResult.model_validate_json(data)
    except ValidationError as exc:
        raise _validation_error(exc) from exc

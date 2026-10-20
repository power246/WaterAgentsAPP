import base64
import json
from collections.abc import Callable
from uuid import UUID, uuid4

from app.application.guidelines.errors import GuidelineServiceError
from app.domain.guidelines import (
    GuidelineDraftV1, GuidelineIdentity, GuidelineMutation, GuidelineReview,
    GuidelineVersionV1, GuidelineOperationKind as Kind, project_model_view,
)
from app.domain.guidelines.canonical import digest, normalize_text
from app.domain.guidelines.mutation_bodies import action_body, create_body, publication_body, save_body
from app.domain.hydrology.parameter_capability_registry import ParameterCapabilityRegistry
from app.ports.clock import ClockPort
from app.ports.errors import GuidelineConflictError, PortError
from app.ports.guidelines import GuidelineRepositoryPort

_STATES = {"draft", "preparing", "available", "preparation_failed", "disabled", "unavailable"}
_CONFLICTS = {
    "guideline_revision_conflict", "guideline_publication_pending", "guideline_retry_conflict",
    "guideline_root_required", "guideline_base_changed", "guideline_bundle_target_changed",
    "guideline_embedding_not_ready", "guideline_version_conflict", "guideline_constraint_conflict",
}


class GuidelineAuthoringService:
    def __init__(self, repository: GuidelineRepositoryPort | None, registry: ParameterCapabilityRegistry,
                 clock: ClockPort, *, id_factory: Callable[[], UUID] = uuid4) -> None:
        self._repository = repository
        self._registry = registry
        self._clock = clock
        self._id_factory = id_factory

    @property
    def repository(self):
        if self._repository is None:
            raise GuidelineServiceError("guideline_storage_unavailable", 503)
        return self._repository

    async def _call(self, action, document_id=None):
        try:
            return await action()
        except GuidelineConflictError as error:
            if error.code == "guideline_bundled_readonly":
                raise GuidelineServiceError("guideline_read_only", 403) from None
            if error.code in {"guideline_document_missing", "guideline_operation_missing", "guideline_base_missing"}:
                raise GuidelineServiceError("guideline_not_found", 404) from None
            current = None
            if document_id is not None:
                try:
                    current = await self.repository.read_document_details(document_id)
                except (PortError, ValueError):
                    pass
            code = "idempotency_conflict" if error.code == "guideline_idempotency_conflict" else (
                error.code if error.code in _CONFLICTS else "guideline_conflict")
            raise GuidelineServiceError(code, 409, current=current) from None
        except (PortError, ValueError):
            raise GuidelineServiceError("guideline_storage_unavailable", 503) from None

    async def _replay(self, mutation, document_id=None):
        return await self._call(lambda: self.repository.read_receipt(mutation), document_id)

    async def get(self, document_id):
        document = await self._call(lambda: self.repository.read_document_details(document_id))
        if document is None:
            raise GuidelineServiceError("guideline_not_found", 404)
        return document

    async def get_version(self, document_id, version_id):
        version = await self._call(lambda: self.repository.read_version(document_id, version_id))
        if version is None:
            raise GuidelineServiceError("guideline_not_found", 404)
        return version

    async def get_operation(self, operation_id):
        operation = await self._call(lambda: self.repository.read_operation(operation_id))
        if operation is None:
            raise GuidelineServiceError("guideline_not_found", 404)
        return operation

    async def list(self, *, source="all", state=None, q="", limit=20, cursor=None):
        if source not in {"all", "bundled", "mine"} or state not in _STATES | {None}:
            raise GuidelineServiceError("guideline_filter_invalid", 422)
        if type(limit) is not int or not 1 <= limit <= 50 or not isinstance(q, str) or len(q) > 160:
            raise GuidelineServiceError("guideline_filter_invalid", 422)
        try:
            q = normalize_text(q).strip()
        except ValueError:
            raise GuidelineServiceError("guideline_filter_invalid", 422) from None
        selector = digest({"source": source, "state": state, "q": q})
        after = None
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 512:
                    raise ValueError()
                data = json.loads(base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True))
                if (not isinstance(data, dict) or set(data) != {"v", "after", "selector"}
                        or type(data["v"]) is not int or data["v"] != 1 or data["selector"] != selector
                        or not isinstance(data["after"], str)):
                    raise ValueError()
                after = UUID(data["after"])
            except (ValueError, TypeError, KeyError):
                raise GuidelineServiceError("guideline_cursor_invalid", 422) from None
        items = await self._call(lambda: self.repository.list_documents(
            source=source, state=state, q=q, after_id=after, limit=limit + 1))
        next_cursor = None
        if len(items) > limit:
            next_cursor = base64.urlsafe_b64encode(json.dumps({"v": 1, "after": str(items[limit - 1].identity.guideline_id),
                                                             "selector": selector}, separators=(",", ":")).encode()).decode().rstrip("=")
        return items[:limit], next_cursor

    async def create(self, draft: GuidelineDraftV1, key):
        document_id = self._id_factory()
        identity = GuidelineIdentity(guideline_id=document_id, family_root_id=document_id, origin="local_custom")
        mutation = GuidelineMutation.for_request(key, Kind.CREATE, create_body(identity, draft, None))
        return await self._call(lambda: self.repository.create_draft(identity, draft, mutation))

    async def save(self, document_id, draft, expected_revision, key):
        mutation = GuidelineMutation.for_request(key, Kind.SAVE_DRAFT, save_body(document_id, draft, expected_revision))
        return await self._call(lambda: self.repository.save_draft(document_id, draft,
                               expected_revision=expected_revision, mutation=mutation), document_id)

    async def local_edit(self, document_id, base_version_id, base_content_digest, expected_revision, key):
        mutation = GuidelineMutation.for_request(key, Kind.LOCAL_EDIT, action_body(
            document_id, expected_revision, base_version_id=str(base_version_id), base_content_digest=base_content_digest))
        if receipt := await self._replay(mutation, document_id):
            return receipt
        if (await self.get(document_id)).identity.origin.value != "bundled":
            raise GuidelineServiceError("guideline_read_only", 403)
        return await self._call(lambda: self.repository.local_edit(document_id, base_version_id, base_content_digest,
                                expected_revision=expected_revision, mutation=mutation), document_id)

    async def publish(self, document_id, *, expected_revision, content_digest, review_confirmed, reviewer, review_note, key):
        mutation = GuidelineMutation.for_request(key, Kind.PUBLISH, publication_body(
            document_id, expected_revision, content_digest, review_confirmed, reviewer, review_note))
        if receipt := await self._replay(mutation, document_id):
            return receipt
        document = await self.get(document_id)
        if document.identity.origin.value == "bundled":
            raise GuidelineServiceError("guideline_read_only", 403)
        if document.revision != expected_revision:
            raise GuidelineServiceError("guideline_revision_conflict", 409, current=document)
        if review_confirmed is not True:
            raise GuidelineServiceError("guideline_review_required", 422)
        if document.draft is None or document.draft.content_digest != content_digest:
            raise GuidelineServiceError("guideline_digest_mismatch", 422)
        if document.draft.source_kind is not None and document.draft.source_kind.value == "reference_derived" and not document.draft.sources:
            raise GuidelineServiceError("guideline_source_required", 422)
        try:
            content = document.draft.publication_content(self._registry)
            review = GuidelineReview(guideline_id=document_id, content_digest=content_digest,
                review_confirmed=True, reviewer=reviewer, review_note=review_note, reviewed_at=self._clock.now())
            version = GuidelineVersionV1(version_id=self._id_factory(), version_number=document.latest_version_number + 1,
                identity=document.identity, content=content, model_view=project_model_view(content), review=review)
        except ValueError:
            raise GuidelineServiceError("guideline_content_invalid", 422) from None
        return await self._call(lambda: self.repository.publish_version(version,
                                expected_revision=expected_revision, mutation=mutation), document_id)

    async def set_enabled(self, document_id, enabled, expected_revision, key):
        mutation = GuidelineMutation.for_request(key, Kind.SET_ENABLED, action_body(document_id, expected_revision, enabled=enabled))
        return await self._call(lambda: self.repository.set_enabled(document_id, enabled,
                                expected_revision=expected_revision, mutation=mutation), document_id)

    async def restore(self, document_id, version_id, expected_revision, key):
        mutation = GuidelineMutation.for_request(key, Kind.RESTORE_BUNDLED,
                                               action_body(document_id, expected_revision, version_id=str(version_id)))
        if receipt := await self._replay(mutation, document_id):
            return receipt
        if (await self.get(document_id)).identity.origin.value != "bundled":
            raise GuidelineServiceError("guideline_read_only", 403)
        return await self._call(lambda: self.repository.restore_bundled(document_id, version_id,
                                expected_revision=expected_revision, mutation=mutation), document_id)

    async def retry(self, operation_id, expected_revision, key):
        mutation = GuidelineMutation.for_request(key, Kind.RETRY, {"operation_id": str(operation_id), "expected_revision": expected_revision})
        if receipt := await self._replay(mutation):
            return receipt
        operation = await self.get_operation(operation_id)
        return await self._call(lambda: self.repository.retry_operation(operation_id,
            expected_revision=expected_revision, mutation=mutation), operation.receipt.document_id)

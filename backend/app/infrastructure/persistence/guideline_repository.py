import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.guidelines import (
    CanonicalGuidelineDocumentV1, GuidelineCatalogSnapshot, GuidelineDocumentSnapshot,
    GuidelineDraftV1, GuidelineEmbeddingBatch, GuidelineEmbeddingProfileV1,
    GuidelineIdentity, GuidelineIndexGeneration, GuidelineMutation, GuidelineMutationReceipt,
    GuidelineOperation, GuidelineOperationKind, GuidelineOrigin, GuidelineVersionV1,
    GuidelineDocumentDetails, GuidelineListItem,
)
from app.domain.guidelines.canonical import canonical_text
from app.domain.guidelines.identity import bundled_guideline_version_id
from app.domain.guidelines.mutation_bodies import action_body, create_body, publish_body, save_body
from app.domain.guidelines.policy import DEFAULT_GUIDELINE_POLICY
from app.domain.hydrology.parameter_key import ParameterKey
from app.domain.hydrology.parameter_capability_registry import ParameterCapabilityRegistry
from app.domain.memory_rag.embedding_vector import EmbeddingVector
from app.infrastructure.config import load_parameter_capability_registry
from app.infrastructure.config.settings import AppSettings
from app.infrastructure.persistence.database import create_database_engine
from app.ports.errors import GuidelineConflictError, PortUnavailableError

_MANAGEMENT_QUERY = """
SELECT d.*, r.activation_epoch AS root_epoch, r.disabled AS root_disabled,
       (d.draft_digest IS NOT NULL AND d.draft_digest IS DISTINCT FROM a.content_digest) AS has_unpublished_changes,
       a.snapshot AS active_body, p.snapshot AS pending_body, base.snapshot AS base_body,
       e.version_id AS effective_version_id, job.body AS preparation,
       (SELECT COALESCE(MAX(v.version_number), 0) FROM guideline_versions v
        WHERE v.document_id = d.document_id) AS latest_number,
       CASE WHEN r.disabled THEN 'disabled'
            WHEN job.state IN ('pending', 'running') THEN 'preparing'
            WHEN job.state = 'failed' THEN 'preparation_failed'
            WHEN ready.version_id IS NOT NULL THEN 'available'
            WHEN d.draft IS NOT NULL THEN 'draft' ELSE 'unavailable' END AS management_state,
       COALESCE(d.draft->>'title', a.snapshot->'content'->>'title', p.snapshot->'content'->>'title',
                base.snapshot->'content'->>'title') AS display_title,
       COALESCE(d.draft->>'summary', a.snapshot->'content'->>'summary', p.snapshot->'content'->>'summary',
                base.snapshot->'content'->>'summary') AS display_summary,
       COALESCE(d.draft->>'source_kind', a.snapshot->'content'->>'source_kind', p.snapshot->'content'->>'source_kind',
                base.snapshot->'content'->>'source_kind') AS display_source_kind,
       COALESCE(d.draft->'keywords', a.snapshot->'content'->'keywords', p.snapshot->'content'->'keywords',
                base.snapshot->'content'->'keywords', '[]'::jsonb)::text AS display_keywords
FROM guideline_documents d JOIN guideline_documents r ON r.document_id = d.family_root_id
LEFT JOIN guideline_versions a ON a.version_id = d.active_version_id
LEFT JOIN guideline_versions p ON p.version_id = d.pending_version_id
LEFT JOIN guideline_catalog_entries e ON e.family_root_id = d.family_root_id AND e.valid_to_revision IS NULL
LEFT JOIN guideline_catalog_state c ON c.singleton_id = 1
LEFT JOIN guideline_embeddings ready ON ready.version_id = d.active_version_id AND ready.generation_id = c.active_generation_id
LEFT JOIN LATERAL (SELECT v.snapshot FROM guideline_versions v WHERE v.document_id = r.document_id
                  AND r.origin = 'bundled' ORDER BY v.version_number DESC LIMIT 1) base ON true
LEFT JOIN LATERAL (SELECT to_jsonb(o) AS body, o.state FROM guideline_operations o
                  WHERE o.document_id = d.document_id AND o.version_id = d.pending_version_id
                  ORDER BY CASE WHEN o.state IN ('pending', 'running', 'failed') THEN 0 ELSE 1 END,
                  o.next_attempt_at DESC, o.operation_id DESC LIMIT 1) job ON true
"""


class PostgresGuidelineRepository:
    """Short transactions only: catalog -> documents (UUID order) -> operations/versions.

    The singleton lock deliberately serializes guideline writes in this local app.
    Provider calls belong outside these methods. Reads never create schema or data.
    """

    def __init__(self, settings: AppSettings, *, engine: AsyncEngine | None = None,
                 registry: ParameterCapabilityRegistry | None = None) -> None:
        self._engine = engine or create_database_engine(settings)
        self._owns_engine = engine is None
        self._timeout = settings.db_operation_timeout_seconds
        self._registry = registry or load_parameter_capability_registry()

    async def aclose(self) -> None:
        if self._owns_engine:
            await self._engine.dispose()

    @asynccontextmanager
    async def _connection(self, *, write: bool = False):
        try:
            async with asyncio.timeout(self._timeout):
                async with self._engine.begin() as connection:
                    if write:
                        await connection.execute(text(
                            "SELECT singleton_id FROM guideline_catalog_state WHERE singleton_id = 1 FOR UPDATE"
                        ))
                    yield connection
        except IntegrityError as error:
            raise GuidelineConflictError("guideline_constraint_conflict") from error
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise PortUnavailableError("Guideline storage is unavailable.") from error

    async def _family(self, connection, root_id: UUID):
        rows = (await connection.execute(text(
            "SELECT * FROM guideline_documents WHERE family_root_id = :root "
            "ORDER BY document_id FOR UPDATE"
        ), {"root": root_id})).mappings().all()
        return {row["document_id"]: row for row in rows}

    @staticmethod
    def _require_revision(document, expected_revision: int) -> None:
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        if document is None or document["revision"] != expected_revision:
            raise GuidelineConflictError("guideline_revision_conflict")

    async def _document_family(self, connection, document_id):
        root = await connection.scalar(text(
            "SELECT family_root_id FROM guideline_documents WHERE document_id = :id"
        ), {"id": document_id})
        if root is None:
            raise GuidelineConflictError("guideline_document_missing")
        return root, await self._family(connection, root)

    async def _receipt(self, connection, mutation):
        body = await connection.scalar(text(
            "SELECT accepted_response FROM guideline_operations WHERE idempotency_key = :key"
        ), {"key": mutation.idempotency_key})
        if body is None:
            return None
        receipt = GuidelineMutationReceipt.model_validate(body)
        if receipt.mutation != mutation:
            raise GuidelineConflictError("guideline_idempotency_conflict")
        return receipt

    async def read_receipt(self, mutation: GuidelineMutation) -> GuidelineMutationReceipt | None:
        async with self._connection() as connection:
            return await self._receipt(connection, mutation)

    @staticmethod
    def _operation(row) -> GuidelineOperation:
        return GuidelineOperation(
            receipt=GuidelineMutationReceipt.model_validate(row["accepted_response"]),
            state=row["state"], activation_epoch=row["activation_epoch"],
            generation_id=row["generation_id"], attempts=row["attempts"],
            lease_owner=row["lease_owner"], lease_until=row["lease_until"],
            safe_error_code=row["safe_error_code"],
        )

    async def read_operation(self, operation_id: UUID) -> GuidelineOperation | None:
        async with self._connection() as connection:
            row = (await connection.execute(text(
                "SELECT * FROM guideline_operations WHERE operation_id = :id"
            ), {"id": operation_id})).mappings().one_or_none()
            return None if row is None else self._operation(row)

    async def read_document(self, document_id: UUID) -> GuidelineDocumentSnapshot | None:
        async with self._connection() as connection:
            row = (await connection.execute(text(
                "SELECT d.*, r.activation_epoch AS root_epoch, r.disabled AS root_disabled "
                "FROM guideline_documents d JOIN guideline_documents r "
                "ON r.document_id = d.family_root_id WHERE d.document_id = :id"
            ), {"id": document_id})).mappings().one_or_none()
            return None if row is None else GuidelineDocumentSnapshot(
                identity=row["identity"], revision=row["revision"], activation_epoch=row["root_epoch"],
                disabled=row["root_disabled"], draft=row["draft"],
                active_version_id=row["active_version_id"], pending_version_id=row["pending_version_id"],
            )

    async def read_version(self, document_id: UUID, version_id: UUID) -> GuidelineVersionV1 | None:
        async with self._connection() as connection:
            body = await connection.scalar(text(
                "SELECT snapshot FROM guideline_versions WHERE document_id = :doc AND version_id = :id"
            ), {"doc": document_id, "id": version_id})
            return None if body is None else GuidelineVersionV1.model_validate(body)

    @staticmethod
    def _has_bundled_update(row):
        return (row["origin"] == "local_override" and row["base_body"] is not None
                and row["identity"]["based_on_version_id"] != row["base_body"]["version_id"])

    async def read_document_details(self, document_id: UUID) -> GuidelineDocumentDetails | None:
        async with self._connection() as connection:
            row = (await connection.execute(text(_MANAGEMENT_QUERY + " WHERE d.document_id = :id"),
                                             {"id": document_id})).mappings().one_or_none()
            if row is None:
                return None
            return GuidelineDocumentDetails(
                identity=row["identity"], revision=row["revision"], activation_epoch=row["root_epoch"],
                disabled=row["root_disabled"], draft=row["draft"], active_version_id=row["active_version_id"],
                draft_digest=row["draft_digest"], has_unpublished_changes=row["has_unpublished_changes"],
                pending_version_id=row["pending_version_id"], active_version=row["active_body"],
                pending_version=row["pending_body"], effective_version_id=row["effective_version_id"],
                latest_version_number=row["latest_number"], latest_bundled_version=row["base_body"],
                preparation=None if row["preparation"] is None else self._operation(row["preparation"]),
                state=row["management_state"], has_bundled_update=self._has_bundled_update(row),
            )

    async def list_documents(self, *, source: str, state: str | None, q: str,
                             after_id: UUID | None, limit: int) -> tuple[GuidelineListItem, ...]:
        async with self._connection() as connection:
            rows = (await connection.execute(text(
                "WITH management AS (" + _MANAGEMENT_QUERY + ") SELECT * FROM management "
                "WHERE (:source = 'all' OR (:source = 'bundled' AND origin = 'bundled') "
                "OR (:source = 'mine' AND origin <> 'bundled')) "
                "AND (CAST(:state AS text) IS NULL OR management_state = :state) "
                "AND (CAST(:after AS uuid) IS NULL OR document_id > CAST(:after AS uuid)) "
                "AND (:q = '' OR strpos(lower(COALESCE(display_title, '') || ' ' || display_keywords), lower(:q)) > 0) "
                "ORDER BY document_id LIMIT :limit"
            ), {"source": source, "state": state, "q": q, "after": after_id, "limit": limit})).mappings().all()
            return tuple(GuidelineListItem(
                identity=row["identity"], revision=row["revision"], activation_epoch=row["root_epoch"],
                title=row["display_title"], summary=row["display_summary"], source_kind=row["display_source_kind"],
                draft_digest=row["draft_digest"], active_version_id=row["active_version_id"],
                has_unpublished_changes=row["has_unpublished_changes"],
                pending_version_id=row["pending_version_id"], effective_version_id=row["effective_version_id"],
                latest_bundled_version_id=None if row["base_body"] is None else row["base_body"]["version_id"],
                state=row["management_state"], has_bundled_update=self._has_bundled_update(row),
            ) for row in rows)

    async def _insert_document(self, connection, identity, draft=None):
        await connection.execute(text(
            "INSERT INTO guideline_documents (document_id, family_root_id, identity, origin, bundle_key, "
            "based_on_version_id, base_content_digest, draft, draft_digest, revision) "
            "VALUES (:id, :root, CAST(:identity AS jsonb), :origin, :bundle, :base, :digest, "
            "CAST(:draft AS jsonb), :draft_digest, 0)"
        ), {"id": identity.guideline_id, "root": identity.family_root_id,
            "identity": identity.model_dump_json(), "origin": identity.origin.value,
            "bundle": identity.bundle_key, "base": identity.based_on_version_id,
            "digest": identity.base_content_digest, "draft": None if draft is None else draft.model_dump_json(),
            "draft_digest": None if draft is None else draft.content_digest})

    async def _accept(self, connection, mutation, *, document_id=None, version_id=None,
                      revision=None, content_digest=None, pending=False, epoch=None, status_code=None):
        receipt = GuidelineMutationReceipt(
            mutation=mutation, status_code=status_code or (202 if pending else (201 if mutation.kind is GuidelineOperationKind.CREATE else 200)),
            operation_id=uuid4(), document_id=document_id, version_id=version_id,
            accepted_revision=revision, content_digest=content_digest,
        )
        await connection.execute(text(
            "INSERT INTO guideline_operations (operation_id, idempotency_key, kind, request_fingerprint, "
            "accepted_response, document_id, version_id, activation_epoch, state) "
            "VALUES (:id, :key, :kind, :fingerprint, CAST(:body AS jsonb), :doc, :version, :epoch, :state)"
        ), {"id": receipt.operation_id, "key": mutation.idempotency_key, "kind": mutation.kind.value,
            "fingerprint": mutation.request_fingerprint, "body": receipt.model_dump_json(),
            "doc": document_id, "version": version_id, "epoch": epoch,
            "state": "pending" if pending else "succeeded"})
        return receipt

    async def create_draft(self, identity: GuidelineIdentity, draft: GuidelineDraftV1,
                           mutation: GuidelineMutation, *, expected_base_revision: int | None = None):
        identity = GuidelineIdentity.model_validate(identity.model_dump())
        draft = GuidelineDraftV1.model_validate(draft.model_dump())
        mutation.require_request(GuidelineOperationKind.CREATE, create_body(identity, draft, expected_base_revision))
        if identity.origin is GuidelineOrigin.BUNDLED:
            raise ValueError("bundled documents cannot own drafts")
        async with self._connection(write=True) as connection:
            if receipt := await self._receipt(connection, mutation):
                return receipt
            if identity.origin is GuidelineOrigin.LOCAL_OVERRIDE:
                family = await self._family(connection, identity.family_root_id)
                root = family.get(identity.family_root_id)
                if root is None or root["origin"] != "bundled" or root["bundle_key"] != identity.base_bundle_key:
                    raise GuidelineConflictError("guideline_base_missing")
                self._require_revision(root, expected_base_revision)
                if any(row["origin"] == "local_override" for row in family.values()):
                    raise GuidelineConflictError("guideline_override_exists")
                await connection.execute(text(
                    "UPDATE guideline_documents SET revision = revision + 1 WHERE document_id = :id"
                ), {"id": identity.family_root_id})
            await self._insert_document(connection, identity, draft)
            await connection.execute(text(
                "UPDATE guideline_documents SET revision = 1 WHERE document_id = :id"
            ), {"id": identity.guideline_id})
            return await self._accept(connection, mutation, document_id=identity.guideline_id,
                                      revision=1, content_digest=draft.content_digest)

    async def local_edit(self, document_id: UUID, base_version_id: UUID, base_content_digest: str, *,
                         expected_revision: int, mutation: GuidelineMutation):
        mutation.require_request(GuidelineOperationKind.LOCAL_EDIT, action_body(
            document_id, expected_revision, base_version_id=str(base_version_id), base_content_digest=base_content_digest))
        async with self._connection(write=True) as connection:
            if receipt := await self._receipt(connection, mutation):
                return receipt
            root_id, family = await self._document_family(connection, document_id)
            root = family[root_id]
            if root_id != document_id or root["origin"] != "bundled":
                raise GuidelineConflictError("guideline_base_missing")
            existing = next((row for row in family.values() if row["origin"] == "local_override"), None)
            if existing is not None:
                return await self._accept(connection, mutation, document_id=existing["document_id"],
                                          revision=existing["revision"], content_digest=existing["draft_digest"])
            self._require_revision(root, expected_revision)
            body = await connection.scalar(text(
                "SELECT snapshot FROM guideline_versions WHERE document_id = :doc AND version_id = :version "
                "AND content_digest = :digest"
            ), {"doc": root_id, "version": base_version_id, "digest": base_content_digest})
            if body is None:
                raise GuidelineConflictError("guideline_base_changed")
            base = GuidelineVersionV1.model_validate(body)
            identity = GuidelineIdentity(
                guideline_id=uuid4(), family_root_id=root_id, origin="local_override",
                base_bundle_key=root["bundle_key"], based_on_version_id=base_version_id,
                base_content_digest=base_content_digest,
            )
            draft = GuidelineDraftV1.model_validate({**base.content.model_dump(), "source_kind": "user_authored"})
            await self._insert_document(connection, identity, draft)
            await connection.execute(text(
                "UPDATE guideline_documents SET revision = revision + 1 WHERE document_id IN (:root, :doc)"
            ), {"root": root_id, "doc": identity.guideline_id})
            return await self._accept(connection, mutation, document_id=identity.guideline_id, revision=1,
                                      content_digest=draft.content_digest, status_code=201)

    async def import_bundled_version(self, version: GuidelineVersionV1):
        version = GuidelineVersionV1.model_validate(version.model_dump())
        identity = version.identity
        if (identity.origin is not GuidelineOrigin.BUNDLED
                or version.version_id != bundled_guideline_version_id(identity.bundle_key, version.version_number)):
            raise ValueError("bundled import requires a stable packaged version identity")
        version.content.require_registered_parameters(self._registry)
        async with self._connection(write=True) as connection:
            family = await self._family(connection, identity.family_root_id)
            if not family:
                await self._insert_document(connection, identity)
                family = await self._family(connection, identity.family_root_id)
            root = family[identity.guideline_id]
            existing = (await connection.execute(text(
                "SELECT * FROM guideline_versions WHERE document_id = :doc AND version_number = :number"
            ), {"doc": identity.guideline_id, "number": version.version_number})).mappings().one_or_none()
            if existing is not None and existing["content_digest"] != version.content.content_digest:
                raise GuidelineConflictError("guideline_bundle_version_conflict")
            maximum = await connection.scalar(text(
                "SELECT COALESCE(MAX(version_number), 0) FROM guideline_versions WHERE document_id = :doc"
            ), {"doc": identity.guideline_id})
            if existing is None and version.version_number < maximum:
                raise GuidelineConflictError("guideline_bundle_older_version")
            if existing is not None:
                operation = (await connection.execute(text(
                    "SELECT * FROM guideline_operations WHERE document_id = :doc AND version_id = :version "
                    "ORDER BY CASE WHEN state IN ('pending', 'running', 'failed') AND activation_epoch = :epoch THEN 0 "
                    "WHEN state = 'succeeded' THEN 1 ELSE 2 END, next_attempt_at DESC, operation_id DESC LIMIT 1"
                ), {"doc": identity.guideline_id, "version": existing["version_id"],
                    "epoch": root["activation_epoch"]})).mappings().one_or_none()
                if operation is not None and (
                    version.version_number < maximum or root["active_version_id"] == existing["version_id"]
                    or (operation["state"] in {"pending", "running", "failed"}
                        and operation["activation_epoch"] == root["activation_epoch"]
                        and root["pending_version_id"] == existing["version_id"])
                ):
                    return GuidelineMutationReceipt.model_validate(operation["accepted_response"])
            else:
                await connection.execute(text(
                    "INSERT INTO guideline_versions (version_id, document_id, family_root_id, version_number, "
                    "content_digest, snapshot) VALUES (:id, :doc, :root, :number, :digest, CAST(:body AS jsonb))"
                ), {"id": version.version_id, "doc": identity.guideline_id, "root": identity.family_root_id,
                    "number": version.version_number, "digest": version.content.content_digest, "body": version.model_dump_json()})
            version_id = version.version_id if existing is None else existing["version_id"]
            await connection.execute(text(
                "UPDATE guideline_operations SET state = 'superseded', lease_owner = NULL, lease_until = NULL, "
                "safe_error_code = NULL WHERE document_id = :doc AND state IN ('pending', 'running', 'failed')"
            ), {"doc": identity.guideline_id})
            await connection.execute(text(
                "UPDATE guideline_documents SET pending_version_id = :version, revision = revision + 1 WHERE document_id = :doc"
            ), {"doc": identity.guideline_id, "version": version_id})
            mutation = GuidelineMutation.for_request(uuid4(), GuidelineOperationKind.IMPORT_BUNDLED, {
                "bundle_key": identity.bundle_key, "version_number": version.version_number,
                "content_digest": version.content.content_digest,
            })
            return await self._accept(connection, mutation, document_id=identity.guideline_id, version_id=version_id,
                                      revision=root["revision"] + 1, content_digest=version.content.content_digest,
                                      pending=True, epoch=root["activation_epoch"])

    async def save_draft(self, document_id: UUID, draft: GuidelineDraftV1, *, expected_revision: int,
                         mutation: GuidelineMutation):
        draft = GuidelineDraftV1.model_validate(draft.model_dump())
        mutation.require_request(GuidelineOperationKind.SAVE_DRAFT, save_body(document_id, draft, expected_revision))
        async with self._connection(write=True) as connection:
            if receipt := await self._receipt(connection, mutation):
                return receipt
            _, family = await self._document_family(connection, document_id)
            document = family[document_id]
            self._require_revision(document, expected_revision)
            if document["origin"] == "bundled":
                raise GuidelineConflictError("guideline_bundled_readonly")
            await connection.execute(text(
                "UPDATE guideline_documents SET draft = CAST(:draft AS jsonb), draft_digest = :digest, "
                "revision = revision + 1 WHERE document_id = :id"
            ), {"id": document_id, "draft": draft.model_dump_json(), "digest": draft.content_digest})
            return await self._accept(connection, mutation, document_id=document_id,
                                      revision=expected_revision + 1, content_digest=draft.content_digest)

    async def publish_version(self, version: GuidelineVersionV1, *, expected_revision: int,
                              mutation: GuidelineMutation):
        version = GuidelineVersionV1.model_validate(version.model_dump())
        version.content.require_registered_parameters(self._registry)
        mutation.require_request(GuidelineOperationKind.PUBLISH, publish_body(version, expected_revision))
        identity = version.identity
        async with self._connection(write=True) as connection:
            if receipt := await self._receipt(connection, mutation):
                return receipt
            family = await self._family(connection, identity.family_root_id)
            if identity.origin is GuidelineOrigin.BUNDLED and not family:
                await self._insert_document(connection, identity)
                family = await self._family(connection, identity.family_root_id)
            document = family.get(identity.guideline_id)
            self._require_revision(document, expected_revision)
            root = family[identity.family_root_id]
            if document["identity"] != identity.model_dump(mode="json"):
                raise GuidelineConflictError("guideline_identity_conflict")
            if identity.origin is not GuidelineOrigin.BUNDLED:
                if root["disabled"] or document["draft_digest"] != version.content.content_digest:
                    raise GuidelineConflictError("guideline_draft_conflict")
            if document["pending_version_id"] is not None:
                live = await connection.scalar(text(
                    "SELECT EXISTS (SELECT 1 FROM guideline_operations WHERE document_id = :doc "
                    "AND version_id = :version AND state IN ('pending', 'running'))"
                ), {"doc": identity.guideline_id, "version": document["pending_version_id"]})
                if live:
                    raise GuidelineConflictError("guideline_publication_pending")
            next_number = await connection.scalar(text(
                "SELECT COALESCE(MAX(version_number), 0) + 1 FROM guideline_versions WHERE document_id = :id"
            ), {"id": identity.guideline_id})
            if version.version_number != next_number:
                raise GuidelineConflictError("guideline_version_conflict")
            if identity.origin is not GuidelineOrigin.BUNDLED:
                await self._supersede(connection, identity.family_root_id)
                epoch = root["activation_epoch"] + 1
                await connection.execute(text(
                    "UPDATE guideline_documents SET activation_epoch = :epoch, "
                    "revision = revision + :bump WHERE document_id = :id"
                ), {"id": identity.family_root_id, "epoch": epoch,
                    "bump": int(identity.guideline_id != identity.family_root_id)})
            else:
                epoch = root["activation_epoch"]
            await connection.execute(text(
                "INSERT INTO guideline_versions (version_id, document_id, family_root_id, version_number, "
                "content_digest, snapshot) VALUES (:id, :doc, :root, :number, :digest, CAST(:body AS jsonb))"
            ), {"id": version.version_id, "doc": identity.guideline_id, "root": identity.family_root_id,
                "number": version.version_number, "digest": version.content.content_digest, "body": version.model_dump_json()})
            await connection.execute(text(
                "UPDATE guideline_documents SET pending_version_id = :version, revision = revision + 1 "
                "WHERE document_id = :id"
            ), {"id": identity.guideline_id, "version": version.version_id})
            return await self._accept(connection, mutation, document_id=identity.guideline_id,
                                      version_id=version.version_id, revision=expected_revision + 1,
                                      content_digest=version.content.content_digest, pending=True, epoch=epoch)

    async def _supersede(self, connection, root_id):
        await connection.execute(text(
            "UPDATE guideline_operations SET state = 'superseded', lease_owner = NULL, "
            "lease_until = NULL, safe_error_code = NULL WHERE document_id IN "
            "(SELECT document_id FROM guideline_documents WHERE family_root_id = :root) "
            "AND state IN ('pending', 'running', 'failed')"
        ), {"root": root_id})
        await connection.execute(text(
            "UPDATE guideline_documents SET pending_version_id = NULL WHERE family_root_id = :root"
        ), {"root": root_id})

    async def read_catalog(self) -> GuidelineCatalogSnapshot:
        async with self._connection() as connection:
            row = (await connection.execute(text(
                "SELECT c.revision, c.active_generation_id, g.profile FROM guideline_catalog_state c "
                "LEFT JOIN guideline_index_generations g ON g.generation_id = c.active_generation_id "
                "WHERE c.singleton_id = 1"
            ))).mappings().one()
            return GuidelineCatalogSnapshot(revision=row["revision"], generation_id=row["active_generation_id"],
                                            profile=row["profile"])

    async def read_effective_versions(self, snapshot: GuidelineCatalogSnapshot,
                                      parameter_keys: tuple[ParameterKey, ...]) -> tuple[GuidelineVersionV1, ...]:
        async with self._connection() as connection:
            bodies = (await connection.execute(text(
                "SELECT v.snapshot FROM guideline_catalog_entries e JOIN guideline_versions v "
                "ON v.version_id = e.version_id WHERE e.valid_from_revision <= :revision "
                "AND (e.valid_to_revision IS NULL OR :revision < e.valid_to_revision) "
                "ORDER BY e.family_root_id"
            ), {"revision": snapshot.revision})).scalars()
            versions = tuple(GuidelineVersionV1.model_validate(body) for body in bodies)
            keys = set(parameter_keys)
            return tuple(v for v in versions if keys.intersection(v.content.parameter_keys))

    @staticmethod
    def _generation(row):
        return GuidelineIndexGeneration(generation_id=row["generation_id"], profile=row["profile"],
                                        state=row["state"], catalog_revision=row["catalog_revision"])

    async def read_generation(self, generation_id: UUID) -> GuidelineIndexGeneration | None:
        async with self._connection() as connection:
            row = (await connection.execute(text(
                "SELECT * FROM guideline_index_generations WHERE generation_id = :id"
            ), {"id": generation_id})).mappings().one_or_none()
            return None if row is None else self._generation(row)

    async def create_generation(self, profile: GuidelineEmbeddingProfileV1) -> GuidelineIndexGeneration:
        profile = GuidelineEmbeddingProfileV1.model_validate(profile.model_dump())
        async with self._connection(write=True) as connection:
            building = (await connection.execute(text(
                "SELECT * FROM guideline_index_generations WHERE state = 'building'"
            ))).mappings().one_or_none()
            if building is not None:
                if building["profile_id"] == profile.profile_id:
                    return self._generation(building)
                raise GuidelineConflictError("guideline_generation_building")
            revision = await connection.scalar(text("SELECT revision FROM guideline_catalog_state WHERE singleton_id = 1"))
            row = (await connection.execute(text(
                "INSERT INTO guideline_index_generations "
                "(generation_id, profile, profile_id, profile_digest, dimensions, state, catalog_revision) "
                "VALUES (:id, CAST(:body AS jsonb), :profile, :digest, :dim, 'building', :revision) RETURNING *"
            ), {"id": uuid4(), "body": profile.model_dump_json(), "profile": profile.profile_id,
                "digest": profile.profile_digest, "dim": profile.output_dimensions, "revision": revision})).mappings().one()
            return self._generation(row)

    async def activate_generation(self, generation_id: UUID, *, expected_catalog_revision: int):
        async with self._connection(write=True) as connection:
            revision = await connection.scalar(text("SELECT revision FROM guideline_catalog_state WHERE singleton_id = 1"))
            self._require_revision({"revision": revision}, expected_catalog_revision)
            row = (await connection.execute(text(
                "SELECT * FROM guideline_index_generations WHERE generation_id = :id FOR UPDATE"
            ), {"id": generation_id})).mappings().one_or_none()
            if row is None or row["state"] != "building":
                raise GuidelineConflictError("guideline_generation_conflict")
            missing = await connection.scalar(text(
                "SELECT EXISTS (SELECT 1 FROM guideline_catalog_entries e "
                "WHERE e.valid_to_revision IS NULL AND NOT EXISTS (SELECT 1 FROM guideline_embeddings b "
                "WHERE b.generation_id = :id AND b.version_id = e.version_id))"
            ), {"id": generation_id})
            if missing:
                raise GuidelineConflictError("guideline_generation_incomplete")
            await connection.execute(text("UPDATE guideline_index_generations SET state = 'retired' WHERE state = 'active'"))
            await connection.execute(text("UPDATE guideline_index_generations SET state = 'active' WHERE generation_id = :id"), {"id": generation_id})
            await connection.execute(text(
                "UPDATE guideline_catalog_state SET active_generation_id = :id, revision = revision + 1 WHERE singleton_id = 1"
            ), {"id": generation_id})

    @staticmethod
    def _now(now: datetime | None) -> datetime:
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("lease clock must be UTC")
        return now

    async def claim_operation(self, owner: str, *, now: datetime | None = None, lease_seconds: int = 60):
        if not owner.strip() or len(owner) > 160 or type(lease_seconds) is not int or not 1 <= lease_seconds <= 3600:
            raise ValueError("invalid guideline claim lease")
        async with self._connection(write=True) as connection:
            scan_time = self._now(now)
            candidates = (await connection.execute(text(
                "SELECT * FROM guideline_operations WHERE (state = 'pending' AND next_attempt_at <= :now) "
                "OR (state = 'running' AND lease_until <= :now) ORDER BY next_attempt_at, operation_id"
            ), {"now": scan_time})).mappings().all()
            for row in candidates:
                root_id, family = await self._document_family(connection, row["document_id"])
                root, document = family[root_id], family[row["document_id"]]
                await connection.execute(text("SELECT operation_id FROM guideline_operations WHERE operation_id = :id FOR UPDATE"), {"id": row["operation_id"]})
                if (root["activation_epoch"] != row["activation_epoch"]
                        or document["pending_version_id"] != row["version_id"]):
                    state, error = "superseded", None
                elif row["attempts"] >= DEFAULT_GUIDELINE_POLICY.max_attempts:
                    state, error = "failed", "guideline_attempts_exhausted"
                else:
                    claimed = (await connection.execute(text(
                        "UPDATE guideline_operations SET state = 'running', attempts = attempts + 1, "
                        "lease_owner = :owner, lease_until = :until WHERE operation_id = :id RETURNING *"
                    ), {"id": row["operation_id"], "owner": owner,
                        "until": self._now(now) + timedelta(seconds=lease_seconds)})).mappings().one()
                    return self._operation(claimed)
                await connection.execute(text(
                    "UPDATE guideline_operations SET state = :state, safe_error_code = :error, "
                    "lease_owner = NULL, lease_until = NULL WHERE operation_id = :id"
                ), {"id": row["operation_id"], "state": state, "error": error})
            return None

    async def _fence(self, connection, claim: GuidelineOperation, now: datetime | None):
        root_id, family = await self._document_family(connection, claim.receipt.document_id)
        row = (await connection.execute(text(
            "SELECT * FROM guideline_operations WHERE operation_id = :id FOR UPDATE"
        ), {"id": claim.receipt.operation_id})).mappings().one_or_none()
        now = self._now(now)
        if (row is None or row["state"] != "running" or claim.state.value != "running"
                or row["attempts"] != claim.attempts or row["lease_owner"] != claim.lease_owner
                or row["lease_until"] != claim.lease_until or row["lease_until"] <= now):
            raise GuidelineConflictError("guideline_stale_attempt")
        if (row["activation_epoch"] != family[root_id]["activation_epoch"]
                or row["version_id"] != family[row["document_id"]]["pending_version_id"]):
            raise GuidelineConflictError("guideline_superseded")
        return row, root_id, family

    async def fail_attempt(self, claim: GuidelineOperation, safe_error_code: str, *, retryable: bool,
                           now: datetime | None = None):
        # Reuse the public safe-error contract before touching durable state.
        GuidelineOperation(receipt=claim.receipt, state="failed", safe_error_code=safe_error_code)
        async with self._connection(write=True) as connection:
            row, _, _ = await self._fence(connection, claim, now)
            retry = retryable and row["attempts"] < DEFAULT_GUIDELINE_POLICY.max_attempts
            await connection.execute(text(
                "UPDATE guideline_operations SET state = :state, safe_error_code = :error, "
                "lease_owner = NULL, lease_until = NULL, next_attempt_at = :next WHERE operation_id = :id"
            ), {"id": row["operation_id"], "state": "pending" if retry else "failed",
                "error": None if retry else safe_error_code,
                "next": self._now(now) + timedelta(seconds=DEFAULT_GUIDELINE_POLICY.retry_backoff_seconds[
                    min(row["attempts"], len(DEFAULT_GUIDELINE_POLICY.retry_backoff_seconds)) - 1])})

    async def retry_operation(self, operation_id: UUID, *, expected_revision: int, mutation: GuidelineMutation):
        mutation.require_request(GuidelineOperationKind.RETRY,
                                 {"operation_id": str(operation_id), "expected_revision": expected_revision})
        async with self._connection(write=True) as connection:
            if receipt := await self._receipt(connection, mutation):
                return receipt
            old = (await connection.execute(text("SELECT * FROM guideline_operations WHERE operation_id = :id"), {"id": operation_id})).mappings().one_or_none()
            if old is None:
                raise GuidelineConflictError("guideline_operation_missing")
            root_id, family = await self._document_family(connection, old["document_id"])
            document, root = family[old["document_id"]], family[root_id]
            self._require_revision(document, expected_revision)
            await connection.execute(text("SELECT operation_id FROM guideline_operations WHERE operation_id = :id FOR UPDATE"), {"id": operation_id})
            if (old["state"] != "failed" or root["disabled"] or old["activation_epoch"] != root["activation_epoch"]
                    or document["pending_version_id"] != old["version_id"]):
                raise GuidelineConflictError("guideline_retry_conflict")
            await connection.execute(text(
                "UPDATE guideline_operations SET state = 'superseded', safe_error_code = NULL WHERE operation_id = :id"
            ), {"id": operation_id})
            return await self._accept(connection, mutation, document_id=old["document_id"], version_id=old["version_id"],
                                      revision=expected_revision, content_digest=old["accepted_response"]["content_digest"],
                                      pending=True, epoch=root["activation_epoch"])

    async def write_embedding(self, claim: GuidelineOperation, generation_id: UUID,
                              canonical_document: CanonicalGuidelineDocumentV1, vector: EmbeddingVector,
                              *, now: datetime | None = None):
        async with self._connection(write=True) as connection:
            row, _, _ = await self._fence(connection, claim, now)
            await self._write_embedding_row(connection, generation_id, row["version_id"], canonical_document, vector,
                                            allowed_states={"building", "active"})
            await connection.execute(text("UPDATE guideline_operations SET generation_id = :gen WHERE operation_id = :id"),
                                     {"gen": generation_id, "id": row["operation_id"]})

    async def write_building_embedding(self, generation_id: UUID, version_id: UUID,
                                       canonical_document: CanonicalGuidelineDocumentV1, vector: EmbeddingVector):
        """Backfill immutable derived rows; this cannot modify an active catalog.

        A duplicate keeps the first stored vector. Only an independently complete
        generation may subsequently be activated under catalog CAS.
        """
        async with self._connection(write=True) as connection:
            await self._write_embedding_row(connection, generation_id, version_id, canonical_document, vector,
                                            allowed_states={"building"})

    async def _write_embedding_row(self, connection, generation_id, version_id, canonical_document, vector,
                                   *, allowed_states):
        generation = (await connection.execute(text(
            "SELECT * FROM guideline_index_generations WHERE generation_id = :id"
        ), {"id": generation_id})).mappings().one_or_none()
        if generation is None or generation["state"] not in allowed_states:
            raise GuidelineConflictError("guideline_generation_conflict")
        profile = GuidelineEmbeddingProfileV1.model_validate(generation["profile"])
        GuidelineEmbeddingBatch.model_validate({"profile": profile.model_dump(), "vectors": [vector.model_dump()]})
        body = await connection.scalar(text("SELECT snapshot FROM guideline_versions WHERE version_id = :id"), {"id": version_id})
        if body is None:
            raise GuidelineConflictError("guideline_version_conflict")
        version = GuidelineVersionV1.model_validate(body)
        expected = CanonicalGuidelineDocumentV1.from_content(version.content, profile, self._registry)
        if canonical_document != expected:
            raise ValueError("embedding document differs from the approved version/profile")
        existing = await connection.scalar(text(
            "SELECT canonical_document_digest FROM guideline_embeddings WHERE generation_id = :gen AND version_id = :version"
        ), {"gen": generation_id, "version": version_id})
        if existing is None:
            await connection.execute(text(
                "INSERT INTO guideline_embeddings (generation_id, version_id, profile_id, dimensions, "
                "canonical_document_digest, embedding) VALUES (:gen, :version, :profile, :dim, :digest, CAST(:vector AS public.vector))"
            ), {"gen": generation_id, "version": version_id, "profile": profile.profile_id,
                "dim": profile.output_dimensions, "digest": expected.canonical_digest,
                "vector": canonical_text(list(vector.values))})
        elif existing != expected.canonical_digest:
            raise GuidelineConflictError("guideline_embedding_conflict")

    async def _replace_catalog(self, connection, root_id, version_id):
        revision = await connection.scalar(text(
            "UPDATE guideline_catalog_state SET revision = revision + 1 WHERE singleton_id = 1 RETURNING revision"
        ))
        await connection.execute(text(
            "UPDATE guideline_catalog_entries SET valid_to_revision = :revision "
            "WHERE family_root_id = :root AND valid_to_revision IS NULL"
        ), {"root": root_id, "revision": revision})
        if version_id is not None:
            await connection.execute(text(
                "INSERT INTO guideline_catalog_entries (family_root_id, version_id, valid_from_revision) "
                "VALUES (:root, :version, :revision)"
            ), {"root": root_id, "version": version_id, "revision": revision})

    async def _require_ready(self, connection, version_id):
        ready = await connection.scalar(text(
            "SELECT EXISTS (SELECT 1 FROM guideline_embeddings b JOIN guideline_catalog_state c "
            "ON c.active_generation_id = b.generation_id WHERE c.singleton_id = 1 AND b.version_id = :id)"
        ), {"id": version_id})
        if not ready:
            raise GuidelineConflictError("guideline_embedding_not_ready")

    async def _select_effective(self, connection, root_id):
        return await connection.scalar(text(
            "SELECT v.version_id FROM guideline_documents d JOIN guideline_documents r ON r.document_id = d.family_root_id "
            "JOIN guideline_versions v ON v.version_id = d.active_version_id "
            "JOIN guideline_catalog_state c ON c.singleton_id = 1 "
            "JOIN guideline_embeddings b ON b.version_id = v.version_id AND b.generation_id = c.active_generation_id "
            "WHERE d.family_root_id = :root AND NOT r.disabled "
            "ORDER BY CASE WHEN d.origin = 'local_override' THEN 0 ELSE 1 END, v.version_number DESC LIMIT 1"
        ), {"root": root_id})

    async def _refresh_catalog(self, connection, root_id):
        selected = await self._select_effective(connection, root_id)
        current = await connection.scalar(text(
            "SELECT version_id FROM guideline_catalog_entries WHERE family_root_id = :root AND valid_to_revision IS NULL"
        ), {"root": root_id})
        if current != selected:
            await self._replace_catalog(connection, root_id, selected)
        return selected

    async def activate_version(self, claim: GuidelineOperation, *, now: datetime | None = None):
        async with self._connection(write=True) as connection:
            row, root_id, family = await self._fence(connection, claim, now)
            await self._require_ready(connection, row["version_id"])
            await connection.execute(text(
                "UPDATE guideline_documents SET active_version_id = :version, pending_version_id = NULL WHERE document_id = :id"
            ), {"id": row["document_id"], "version": row["version_id"]})
            await self._refresh_catalog(connection, root_id)
            await connection.execute(text(
                "UPDATE guideline_operations SET state = 'succeeded', lease_owner = NULL, lease_until = NULL WHERE operation_id = :id"
            ), {"id": row["operation_id"]})

    async def set_enabled(self, document_id: UUID, enabled: bool, *, expected_revision: int, mutation: GuidelineMutation):
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        mutation.require_request(GuidelineOperationKind.SET_ENABLED, action_body(document_id, expected_revision, enabled=enabled))
        async with self._connection(write=True) as connection:
            if receipt := await self._receipt(connection, mutation):
                return receipt
            root_id, family = await self._document_family(connection, document_id)
            if document_id != root_id:
                raise GuidelineConflictError("guideline_root_required")
            root = family[root_id]
            self._require_revision(root, expected_revision)
            previous = await connection.scalar(text(
                "SELECT version_id FROM guideline_catalog_entries WHERE family_root_id = :root "
                "ORDER BY valid_from_revision DESC LIMIT 1"
            ), {"root": root_id})
            await self._supersede(connection, root_id)
            await connection.execute(text(
                "UPDATE guideline_documents SET disabled = :disabled, activation_epoch = activation_epoch + 1, "
                "revision = revision + 1 WHERE document_id = :id"
            ), {"id": root_id, "disabled": not enabled})
            selected = await self._refresh_catalog(connection, root_id)
            if enabled and previous is not None and selected is None:
                raise GuidelineConflictError("guideline_embedding_not_ready")
            return await self._accept(connection, mutation, document_id=root_id, revision=expected_revision + 1)

    async def restore_bundled(self, document_id: UUID, version_id: UUID, *, expected_revision: int, mutation: GuidelineMutation):
        mutation.require_request(GuidelineOperationKind.RESTORE_BUNDLED,
                                 action_body(document_id, expected_revision, version_id=str(version_id)))
        async with self._connection(write=True) as connection:
            if receipt := await self._receipt(connection, mutation):
                return receipt
            root_id, family = await self._document_family(connection, document_id)
            root = family[root_id]
            self._require_revision(root, expected_revision)
            if root_id != document_id or root["origin"] != "bundled":
                raise GuidelineConflictError("guideline_base_missing")
            digest = await connection.scalar(text(
                "SELECT content_digest FROM guideline_versions WHERE document_id = :doc AND version_id = :version"
            ), {"doc": root_id, "version": version_id})
            if digest is None:
                raise GuidelineConflictError("guideline_version_conflict")
            await self._require_ready(connection, version_id)
            latest = await connection.scalar(text(
                "SELECT v.version_id FROM guideline_versions v JOIN guideline_embeddings b ON b.version_id = v.version_id "
                "JOIN guideline_catalog_state c ON c.active_generation_id = b.generation_id AND c.singleton_id = 1 "
                "WHERE v.document_id = :doc AND EXISTS (SELECT 1 FROM guideline_operations o "
                "WHERE o.version_id = v.version_id AND o.state = 'succeeded') "
                "ORDER BY v.version_number DESC LIMIT 1"
            ), {"doc": root_id})
            if latest != version_id:
                raise GuidelineConflictError("guideline_bundle_target_changed")
            await self._supersede(connection, root_id)
            await connection.execute(text(
                "UPDATE guideline_documents SET disabled = false, activation_epoch = activation_epoch + 1, "
                "revision = revision + 1, active_version_id = :version WHERE document_id = :id"
            ), {"id": root_id, "version": version_id})
            await connection.execute(text(
                "UPDATE guideline_documents SET active_version_id = NULL WHERE family_root_id = :root AND origin = 'local_override'"
            ), {"root": root_id})
            await self._refresh_catalog(connection, root_id)
            return await self._accept(connection, mutation, document_id=root_id, version_id=version_id,
                                      revision=expected_revision + 1, content_digest=digest)

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import json
from uuid import UUID, uuid4

from psycopg.types.range import Range
from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.domain import (
    EmbeddingProfile,
    RAGCaseProjectionV1,
    RAGFragmentProjectionV1,
    RAGIndexGeneration,
    RAGIndexGenerationStatus,
    RAGIndexJob,
    RAGIndexJobStatus,
    RAGJobCounts,
)
from app.infrastructure.persistence.records import RAGConsumptionLinkRecord
from app.infrastructure.persistence.schema import (
    rag_case_projections,
    rag_case_windows,
    rag_consumption_links,
    rag_fragment_projections,
    rag_index_generations,
    rag_index_jobs,
)
from app.ports.errors import PortUnavailableError


class RAGPersistenceConflictError(RuntimeError):
    """A persisted RAG identity or state transition conflicts with the request."""


def _uuid(value: str | UUID, field_name: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be a UUID") from error


def _parameter_key_text(parameter) -> str:
    return json.dumps(
        parameter.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _generation_from_row(row) -> RAGIndexGeneration:
    profile = EmbeddingProfile(
        profile_id=row["profile_id"],
        provider=row["provider"],
        model=row["model"],
        output_dimensions=row["output_dimensions"],
        max_input_tokens=row["max_input_tokens"],
        distance_metric=row["distance_metric"],
        semantic_min_similarity=row["semantic_min_similarity"],
        canonical_query_schema_version=row["canonical_query_schema_version"],
        canonical_template_version=row["canonical_template_version"],
        projection_schema_version=row["projection_schema_version"],
        profile_digest=row["profile_digest"],
    )
    return RAGIndexGeneration(
        generation_id=str(row["generation_id"]),
        profile=profile,
        status=row["status"],
        source_cutoff_created_at=row["source_cutoff_created_at"],
        source_cutoff_item_id=str(row["source_cutoff_item_id"]),
        created_at=row["created_at"],
        activated_at=row["activated_at"],
        retired_at=row["retired_at"],
        failed_at=row["failed_at"],
        last_safe_error_code=row["last_safe_error_code"],
    )


def _job_from_row(row) -> RAGIndexJob:
    return RAGIndexJob(
        job_id=str(row["job_id"]),
        generation_id=str(row["generation_id"]),
        source_run_id=str(row["source_run_id"]),
        source_session_id=str(row["source_session_id"]),
        source_result_item_id=str(row["source_result_item_id"]),
        projection_schema_version=row["projection_schema_version"],
        status=row["status"],
        attempts=row["attempts"],
        next_attempt_at=row["next_attempt_at"],
        lease_owner=row["lease_owner"],
        lease_until=row["lease_until"],
        last_safe_error_code=row["last_safe_error_code"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
    )


class PostgresRAGRepository:
    """Transaction owner for M8 generation, job, projection, and ownership rows."""

    def __init__(self, engine: AsyncEngine, *, operation_timeout_seconds: float) -> None:
        if operation_timeout_seconds <= 0:
            raise ValueError("operation_timeout_seconds must be positive")
        self._engine = engine
        self._operation_timeout_seconds = operation_timeout_seconds

    async def create_building_generation(
        self,
        profile: EmbeddingProfile,
        source_cutoff_created_at: datetime,
        source_cutoff_item_id: str,
    ) -> RAGIndexGeneration:
        now = datetime.now(UTC)
        generation = RAGIndexGeneration(
            generation_id=str(uuid4()),
            profile=profile,
            status=RAGIndexGenerationStatus.BUILDING,
            source_cutoff_created_at=source_cutoff_created_at,
            source_cutoff_item_id=source_cutoff_item_id,
            created_at=now,
        )
        values = self._generation_values(generation)
        try:
            async with self._transaction() as connection:
                await connection.execute(insert(rag_index_generations).values(**values))
        except IntegrityError as error:
            raise RAGPersistenceConflictError(
                "a building generation already exists or its identity is invalid"
            ) from error
        return generation

    async def read_generation(self, generation_id: str) -> RAGIndexGeneration | None:
        row = await self._read_one(
            select(rag_index_generations).where(
                rag_index_generations.c.generation_id
                == _uuid(generation_id, "generation_id")
            )
        )
        return None if row is None else _generation_from_row(row)

    async def list_generations(self) -> tuple[RAGIndexGeneration, ...]:
        rows = await self._read_all(
            select(rag_index_generations).order_by(
                rag_index_generations.c.created_at,
                rag_index_generations.c.generation_id,
            )
        )
        return tuple(_generation_from_row(row) for row in rows)

    async def read_active_generation(self) -> RAGIndexGeneration | None:
        row = await self._read_one(
            select(rag_index_generations).where(
                rag_index_generations.c.status
                == RAGIndexGenerationStatus.ACTIVE.value
            )
        )
        return None if row is None else _generation_from_row(row)

    async def activate_generation(
        self,
        generation_id: str,
        activated_at: datetime,
    ) -> RAGIndexGeneration:
        identifier = _uuid(generation_id, "generation_id")
        try:
            async with self._transaction() as connection:
                row = await self._locked_generation(connection, identifier)
                if row is None or row["status"] != RAGIndexGenerationStatus.BUILDING.value:
                    raise RAGPersistenceConflictError(
                        "only an existing building generation can be activated"
                    )
                await connection.execute(
                    update(rag_index_generations)
                    .where(
                        rag_index_generations.c.status
                        == RAGIndexGenerationStatus.ACTIVE.value
                    )
                    .values(
                        status=RAGIndexGenerationStatus.RETIRED.value,
                        retired_at=activated_at,
                    )
                )
                await connection.execute(
                    update(rag_index_generations)
                    .where(rag_index_generations.c.generation_id == identifier)
                    .values(
                        status=RAGIndexGenerationStatus.ACTIVE.value,
                        activated_at=activated_at,
                    )
                )
                activated = await self._locked_generation(connection, identifier)
        except IntegrityError as error:
            raise RAGPersistenceConflictError(
                "generation activation conflicts with the active generation"
            ) from error
        assert activated is not None
        return _generation_from_row(activated)

    async def retire_generation(
        self,
        generation_id: str,
        retired_at: datetime,
    ) -> RAGIndexGeneration:
        identifier = _uuid(generation_id, "generation_id")
        async with self._transaction() as connection:
            row = await self._locked_generation(connection, identifier)
            if row is None or row["status"] != RAGIndexGenerationStatus.ACTIVE.value:
                raise RAGPersistenceConflictError(
                    "only an existing active generation can be retired"
                )
            await connection.execute(
                update(rag_index_generations)
                .where(rag_index_generations.c.generation_id == identifier)
                .values(
                    status=RAGIndexGenerationStatus.RETIRED.value,
                    retired_at=retired_at,
                )
            )
            retired = await self._locked_generation(connection, identifier)
        assert retired is not None
        return _generation_from_row(retired)

    async def fail_generation(
        self,
        generation_id: str,
        failed_at: datetime,
        safe_error_code: str,
    ) -> RAGIndexGeneration:
        if not safe_error_code.strip():
            raise ValueError("safe_error_code must not be blank")
        identifier = _uuid(generation_id, "generation_id")
        async with self._transaction() as connection:
            row = await self._locked_generation(connection, identifier)
            if row is None or row["status"] != RAGIndexGenerationStatus.BUILDING.value:
                raise RAGPersistenceConflictError(
                    "only an existing building generation can fail"
                )
            await connection.execute(
                update(rag_index_generations)
                .where(rag_index_generations.c.generation_id == identifier)
                .values(
                    status=RAGIndexGenerationStatus.FAILED.value,
                    failed_at=failed_at,
                    last_safe_error_code=safe_error_code,
                )
            )
            failed = await self._locked_generation(connection, identifier)
        assert failed is not None
        return _generation_from_row(failed)

    async def delete_generation(self, generation_id: str) -> None:
        identifier = _uuid(generation_id, "generation_id")
        async with self._transaction() as connection:
            row = await self._locked_generation(connection, identifier)
            if row is None:
                return
            if row["status"] not in {
                RAGIndexGenerationStatus.RETIRED.value,
                RAGIndexGenerationStatus.FAILED.value,
            }:
                raise RAGPersistenceConflictError(
                    "only retired or failed generations can be deleted"
                )
            await connection.execute(
                delete(rag_index_generations).where(
                    rag_index_generations.c.generation_id == identifier
                )
            )

    async def enqueue(self, job: RAGIndexJob) -> RAGIndexJob:
        if job.status is not RAGIndexJobStatus.PENDING:
            raise ValueError("new RAG jobs must be pending")
        values = self._job_values(job)
        statement = (
            postgresql_insert(rag_index_jobs)
            .values(**values)
            .on_conflict_do_nothing(
                constraint="uq_rag_jobs_identity",
            )
        )
        async with self._transaction() as connection:
            await connection.execute(statement)
            row = (
                await connection.execute(
                    select(rag_index_jobs).where(
                        and_(
                            rag_index_jobs.c.generation_id
                            == values["generation_id"],
                            rag_index_jobs.c.source_result_item_id
                            == values["source_result_item_id"],
                            rag_index_jobs.c.projection_schema_version
                            == values["projection_schema_version"],
                        )
                    )
                )
            ).mappings().one()
        persisted = _job_from_row(row)
        if (
            persisted.source_run_id != job.source_run_id
            or persisted.source_session_id != job.source_session_id
        ):
            raise RAGPersistenceConflictError(
                "existing job identity belongs to a different source scope"
            )
        return persisted

    async def claim(
        self,
        generation_id: str,
        lease_owner: str,
        lease_until: datetime,
        limit: int,
    ) -> tuple[RAGIndexJob, ...]:
        if not lease_owner.strip():
            raise ValueError("lease_owner must not be blank")
        if type(limit) is not int or limit <= 0:
            raise ValueError("limit must be a positive integer")
        now = datetime.now(UTC)
        if lease_until <= now:
            raise ValueError("lease_until must be in the future")
        identifier = _uuid(generation_id, "generation_id")
        async with self._transaction() as connection:
            rows = tuple(
                (
                    await connection.execute(
                        select(rag_index_jobs)
                        .where(
                            rag_index_jobs.c.generation_id == identifier,
                            rag_index_jobs.c.attempts < 5,
                            or_(
                                rag_index_jobs.c.status
                                == RAGIndexJobStatus.PENDING.value,
                                and_(
                                    rag_index_jobs.c.status
                                    == RAGIndexJobStatus.RETRY_WAIT.value,
                                    rag_index_jobs.c.next_attempt_at <= now,
                                ),
                                and_(
                                    rag_index_jobs.c.status
                                    == RAGIndexJobStatus.LEASED.value,
                                    rag_index_jobs.c.lease_until <= now,
                                ),
                            ),
                        )
                        .order_by(
                            rag_index_jobs.c.created_at,
                            rag_index_jobs.c.job_id,
                        )
                        .with_for_update(skip_locked=True)
                        .limit(limit)
                    )
                ).mappings()
            )
            claimed: list[RAGIndexJob] = []
            for row in rows:
                updated = (
                    await connection.execute(
                        update(rag_index_jobs)
                        .where(rag_index_jobs.c.job_id == row["job_id"])
                        .values(
                            status=RAGIndexJobStatus.LEASED.value,
                            attempts=row["attempts"] + 1,
                            next_attempt_at=None,
                            lease_owner=lease_owner,
                            lease_until=lease_until,
                            updated_at=now,
                            completed_at=None,
                        )
                        .returning(rag_index_jobs)
                    )
                ).mappings().one()
                claimed.append(_job_from_row(updated))
        return tuple(claimed)

    async def save(self, job: RAGIndexJob) -> None:
        identifier = _uuid(job.job_id, "job_id")
        values = self._job_values(job)
        async with self._transaction() as connection:
            row = (
                await connection.execute(
                    select(rag_index_jobs)
                    .where(rag_index_jobs.c.job_id == identifier)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if row is None:
                raise RAGPersistenceConflictError("RAG job does not exist")
            persisted = _job_from_row(row)
            self._validate_job_update(persisted, job)
            await connection.execute(
                update(rag_index_jobs)
                .where(rag_index_jobs.c.job_id == identifier)
                .values(**values)
            )

    async def counts(self, generation_id: str) -> RAGJobCounts:
        identifier = _uuid(generation_id, "generation_id")
        rows = await self._read_all(
            select(rag_index_jobs.c.status, func.count().label("count"))
            .where(rag_index_jobs.c.generation_id == identifier)
            .group_by(rag_index_jobs.c.status)
        )
        values = {row["status"]: row["count"] for row in rows}
        return RAGJobCounts(**values)

    async def write_ready_projection(
        self,
        job: RAGIndexJob,
        case: RAGCaseProjectionV1,
        fragments: tuple[RAGFragmentProjectionV1, ...],
    ) -> None:
        self._validate_projection_scope(job, case, fragments)
        windows = self._window_rows(case)
        generation_id = _uuid(case.case_ref.generation_id, "generation_id")
        case_id = _uuid(case.case_ref.case_id, "case_id")
        now = datetime.now(UTC)
        try:
            async with self._transaction() as connection:
                existing = (
                    await connection.execute(
                        select(rag_case_projections.c.projection_digest).where(
                            rag_case_projections.c.generation_id == generation_id,
                            rag_case_projections.c.case_id == case_id,
                        )
                    )
                ).mappings().one_or_none()
                if existing is not None:
                    await self._validate_idempotent_projection(
                        connection, case, fragments, existing["projection_digest"]
                    )
                    return
                await connection.execute(
                    insert(rag_case_projections).values(
                        generation_id=generation_id,
                        case_id=case_id,
                        profile_id=case.profile_id,
                        embedding_dimensions=len(case.embedding.values),
                        source_run_id=_uuid(case.case_ref.run_id, "source_run_id"),
                        source_session_id=_uuid(
                            case.case_ref.session_id, "source_session_id"
                        ),
                        source_result_item_id=_uuid(
                            case.case_ref.result_item_id, "source_result_item_id"
                        ),
                        analysis_context_item_id=_uuid(
                            case.case_ref.analysis_context_item_id,
                            "analysis_context_item_id",
                        ),
                        target_comid=case.case_ref.target_comid,
                        projection_schema_version=case.projection_schema_version,
                        source_content_type=case.source_content_type,
                        source_content_schema_version=case.source_content_schema_version,
                        mode=case.mode.value,
                        parameter_keys=[
                            _parameter_key_text(item) for item in case.parameter_keys
                        ],
                        window_ids=list(case.window_ids),
                        source_completed_at=case.source_completed_at,
                        canonical_query=case.canonical_query,
                        canonical_query_digest=case.canonical_query_digest,
                        search_document=func.to_tsvector("simple", case.canonical_query),
                        embedding=list(case.embedding.values),
                        result_projection=case.result_projection.model_dump(mode="json"),
                        projection_digest=case.projection_digest,
                        ready_at=now,
                    )
                )
                await connection.execute(insert(rag_case_windows), windows)
                if fragments:
                    await connection.execute(
                        insert(rag_fragment_projections),
                        [self._fragment_values(fragment) for fragment in fragments],
                    )
                updated = await connection.execute(
                    update(rag_index_jobs)
                    .where(
                        rag_index_jobs.c.job_id == _uuid(job.job_id, "job_id"),
                        rag_index_jobs.c.status == RAGIndexJobStatus.LEASED.value,
                        rag_index_jobs.c.lease_owner == job.lease_owner,
                    )
                    .values(
                        status=RAGIndexJobStatus.COMPLETED.value,
                        lease_owner=None,
                        lease_until=None,
                        next_attempt_at=None,
                        last_safe_error_code=None,
                        updated_at=now,
                        completed_at=now,
                    )
                )
                if updated.rowcount != 1:
                    raise RAGPersistenceConflictError(
                        "projection job is not leased by the expected owner"
                    )
        except IntegrityError as error:
            raise RAGPersistenceConflictError(
                "projection violates generation or source ownership constraints"
            ) from error

    async def append_consumption_link(
        self,
        link: RAGConsumptionLinkRecord,
    ) -> RAGConsumptionLinkRecord:
        values = link.model_dump(mode="python")
        statement = (
            postgresql_insert(rag_consumption_links)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_rag_consumption_identity")
        )
        try:
            async with self._transaction() as connection:
                await connection.execute(statement)
                row = (
                    await connection.execute(
                        select(rag_consumption_links).where(
                            rag_consumption_links.c.source_generation_id
                            == link.source_generation_id,
                            rag_consumption_links.c.source_case_id
                            == link.source_case_id,
                            rag_consumption_links.c.consumer_item_id
                            == link.consumer_item_id,
                        )
                    )
                ).mappings().one()
        except IntegrityError as error:
            raise RAGPersistenceConflictError(
                "consumption link has no valid source case or consumer item"
            ) from error
        persisted = RAGConsumptionLinkRecord.model_validate(dict(row))
        if persisted != link:
            raise RAGPersistenceConflictError(
                "existing consumption identity has different ownership fields"
            )
        return persisted

    async def read_consumption_links(
        self,
        consumer_run_id: UUID,
    ) -> tuple[RAGConsumptionLinkRecord, ...]:
        rows = await self._read_all(
            select(rag_consumption_links)
            .where(rag_consumption_links.c.consumer_run_id == consumer_run_id)
            .order_by(
                rag_consumption_links.c.created_at,
                rag_consumption_links.c.link_id,
            )
        )
        return tuple(
            RAGConsumptionLinkRecord.model_validate(dict(row)) for row in rows
        )

    async def find_source_delete_conflicts(
        self,
        source_run_ids: tuple[UUID, ...],
    ) -> tuple[RAGConsumptionLinkRecord, ...]:
        if not source_run_ids:
            return ()
        rows = await self._read_all(
            select(rag_consumption_links)
            .join(
                rag_case_projections,
                and_(
                    rag_case_projections.c.generation_id
                    == rag_consumption_links.c.source_generation_id,
                    rag_case_projections.c.case_id
                    == rag_consumption_links.c.source_case_id,
                ),
            )
            .where(
                rag_case_projections.c.source_run_id.in_(source_run_ids),
                rag_consumption_links.c.consumer_run_id.not_in(source_run_ids),
            )
            .order_by(
                rag_consumption_links.c.created_at,
                rag_consumption_links.c.link_id,
            )
        )
        return tuple(
            RAGConsumptionLinkRecord.model_validate(dict(row)) for row in rows
        )

    @staticmethod
    def _generation_values(generation: RAGIndexGeneration) -> dict[str, object]:
        profile = generation.profile
        return {
            "generation_id": _uuid(generation.generation_id, "generation_id"),
            "profile_id": profile.profile_id,
            "provider": profile.provider,
            "model": profile.model,
            "output_dimensions": profile.output_dimensions,
            "max_input_tokens": profile.max_input_tokens,
            "distance_metric": profile.distance_metric.value,
            "semantic_min_similarity": profile.semantic_min_similarity,
            "canonical_query_schema_version": profile.canonical_query_schema_version,
            "canonical_template_version": profile.canonical_template_version,
            "projection_schema_version": profile.projection_schema_version,
            "profile_digest": profile.profile_digest,
            "status": generation.status.value,
            "source_cutoff_created_at": generation.source_cutoff_created_at,
            "source_cutoff_item_id": _uuid(
                generation.source_cutoff_item_id, "source_cutoff_item_id"
            ),
            "created_at": generation.created_at,
            "activated_at": generation.activated_at,
            "retired_at": generation.retired_at,
            "failed_at": generation.failed_at,
            "last_safe_error_code": generation.last_safe_error_code,
        }

    @staticmethod
    def _job_values(job: RAGIndexJob) -> dict[str, object]:
        return {
            "job_id": _uuid(job.job_id, "job_id"),
            "generation_id": _uuid(job.generation_id, "generation_id"),
            "source_run_id": _uuid(job.source_run_id, "source_run_id"),
            "source_session_id": _uuid(job.source_session_id, "source_session_id"),
            "source_result_item_id": _uuid(
                job.source_result_item_id, "source_result_item_id"
            ),
            "projection_schema_version": job.projection_schema_version,
            "status": job.status.value,
            "attempts": job.attempts,
            "next_attempt_at": job.next_attempt_at,
            "lease_owner": job.lease_owner,
            "lease_until": job.lease_until,
            "last_safe_error_code": job.last_safe_error_code,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "completed_at": job.completed_at,
        }

    @staticmethod
    def _validate_job_update(persisted: RAGIndexJob, submitted: RAGIndexJob) -> None:
        immutable = (
            "generation_id",
            "source_run_id",
            "source_session_id",
            "source_result_item_id",
            "projection_schema_version",
            "created_at",
        )
        if any(getattr(persisted, field) != getattr(submitted, field) for field in immutable):
            raise RAGPersistenceConflictError("RAG job immutable identity changed")
        if submitted.attempts < persisted.attempts:
            raise RAGPersistenceConflictError("RAG job attempts cannot decrease")
        if submitted == persisted:
            return
        if submitted.updated_at <= persisted.updated_at:
            raise RAGPersistenceConflictError("stale RAG job update")
        if persisted.status is RAGIndexJobStatus.LEASED:
            if submitted.lease_owner is not None and (
                submitted.lease_owner != persisted.lease_owner
            ):
                raise RAGPersistenceConflictError("RAG job lease owner changed")
        elif submitted.status is not persisted.status:
            raise RAGPersistenceConflictError(
                "only a leased RAG job may transition through save"
            )

    @staticmethod
    def _validate_projection_scope(
        job: RAGIndexJob,
        case: RAGCaseProjectionV1,
        fragments: tuple[RAGFragmentProjectionV1, ...],
    ) -> None:
        reference = case.case_ref
        if job.status is not RAGIndexJobStatus.LEASED:
            raise ValueError("projection writes require a leased job")
        if (
            job.generation_id != reference.generation_id
            or job.source_run_id != reference.run_id
            or job.source_session_id != reference.session_id
            or job.source_result_item_id != reference.result_item_id
            or job.projection_schema_version != case.projection_schema_version
        ):
            raise ValueError("job and case projection identities do not match")
        if len(case.embedding.values) < 1:
            raise ValueError("case embedding must not be empty")
        if set(case.window_ids) != {
            cell.window_id for cell in case.result_projection.cells
        }:
            raise ValueError("case windows must match result projection windows")
        if set(case.parameter_keys) != {
            cell.parameter for cell in case.result_projection.cells
        }:
            raise ValueError("case parameters must match result projection parameters")
        identities: set[str] = set()
        for fragment in fragments:
            if fragment.case_ref != reference:
                raise ValueError("fragment belongs to a different case")
            if fragment.source_fragment_item_id in identities:
                raise ValueError("duplicate source fragment projection")
            identities.add(fragment.source_fragment_item_id)
            if fragment.window_id not in case.window_ids:
                raise ValueError("fragment window is outside case projection")
            if fragment.parameter not in case.parameter_keys:
                raise ValueError("fragment parameter is outside case projection")

    @staticmethod
    def _window_rows(case: RAGCaseProjectionV1) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []
        for window in case.windows:
            if len(window.season_buckets) != 1:
                raise ValueError("persisted case windows require one season bucket")
            rows.append(
                {
                    "generation_id": _uuid(
                        case.case_ref.generation_id, "generation_id"
                    ),
                    "case_id": _uuid(case.case_ref.case_id, "case_id"),
                    "window_id": window.window_id,
                    "time_range": Range(
                        window.time_range.start,
                        window.time_range.end,
                        "[)",
                    ),
                    "local_months": list(window.local_months),
                    "season_bucket": window.season_buckets[0].value,
                    "season_year": window.season_year,
                    "duration_seconds": window.duration_seconds,
                    "resolution": window.resolution.value,
                    "parameter_keys": sorted(
                        {_parameter_key_text(item) for item in window.parameter_keys}
                    ),
                }
            )
        return rows

    @staticmethod
    def _fragment_values(fragment: RAGFragmentProjectionV1) -> dict[str, object]:
        return {
            "generation_id": _uuid(fragment.case_ref.generation_id, "generation_id"),
            "case_id": _uuid(fragment.case_ref.case_id, "case_id"),
            "source_fragment_item_id": _uuid(
                fragment.source_fragment_item_id, "source_fragment_item_id"
            ),
            "source_run_id": _uuid(fragment.case_ref.run_id, "source_run_id"),
            "source_session_id": _uuid(
                fragment.case_ref.session_id, "source_session_id"
            ),
            "evidence_id": fragment.evidence_id,
            "projection_schema_version": fragment.projection_schema_version,
            "source_content_type": fragment.source_content_type,
            "source_content_schema_version": fragment.source_content_schema_version,
            "parameter_key": _parameter_key_text(fragment.parameter),
            "window_id": fragment.window_id,
            "valid_time_range": Range(
                fragment.valid_time_range.start,
                fragment.valid_time_range.end,
                "[)",
            ),
            "quality_tier": fragment.quality_tier.value,
            "applicability": fragment.applicability,
            "provenance_identity": fragment.provenance_identity,
            "safe_projection": fragment.model_dump(mode="json"),
            "projection_digest": fragment.projection_digest,
        }

    @staticmethod
    async def _validate_idempotent_projection(
        connection: AsyncConnection,
        case: RAGCaseProjectionV1,
        fragments: tuple[RAGFragmentProjectionV1, ...],
        stored_case_digest: str,
    ) -> None:
        if stored_case_digest != case.projection_digest:
            raise RAGPersistenceConflictError(
                "existing case identity has a different projection digest"
            )
        rows = tuple(
            (
                await connection.execute(
                    select(
                        rag_fragment_projections.c.source_fragment_item_id,
                        rag_fragment_projections.c.projection_digest,
                    ).where(
                        rag_fragment_projections.c.generation_id
                        == _uuid(case.case_ref.generation_id, "generation_id"),
                        rag_fragment_projections.c.case_id
                        == _uuid(case.case_ref.case_id, "case_id"),
                    )
                )
            ).mappings()
        )
        stored = {
            str(row["source_fragment_item_id"]): row["projection_digest"] for row in rows
        }
        submitted = {
            fragment.source_fragment_item_id: fragment.projection_digest
            for fragment in fragments
        }
        if stored != submitted:
            raise RAGPersistenceConflictError(
                "existing case identity has different fragment projections"
            )

    async def _locked_generation(
        self,
        connection: AsyncConnection,
        generation_id: UUID,
    ):
        return (
            await connection.execute(
                select(rag_index_generations)
                .where(rag_index_generations.c.generation_id == generation_id)
                .with_for_update()
            )
        ).mappings().one_or_none()

    async def _read_one(self, statement):
        rows = await self._read_all(statement)
        if len(rows) > 1:
            raise RAGPersistenceConflictError("RAG read expected at most one row")
        return rows[0] if rows else None

    async def _read_all(self, statement) -> tuple:
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                async with self._engine.connect() as connection:
                    return tuple((await connection.execute(statement)).mappings())
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise PortUnavailableError(
                "RAG persistence is unavailable",
                provider="postgresql",
                operation="rag_read",
            ) from error

    @asynccontextmanager
    async def _transaction(self):
        try:
            async with asyncio.timeout(self._operation_timeout_seconds):
                async with self._engine.begin() as connection:
                    yield connection
        except IntegrityError:
            raise
        except (SQLAlchemyError, OSError, TimeoutError) as error:
            raise PortUnavailableError(
                "RAG persistence is unavailable",
                provider="postgresql",
                operation="rag_write",
            ) from error

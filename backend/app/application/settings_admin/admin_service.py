"""Confirmed local Admin actions over the existing M8 maintenance graph."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from hashlib import sha256
from hmac import compare_digest
from uuid import UUID, uuid4

from app.domain.settings_admin import AdminCommand
from app.application.settings_admin.confirmation_registry import ConfirmationRegistry
from app.application.settings_admin.errors import (
    AdminConfirmationInvalidError, AdminOperationNotFoundError,
    AdminPreviewStaleError, HistoryCleanupDependencyConflictError,
    HistoryCleanupSelectionTooLargeError,
    CredentialStoreUnavailableError,
    RAGGenerationConflictError, RAGGenerationNotFoundError,
    RAGReaderDrainRequiredError, RAGRetentionNotElapsedError,
    UnitPolicyIdempotencyConflictError,
)
from app.application.settings_admin.settings_service import SettingsService
from app.application.transcript_memory.maintenance import (
    HistoryStorageFacts, RAGMaintenanceConflictError, RAGMaintenanceOperations,
)
from app.domain import EmbeddingProfile, RAGFailedJobRetryFilter, RAGIndexGenerationStatus
from app.domain.memory_rag import RAGCleanupPlan, RAGProfileHandoffState
from app.domain.settings_admin import (
    AdminActionKind, AdminConfirmation, AdminOperation, AdminOperationState,
    AdminProgress, GenerationDeletionResult, HistoryCleanupResult,
    ProfileHandoffResult, RetryFailedJobsResult,
)
from app.ports.admin_operation_store import AdminOperationStorePort
from app.ports.clock import ClockPort
from app.ports.errors import PortUnavailableError


def _digest(value: object) -> str:
    return sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class HistoryPreview:
    plan: RAGCleanupPlan | None
    storage: HistoryStorageFacts | None
    cascade_authorized: bool
    confirmation: AdminConfirmation | None


@dataclass(frozen=True, slots=True)
class ProfilePreview:
    handoff: RAGProfileHandoffState
    estimated_jobs: int
    confirmation: AdminConfirmation | None


@dataclass(frozen=True, slots=True)
class GenerationPreview:
    action: AdminActionKind
    generation_id: str
    affected_jobs: int
    confirmation: AdminConfirmation | None


class AdminService:
    def __init__(
        self, *, settings: SettingsService, maintenance: RAGMaintenanceOperations,
        store: AdminOperationStorePort, clock: ClockPort,
        profile_for_id: Callable[[str], EmbeddingProfile],
        apply_profile: Callable[[EmbeddingProfile], Awaitable[str]],
        notify_maintenance: Callable[[], None],
        credential_store_available: Callable[[], Awaitable[bool]] | None = None,
        confirmations: ConfirmationRegistry[AdminCommand] | None = None,
        id_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._settings = settings
        self._maintenance = maintenance
        self._store = store
        self._clock = clock
        self._profile_for_id = profile_for_id
        self._apply_profile = apply_profile
        self._notify_maintenance = notify_maintenance
        self._credential_store_available = credential_store_available
        self._confirmations = confirmations or ConfirmationRegistry()
        self._id_factory = id_factory
        self._schedule: Callable[[AdminOperation, AdminCommand], None] | None = None
        self._accept_tasks: set[asyncio.Task[AdminOperation]] = set()

    def bind_runner(self, schedule: Callable[[AdminOperation, AdminCommand], None]) -> None:
        self._schedule = schedule

    async def drain_accepts(self) -> None:
        if self._accept_tasks:
            await asyncio.gather(*tuple(self._accept_tasks), return_exceptions=True)

    def _accept_done(self, task: asyncio.Task[AdminOperation]) -> None:
        self._accept_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def storage_status(self) -> dict[str, str]:
        history_state = "available"
        rag_state = "available"
        try:
            await self._maintenance.list_history_run_ids(limit=1)
        except PortUnavailableError:
            history_state = "unavailable"
        try:
            await self._maintenance.inspect_index_health()
        except PortUnavailableError:
            rag_state = "unavailable"
        credential_state = "unknown"
        if self._credential_store_available is not None:
            try:
                credential_state = (
                    "available" if await self._credential_store_available() else "unavailable"
                )
            except (PortUnavailableError, CredentialStoreUnavailableError):
                credential_state = "unavailable"
        return {
            "history": history_state, "rag_index": rag_state,
            "evidence_cache": "not_present", "credential_store": credential_state,
        }

    async def rag_status(self) -> tuple[object, RAGProfileHandoffState]:
        snapshot = await self._settings.get_snapshot()
        profile = self._profile_for_id(snapshot.defaults.desired_embedding_profile_id)
        return (
            await self._maintenance.inspect_index_health(),
            await self._maintenance.inspect_profile_handoff(profile),
        )

    async def list_operations(
        self, *, limit: int, cursor: str | None,
    ) -> tuple[tuple[AdminOperation, ...], str | None]:
        return await self._store.list_recent(limit=limit, cursor=cursor)

    async def get_operation(self, operation_id: UUID) -> AdminOperation:
        operation = await self._store.get(operation_id)
        if operation is None:
            raise AdminOperationNotFoundError()
        return operation

    async def preview_history(
        self, *, run_ids: tuple[UUID, ...] | None,
        cascade_consumers: bool,
    ) -> HistoryPreview:
        all_history = run_ids is None
        if run_ids is None:
            selected = await self._maintenance.list_history_run_ids(limit=501)
            if len(selected) > 500:
                raise HistoryCleanupSelectionTooLargeError()
        else:
            selected = tuple(sorted(str(value) for value in run_ids))
        if not selected:
            return HistoryPreview(None, None, cascade_consumers, None)
        plan, storage, fingerprint = await self._history_facts(
            selected, all_history=all_history,
        )
        if len(plan.delete_run_ids) > 500 or len(plan.dependencies) > 500:
            raise HistoryCleanupSelectionTooLargeError()
        confirmation = None
        if not plan.cascade_required or cascade_consumers:
            command = AdminCommand(
                kind=AdminActionKind.HISTORY_CLEANUP,
                resource_identity="history",
                facts_digest=fingerprint,
                requested_run_ids=tuple(UUID(value) for value in selected),
                all_history=all_history,
                expected_delete_run_ids=tuple(UUID(value) for value in plan.delete_run_ids),
                expected_dependency_ids=tuple(UUID(item.link_id) for item in plan.dependencies),
                cascade_consumers=cascade_consumers,
            )
            confirmation = await self._confirmations.issue(
                action=command.kind.value,
                phrase=f"DELETE {len(plan.delete_run_ids)} RUNS",
                payload=command, now=self._clock.now(),
            )
        return HistoryPreview(plan, storage, cascade_consumers, confirmation)

    async def preview_profile_handoff(self) -> ProfilePreview:
        snapshot = await self._settings.get_snapshot()
        profile = self._profile_for_id(snapshot.defaults.desired_embedding_profile_id)
        handoff = await self._maintenance.inspect_profile_handoff(profile)
        health = await self._maintenance.inspect_index_health()
        estimated = sum(item.job_counts.total for item in health.generations
                        if item.generation_id == handoff.building_generation_id)
        if handoff.active_generation_id is not None and handoff.phase.value in {
            "reader_drain", "credential_cleanup_ready",
        }:
            return ProfilePreview(handoff, estimated, None)
        fingerprint = _digest(self._profile_facts(snapshot.revision, handoff))
        command = AdminCommand(
            kind=AdminActionKind.PROFILE_HANDOFF,
            resource_identity="rag:profile", facts_digest=fingerprint,
            desired_profile_id=profile.profile_id,
        )
        confirmation = await self._confirmations.issue(
            action=command.kind.value, phrase="APPLY RAG PROFILE",
            payload=command, now=self._clock.now(),
        )
        return ProfilePreview(handoff, estimated, confirmation)

    async def preview_failed_job_retry(
        self, generation_id: str, *, safe_error_codes: tuple[str, ...],
        limit: int,
    ) -> GenerationPreview:
        fingerprint, count = await self._retry_facts(
            generation_id, safe_error_codes, limit,
        )
        command = AdminCommand(
            kind=AdminActionKind.RETRY_FAILED_JOBS,
            resource_identity=f"rag:{generation_id}", facts_digest=fingerprint,
            generation_id=generation_id, safe_error_codes=safe_error_codes, limit=limit,
        )
        confirmation = await self._confirmations.issue(
            action=command.kind.value, phrase=f"RETRY {min(count, limit)} JOBS",
            payload=command, now=self._clock.now(),
        ) if count else None
        return GenerationPreview(command.kind, generation_id, min(count, limit), confirmation)

    async def preview_generation_deletion(self, generation_id: str) -> GenerationPreview:
        fingerprint, jobs = await self._deletion_facts(generation_id)
        command = AdminCommand(
            kind=AdminActionKind.GENERATION_DELETION,
            resource_identity=f"rag:{generation_id}", facts_digest=fingerprint,
            generation_id=generation_id,
        )
        confirmation = await self._confirmations.issue(
            action=command.kind.value, phrase="DELETE RAG GENERATION",
            payload=command, now=self._clock.now(),
        )
        return GenerationPreview(command.kind, generation_id, jobs, confirmation)

    async def execute(
        self, *, kind: AdminActionKind, token: str, phrase: str,
        idempotency_key: UUID, raw_body: bytes,
        generation_id: str | None = None,
    ) -> AdminOperation:
        task = asyncio.create_task(
            self._accept(
                kind=kind, token=token, phrase=phrase,
                idempotency_key=idempotency_key, raw_body=raw_body,
                generation_id=generation_id,
            ),
            name="admin-operation-accept",
        )
        self._accept_tasks.add(task)
        task.add_done_callback(self._accept_done)
        return await asyncio.shield(task)

    async def _accept(
        self, *, kind: AdminActionKind, token: str, phrase: str,
        idempotency_key: UUID, raw_body: bytes,
        generation_id: str | None,
    ) -> AdminOperation:
        if idempotency_key.version != 4:
            raise ValueError("Admin idempotency key must be UUID v4")
        fingerprint = sha256(raw_body).digest()
        receipt = await self._store.read_receipt(idempotency_key)
        if receipt is not None:
            if not compare_digest(receipt[1], fingerprint):
                raise UnitPolicyIdempotencyConflictError()
            return receipt[0]
        async with self._confirmations.claim(
            token=token, action=kind.value, phrase=phrase, now=self._clock.now(),
        ) as (command, token_digest):
            if generation_id is not None and command.generation_id != generation_id:
                raise AdminConfirmationInvalidError()
            if not compare_digest(await self._current_digest(command), command.facts_digest):
                raise AdminPreviewStaleError()
            total = (
                len(command.expected_delete_run_ids)
                if kind is AdminActionKind.HISTORY_CLEANUP
                else command.limit if kind is AdminActionKind.RETRY_FAILED_JOBS else 1
            )
            operation = AdminOperation(
                operation_id=self._id_factory(), kind=kind,
                state=AdminOperationState.PENDING,
                resource_identity=command.resource_identity,
                progress=AdminProgress(total_items=total),
                created_at=self._clock.now(),
            )
            saved = await self._store.create(
                operation, idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                confirmation_token_digest=token_digest, command=command,
            )
        if saved.state is AdminOperationState.PENDING and self._schedule is not None:
            self._schedule(saved, command)
        return saved

    async def handle(self, command: AdminCommand):
        if not compare_digest(await self._current_digest(command), command.facts_digest):
            raise AdminPreviewStaleError()
        if command.kind is AdminActionKind.HISTORY_CLEANUP:
            before = await self._maintenance.inspect_history_storage(
                tuple(str(value) for value in command.expected_delete_run_ids)
            )
            result = await self._maintenance.delete_history_runs(
                tuple(str(value) for value in command.requested_run_ids),
                cascade_consumers=command.cascade_consumers,
                expected_delete_run_ids=tuple(str(value) for value in command.expected_delete_run_ids),
                expected_dependency_ids=tuple(str(value) for value in command.expected_dependency_ids),
            )
            return HistoryCleanupResult(
                deleted_run_ids=tuple(UUID(value) for value in result.deleted_run_ids),
                retained_idempotency_tombstones=before.idempotency_keys,
            )
        if command.kind is AdminActionKind.PROFILE_HANDOFF:
            profile = self._profile_for_id(command.desired_profile_id)
            active_id = await self._apply_profile(profile)
            return ProfileHandoffResult(active_generation_id=active_id)
        if command.kind is AdminActionKind.RETRY_FAILED_JOBS:
            jobs = await self._maintenance.retry_failed_jobs(
                command.generation_id,
                RAGFailedJobRetryFilter(
                    safe_error_codes=command.safe_error_codes, limit=command.limit,
                ),
            )
            self._notify_maintenance()
            return RetryFailedJobsResult(retried_jobs=len(jobs))
        await self._maintenance.delete_retired_generation(command.generation_id)
        return GenerationDeletionResult(deleted_generation_id=command.generation_id)

    async def _current_digest(self, command: AdminCommand) -> str:
        if command.kind is AdminActionKind.HISTORY_CLEANUP:
            selected = tuple(str(value) for value in command.requested_run_ids)
            if command.all_history:
                latest = await self._maintenance.list_history_run_ids(limit=501)
                if tuple(latest) != selected:
                    raise AdminPreviewStaleError()
            _, _, digest = await self._history_facts(selected, all_history=command.all_history)
            return digest
        if command.kind is AdminActionKind.PROFILE_HANDOFF:
            snapshot = await self._settings.get_snapshot()
            if snapshot.defaults.desired_embedding_profile_id != command.desired_profile_id:
                raise AdminPreviewStaleError()
            profile = self._profile_for_id(command.desired_profile_id)
            handoff = await self._maintenance.inspect_profile_handoff(profile)
            return _digest(self._profile_facts(snapshot.revision, handoff))
        if command.kind is AdminActionKind.RETRY_FAILED_JOBS:
            digest, _ = await self._retry_facts(
                command.generation_id, command.safe_error_codes, command.limit,
            )
            return digest
        digest, _ = await self._deletion_facts(command.generation_id)
        return digest

    async def _history_facts(
        self, selected: tuple[str, ...], *, all_history: bool,
    ) -> tuple[RAGCleanupPlan, HistoryStorageFacts, str]:
        plan = await self._maintenance.inspect_cleanup_dependencies(selected)
        storage = await self._maintenance.inspect_history_storage(plan.delete_run_ids)
        if set(storage.run_ids) != set(plan.delete_run_ids) or not storage.all_terminal:
            raise HistoryCleanupDependencyConflictError()
        digest = _digest({
            "selection": selected, "all_history": all_history,
            "plan": plan.model_dump(mode="json"),
            "storage": {
                "run_ids": storage.run_ids, "sessions": storage.sessions,
                "transcript_items": storage.transcript_items,
                "idempotency_keys": storage.idempotency_keys,
            },
        })
        return plan, storage, digest

    @staticmethod
    def _profile_facts(revision: int, handoff: RAGProfileHandoffState) -> dict[str, object]:
        return {"settings_revision": revision, "handoff": handoff.model_dump(mode="json")}

    async def _retry_facts(
        self, generation_id: str, safe_error_codes: tuple[str, ...], limit: int,
    ) -> tuple[str, int]:
        health = await self._maintenance.inspect_index_health()
        generation = next((item for item in health.generations
                           if item.generation_id == generation_id), None)
        if generation is None:
            raise RAGGenerationNotFoundError()
        if generation.status is not RAGIndexGenerationStatus.BUILDING:
            raise RAGGenerationConflictError()
        available = await self._maintenance.failed_job_codes(generation_id)
        if not set(safe_error_codes) <= set(available):
            raise RAGGenerationConflictError()
        count = await self._maintenance.count_failed_jobs(generation_id, safe_error_codes)
        return _digest({
            "generation_id": generation_id, "profile_digest": generation.profile_digest,
            "status": generation.status.value, "codes": available,
            "selected_codes": safe_error_codes, "matching_jobs": count, "limit": limit,
        }), count

    async def _deletion_facts(self, generation_id: str) -> tuple[str, int]:
        try:
            generation = await self._maintenance.inspect_generation_deletion(generation_id)
        except RAGMaintenanceConflictError as error:
            if "does not exist" in str(error):
                raise RAGGenerationNotFoundError() from error
            if "retention" in str(error):
                raise RAGRetentionNotElapsedError() from error
            if "readers" in str(error):
                raise RAGReaderDrainRequiredError() from error
            raise RAGGenerationConflictError() from error
        health = await self._maintenance.inspect_index_health()
        item = next((value for value in health.generations
                     if value.generation_id == generation_id), None)
        if item is None:
            raise RAGGenerationConflictError()
        return _digest({
            "generation": generation.model_dump(mode="json"),
            "active": health.active_generation_id, "readers": item.reader_count,
            "jobs": item.job_counts.model_dump(mode="json"),
        }), item.job_counts.total

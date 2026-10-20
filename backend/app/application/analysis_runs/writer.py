import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from math import isfinite
from typing import Protocol
from uuid import UUID

from app.application.analysis_runs.creation import (
    RunCreationCandidate,
    RunCreationResult,
)
from app.application.analysis_runs.models import (
    AnalysisRunSnapshot,
    PublicError,
    RunStatus,
    RunStateMutation,
    SessionStateMutation,
    TranscriptItemIdentity,
)
from app.application.analysis_runs.persistence import (
    BatchVerification,
    PersistenceGroup,
    PersistenceInfo,
    PersistenceState,
    PersistenceWriteBatch,
    PersistenceWriteConflictError,
    RAGConsumptionLink,
)
from app.application.transcripts.buffer import (
    TranscriptBuffer,
)
from app.application.transcripts.models import TranscriptContentRef
from app.ports.transcript_store import TranscriptStorePort


@dataclass(frozen=True)
class _BatchFreezePlan:
    run_id: UUID
    groups: tuple[PersistenceGroup, ...]
    transcript_buffer: TranscriptBuffer
    item_ids: tuple[UUID, ...]
    persisted: tuple[TranscriptContentRef, ...]
    generating: tuple[TranscriptContentRef, ...]
    run_mutations: tuple[RunStateMutation, ...]
    session_mutations: tuple[SessionStateMutation, ...]
    rag_consumption_links: tuple[RAGConsumptionLink, ...]


class WriterRuntime(Protocol):
    snapshot: AnalysisRunSnapshot
    transcript_buffer: TranscriptBuffer
    transcript_items: list[TranscriptItemIdentity]
    state_mutations: list[RunStateMutation | SessionStateMutation]
    persistence_groups: list[PersistenceGroup]
    rag_consumption_links: dict[UUID, tuple[RAGConsumptionLink, ...]]
    persistence_active_batch: PersistenceWriteBatch | None
    persisted_item_ids: set[UUID]
    persisted_mutation_ids: set[UUID]
    persistence_failures: int
    persistence_exhausted: bool
    persistence_unknown: bool
    persistence_warning: PublicError | None
    third_retry_failed_at: float | None
    condition: asyncio.Condition
    writer_wakeup: asyncio.Event
    writer_stop: asyncio.Event
    writer_quiesced: asyncio.Event


class ActivityLogger(Protocol):
    def __call__(
        self,
        event_name: str,
        runtime: WriterRuntime,
        *,
        exception: BaseException | None = None,
        error_id: UUID | None = None,
        level: int | None = None,
        **fields: object,
    ) -> None: ...


class TranscriptWriter:
    def __init__(
        self,
        store: TranscriptStorePort[
            RunCreationCandidate,
            RunCreationResult,
            PersistenceWriteBatch,
            BatchVerification,
        ],
        *,
        error_id_factory: Callable[[], UUID],
        recovered: Callable[[WriterRuntime, UUID], None],
        log_activity: ActivityLogger,
        inline_content_bytes: int,
        preview_bytes: int,
        batch_items: int,
        retry_delays: tuple[float, ...],
        attempt_timeout: float,
        completion_delivery_grace: float,
        sleep: Callable[[float], Awaitable[None]],
        terminal_persisted: Callable[[UUID], None] | None = None,
    ) -> None:
        if type(batch_items) is not int or batch_items <= 0:
            raise ValueError("persistence_batch_items must be a positive integer")
        if len(retry_delays) != 5 or any(
            not isfinite(delay) or delay < 0 for delay in retry_delays
        ):
            raise ValueError(
                "persistence_retry_delays must contain five finite non-negative delays"
            )
        if (
            isinstance(attempt_timeout, bool)
            or not isfinite(attempt_timeout)
            or attempt_timeout <= 0
        ):
            raise ValueError("persistence_attempt_timeout must be finite and positive")
        if (
            not isfinite(completion_delivery_grace)
            or completion_delivery_grace < 0
        ):
            raise ValueError(
                "completion_delivery_grace must be finite and non-negative"
            )
        self._store = store
        self._error_id_factory = error_id_factory
        self._recovered = recovered
        self._log_activity = log_activity
        self._inline_content_bytes = inline_content_bytes
        self._preview_bytes = preview_bytes
        self._batch_items = batch_items
        self._retry_delays = retry_delays
        self._attempt_timeout = attempt_timeout
        self._completion_delivery_grace = completion_delivery_grace
        self._sleep = sleep
        self._terminal_persisted = terminal_persisted

    def enqueue_locked(
        self,
        runtime: WriterRuntime,
        *,
        item_ids: tuple[UUID, ...] = (),
        mutation_ids: tuple[UUID, ...] = (),
        recovery_fact: bool = False,
    ) -> None:
        runtime.persistence_groups.append(
            PersistenceGroup(
                item_ids=item_ids,
                mutation_ids=mutation_ids,
                recovery_fact=recovery_fact,
            )
        )
        runtime.writer_wakeup.set()

    async def run(self, runtime: WriterRuntime) -> None:
        try:
            while True:
                try:
                    await runtime.writer_wakeup.wait()
                    runtime.writer_wakeup.clear()
                    while True:
                        async with runtime.condition:
                            if runtime.writer_stop.is_set():
                                return
                            plan = self._freeze_batch_locked(runtime)
                            if plan is None:
                                break
                        if isinstance(plan, _BatchFreezePlan):
                            batch = await asyncio.to_thread(
                                self._build_batch_from_plan, plan
                            )
                            async with runtime.condition:
                                if runtime.writer_stop.is_set():
                                    return
                                runtime.persistence_active_batch = batch
                        else:
                            batch = plan
                        succeeded = await self._attempt_batch(runtime, batch)
                        if succeeded:
                            continue
                        async with runtime.condition:
                            if runtime.persistence_exhausted:
                                break
                            delay = self._retry_delays[
                                runtime.persistence_failures - 1
                            ]
                        await self._sleep(delay)
                        if runtime.writer_stop.is_set():
                            return
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    async with runtime.condition:
                        self._record_failure_locked(
                            runtime,
                            error,
                            retryable=False,
                        )
                        runtime.persistence_exhausted = True
                        runtime.condition.notify_all()
        finally:
            runtime.writer_quiesced.set()
            async with runtime.condition:
                runtime.condition.notify_all()

    def info_locked(self, runtime: WriterRuntime) -> PersistenceInfo:
        if runtime.persistence_exhausted:
            state = PersistenceState.RETRY_EXHAUSTED
        elif self.has_pending_locked(runtime):
            state = PersistenceState.PENDING
        else:
            state = PersistenceState.UP_TO_DATE
        return PersistenceInfo(state=state, warning=runtime.persistence_warning)

    @staticmethod
    def has_pending_locked(runtime: WriterRuntime) -> bool:
        finalized = {item.item_id for item in runtime.transcript_buffer.items}
        reserved = {item.item_id for item in runtime.transcript_items}
        mutation_ids = {
            mutation.mutation_id for mutation in runtime.state_mutations
        }
        return bool(
            reserved - finalized
            or finalized - runtime.persisted_item_ids
            or mutation_ids - runtime.persisted_mutation_ids
            or runtime.persistence_groups
            or runtime.persistence_active_batch is not None
            or runtime.persistence_unknown
        )

    def retry_locked(self, runtime: WriterRuntime) -> PersistenceInfo:
        if runtime.persistence_exhausted and self.has_pending_locked(runtime):
            runtime.persistence_exhausted = False
            runtime.persistence_failures = 0
            runtime.third_retry_failed_at = None
            runtime.writer_wakeup.set()
            runtime.condition.notify_all()
        return self.info_locked(runtime)

    async def wait_for_required(
        self,
        runtime: WriterRuntime,
        required_item_ids: set[UUID],
        required_mutation_ids: set[UUID],
    ) -> PublicError | None:
        while True:
            async with runtime.condition:
                if (
                    required_item_ids <= runtime.persisted_item_ids
                    and required_mutation_ids <= runtime.persisted_mutation_ids
                ):
                    return None
                if runtime.persistence_exhausted:
                    return runtime.persistence_warning or self.error_for(
                        RuntimeError("persistence writer is unavailable")
                    )
                failed_at = runtime.third_retry_failed_at
                if failed_at is None:
                    await runtime.condition.wait()
                    continue
                remaining = (
                    failed_at
                    + self._completion_delivery_grace
                    - asyncio.get_running_loop().time()
                )
                if remaining <= 0:
                    return runtime.persistence_warning or self.error_for(
                        RuntimeError("persistence delivery window expired")
                    )
                try:
                    async with asyncio.timeout(remaining):
                        await runtime.condition.wait()
                except TimeoutError:
                    return runtime.persistence_warning or self.error_for(
                        RuntimeError("persistence delivery window expired")
                    )

    async def drain(self, runtime: WriterRuntime) -> None:
        async with runtime.condition:
            await runtime.condition.wait_for(
                lambda: runtime.persistence_exhausted
                or not (
                    runtime.persistence_groups
                    or runtime.persistence_active_batch is not None
                    or runtime.persistence_unknown
                )
            )

    def task_done(self, runtime: WriterRuntime, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._log_activity(
                "persistence_writer_failed",
                runtime,
                exception=error,
            )

    def error_for(
        self,
        error: BaseException,
        *,
        retryable: bool = True,
    ) -> PublicError:
        del error
        return PublicError(
            error_id=self._error_id_factory(),
            code=(
                "persistence_write_failed"
                if retryable
                else "persistence_write_conflict"
            ),
            message=(
                "Some analysis records have not been saved yet."
                if retryable
                else "Some analysis records conflict with stored history."
            ),
            failed_stage="persist_transcript",
            retryable=retryable,
        )

    def _freeze_batch_locked(
        self,
        runtime: WriterRuntime,
    ) -> PersistenceWriteBatch | _BatchFreezePlan | None:
        if runtime.persistence_exhausted:
            return None
        if runtime.persistence_active_batch is not None:
            return runtime.persistence_active_batch
        selected: list[PersistenceGroup] = []
        selected_items = 0
        selected_item_ids: set[UUID] = set()
        finalized = {
            item.item_id: item for item in runtime.transcript_buffer.items
        }
        reserved_ids = {item.item_id for item in runtime.transcript_items}
        persisted_ids = runtime.persisted_item_ids
        for group in runtime.persistence_groups:
            if selected and (
                group.recovery_fact
                or selected[-1].recovery_fact
                or selected_items + len(group.item_ids) > self._batch_items
            ):
                break
            group_ids = set(group.item_ids)
            if selected_item_ids & group_ids:
                raise ValueError("selected transcript item identities must be unique")
            if not group_ids <= finalized.keys():
                raise ValueError("selected transcript item is not finalized")
            available_ids = persisted_ids | selected_item_ids | group_ids
            waiting = False
            for item_id in group.item_ids:
                for reference in finalized[item_id].content.references:
                    if reference in available_ids:
                        continue
                    if reference in reserved_ids:
                        waiting = True
                        continue
                    raise ValueError("transcript reference target does not exist")
            if waiting:
                continue
            selected.append(group)
            selected_item_ids.update(group_ids)
            selected_items += len(group.item_ids)
            if group.recovery_fact or selected_items >= self._batch_items:
                break
        if not selected:
            return None
        return self._prepare_batch_locked(runtime, tuple(selected))

    def _prepare_batch_locked(
        self,
        runtime: WriterRuntime,
        groups: tuple[PersistenceGroup, ...],
    ) -> _BatchFreezePlan:
        item_ids = tuple(item_id for group in groups for item_id in group.item_ids)
        mutation_ids = {
            mutation_id for group in groups for mutation_id in group.mutation_ids
        }
        selected_item_ids = set(item_ids)
        persisted = tuple(
            TranscriptContentRef(run_id=runtime.snapshot.run_id, item_id=item_id)
            for item_id in runtime.persisted_item_ids
        )
        generating = tuple(
            TranscriptContentRef(
                run_id=runtime.snapshot.run_id,
                item_id=item.item_id,
            )
            for item in runtime.transcript_items
            if item.item_id not in runtime.persisted_item_ids
            and item.item_id not in selected_item_ids
        )
        run_mutations = tuple(
            mutation
            for mutation in runtime.state_mutations
            if isinstance(mutation, RunStateMutation)
            and mutation.mutation_id in mutation_ids
        )
        session_mutations = tuple(
            mutation
            for mutation in runtime.state_mutations
            if isinstance(mutation, SessionStateMutation)
            and mutation.mutation_id in mutation_ids
        )
        return _BatchFreezePlan(
            run_id=runtime.snapshot.run_id,
            groups=groups,
            transcript_buffer=runtime.transcript_buffer.snapshot_for_storage(),
            item_ids=item_ids,
            persisted=persisted,
            generating=generating,
            run_mutations=run_mutations,
            session_mutations=session_mutations,
            rag_consumption_links=tuple(
                link
                for item_id in item_ids
                for link in runtime.rag_consumption_links.get(item_id, ())
            ),
        )

    def _build_batch_from_plan(
        self,
        plan: _BatchFreezePlan,
    ) -> PersistenceWriteBatch:
        transcript = plan.transcript_buffer.to_storage_batch(
            inline_content_bytes=self._inline_content_bytes,
            preview_bytes=self._preview_bytes,
            persisted=plan.persisted,
            generating=plan.generating,
            item_ids=plan.item_ids,
        )
        return PersistenceWriteBatch(
            run_id=plan.run_id,
            groups=plan.groups,
            transcript=transcript,
            run_mutations=plan.run_mutations,
            session_mutations=plan.session_mutations,
            rag_consumption_links=plan.rag_consumption_links,
        )

    async def _attempt_batch(
        self,
        runtime: WriterRuntime,
        batch: PersistenceWriteBatch,
    ) -> bool:
        try:
            async with asyncio.timeout(self._attempt_timeout):
                if runtime.persistence_unknown:
                    verification = await self._store.verify_batch(batch)
                    if verification is BatchVerification.NOT_COMMITTED:
                        async with runtime.condition:
                            runtime.persistence_unknown = False
                        raise RuntimeError(
                            "the previous persistence attempt rolled back"
                        )
                else:
                    try:
                        await self._store.write_batch(batch)
                    except PersistenceWriteConflictError:
                        raise
                    except Exception as error:
                        async with runtime.condition:
                            runtime.persistence_unknown = True
                        verification = await self._store.verify_batch(batch)
                        if verification is BatchVerification.NOT_COMMITTED:
                            async with runtime.condition:
                                runtime.persistence_unknown = False
                            raise RuntimeError(
                                "the persistence attempt did not commit"
                            ) from error
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            async with runtime.condition:
                runtime.persistence_unknown = True
                self._record_failure_locked(runtime, error)
                runtime.condition.notify_all()
            return False
        except PersistenceWriteConflictError as error:
            async with runtime.condition:
                self._record_failure_locked(
                    runtime,
                    error,
                    retryable=False,
                )
                runtime.persistence_failures = 6
                runtime.persistence_exhausted = True
                runtime.condition.notify_all()
            return False
        except Exception as error:
            async with runtime.condition:
                self._record_failure_locked(runtime, error)
                runtime.condition.notify_all()
            return False
        async with runtime.condition:
            self._confirm_batch_locked(runtime, batch)
            runtime.condition.notify_all()
        if self._terminal_persisted is not None and any(
            mutation.snapshot.status
            in {
                RunStatus.COMPLETED,
                RunStatus.COMPLETED_WITH_PERSISTENCE_ERROR,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
            }
            for mutation in batch.run_mutations
        ):
            try:
                self._terminal_persisted(batch.run_id)
            except Exception as error:
                self._log_activity(
                    "run_terminal_wake_failed",
                    runtime,
                    exception=error,
                )
        return True

    def _record_failure_locked(
        self,
        runtime: WriterRuntime,
        error: BaseException,
        *,
        retryable: bool = True,
    ) -> None:
        runtime.persistence_failures += 1
        if runtime.persistence_warning is None or (
            not retryable and runtime.persistence_warning.retryable
        ):
            runtime.persistence_warning = self.error_for(
                error,
                retryable=retryable,
            )
        if runtime.persistence_failures == 4:
            runtime.third_retry_failed_at = asyncio.get_running_loop().time()
        if runtime.persistence_failures >= 6:
            runtime.persistence_exhausted = True
        self._log_activity(
            "persistence_write_failed",
            runtime,
            exception=error,
            error_id=runtime.persistence_warning.error_id,
            level=(
                logging.ERROR
                if runtime.persistence_exhausted or not retryable
                else logging.WARNING
            ),
            attempt=runtime.persistence_failures,
        )

    def _confirm_batch_locked(
        self,
        runtime: WriterRuntime,
        batch: PersistenceWriteBatch,
    ) -> None:
        runtime.persisted_item_ids.update(batch.item_ids)
        runtime.persisted_mutation_ids.update(batch.mutation_ids)
        selected = set(batch.groups)
        runtime.persistence_groups[:] = [
            group for group in runtime.persistence_groups if group not in selected
        ]
        runtime.persistence_active_batch = None
        runtime.persistence_unknown = False
        warning = runtime.persistence_warning
        runtime.persistence_failures = 0
        runtime.persistence_exhausted = False
        runtime.third_retry_failed_at = None
        if warning is not None and not batch.is_recovery_fact:
            self._recovered(runtime, warning.error_id)
            self._log_activity(
                "persistence_write_recovered",
                runtime,
                error_id=warning.error_id,
            )
        runtime.persistence_warning = None
        if runtime.persistence_groups:
            runtime.writer_wakeup.set()

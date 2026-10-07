from uuid import UUID

from pydantic import model_validator

from app.application.analysis_runs.models import (
    AgentSessionStatus,
    PublicError,
    RunStatus,
    RunStateMutation,
    SessionStateMutation,
)
from app.application.transcripts.models import TranscriptStorageBatch
from app.domain.common import UtcDatetime
from app.domain.domain_enum import DomainEnum
from app.domain.domain_model import DomainModel


class PersistenceState(DomainEnum):
    UP_TO_DATE = "up_to_date"
    PENDING = "pending"
    RETRY_EXHAUSTED = "retry_exhausted"


class PersistenceInfo(DomainModel):
    state: PersistenceState
    warning: PublicError | None = None


class PersistenceGroup(DomainModel):
    item_ids: tuple[UUID, ...] = ()
    mutation_ids: tuple[UUID, ...] = ()
    recovery_fact: bool = False

    @model_validator(mode="after")
    def validate_group(self) -> "PersistenceGroup":
        if not self.item_ids and not self.mutation_ids:
            raise ValueError("persistence group must contain work")
        if len(self.item_ids) != len(set(self.item_ids)):
            raise ValueError("persistence group item identities must be unique")
        if len(self.mutation_ids) != len(set(self.mutation_ids)):
            raise ValueError("persistence group mutation identities must be unique")
        return self


class RAGConsumptionSource(DomainModel):
    """Indexed source identity attached to one persisted consumer item."""

    source_generation_id: UUID
    source_case_id: UUID
    source_result_item_id: UUID


class RAGConsumptionLink(DomainModel):
    """Atomic ownership edge persisted with its consumer transcript item."""

    link_id: UUID
    source_generation_id: UUID
    source_case_id: UUID
    source_result_item_id: UUID
    consumer_run_id: UUID
    consumer_session_id: UUID
    consumer_item_id: UUID
    created_at: UtcDatetime


class PersistenceWriteBatch(DomainModel):
    run_id: UUID
    groups: tuple[PersistenceGroup, ...]
    transcript: TranscriptStorageBatch = TranscriptStorageBatch()
    run_mutations: tuple[RunStateMutation, ...] = ()
    session_mutations: tuple[SessionStateMutation, ...] = ()
    rag_consumption_links: tuple[RAGConsumptionLink, ...] = ()

    @property
    def item_ids(self) -> tuple[UUID, ...]:
        return tuple(item.item_id for item in self.transcript.items)

    @property
    def mutation_ids(self) -> tuple[UUID, ...]:
        return tuple(
            mutation.mutation_id
            for mutation in (*self.run_mutations, *self.session_mutations)
        )

    @property
    def is_recovery_fact(self) -> bool:
        return any(group.recovery_fact for group in self.groups)

    @model_validator(mode="after")
    def validate_batch(self) -> "PersistenceWriteBatch":
        if not self.groups:
            raise ValueError("persistence batch must contain groups")
        if len(set(self.groups)) != len(self.groups):
            raise ValueError("persistence batch groups must be unique")
        expected_items = tuple(
            item_id for group in self.groups for item_id in group.item_ids
        )
        expected_mutations = tuple(
            mutation_id for group in self.groups for mutation_id in group.mutation_ids
        )
        if len(expected_items) != len(set(expected_items)):
            raise ValueError("persistence batch item identities must be unique")
        if len(expected_mutations) != len(set(expected_mutations)):
            raise ValueError("persistence batch mutation identities must be unique")
        if set(expected_items) != set(self.item_ids):
            raise ValueError("persistence batch items must match its groups")
        if set(expected_mutations) != set(self.mutation_ids):
            raise ValueError("persistence batch mutations must match its groups")
        if any(
            mutation.snapshot.run_id != self.run_id
            for mutation in self.run_mutations
        ) or any(
            mutation.run_id != self.run_id
            for mutation in self.session_mutations
        ):
            raise ValueError("persistence batch records must belong to one run")
        item_ids = set(self.item_ids)
        stored_items = {item.item_id: item for item in self.transcript.items}
        for mutation in (*self.run_mutations, *self.session_mutations):
            if (
                mutation.transcript_item is not None
                and mutation.transcript_item.item_id not in item_ids
            ):
                raise ValueError(
                    "a state mutation and its transcript item must be in one batch"
                )
            identity = mutation.transcript_item
            if identity is not None:
                stored = stored_items[identity.item_id]
                for field in (
                    "run_id",
                    "session_id",
                    "transcript_sequence",
                    "created_at",
                    "request_id",
                    "operation_id",
                ):
                    if getattr(stored, field) != getattr(identity, field):
                        raise ValueError(
                            "state mutation transcript identity must match its item"
                        )
                if stored.content_type != "lifecycle_event":
                    raise ValueError(
                        "state mutation transcript item must be a lifecycle event"
                    )
        link_ids = tuple(link.link_id for link in self.rag_consumption_links)
        if len(link_ids) != len(set(link_ids)):
            raise ValueError("RAG consumption link identities must be unique")
        link_owners = tuple(
            (
                link.source_generation_id,
                link.source_case_id,
                link.consumer_item_id,
            )
            for link in self.rag_consumption_links
        )
        if len(link_owners) != len(set(link_owners)):
            raise ValueError("RAG consumption ownership identities must be unique")
        for link in self.rag_consumption_links:
            consumer = stored_items.get(link.consumer_item_id)
            if (
                consumer is None
                or link.consumer_run_id != self.run_id
                or consumer.run_id != link.consumer_run_id
                or consumer.session_id != link.consumer_session_id
                or consumer.content_type != "evidence_source_records"
            ):
                raise ValueError(
                    "RAG consumption link must accompany its source-record item"
                )
        for mutation in self.run_mutations:
            terminal = mutation.snapshot.status in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
                RunStatus.COMPLETED_WITH_PERSISTENCE_ERROR,
            }
            if terminal != (mutation.transcript_item is not None):
                raise ValueError("run terminal mutation must own its lifecycle item")
            if (
                mutation.transcript_item is not None
                and mutation.transcript_item.session_id is not None
            ):
                raise ValueError("run lifecycle item must be run-owned")
        for mutation in self.session_mutations:
            terminal = mutation.summary.status in {
                AgentSessionStatus.COMPLETED,
                AgentSessionStatus.FAILED,
                AgentSessionStatus.CANCELLED,
            }
            needs_item = mutation.previous_status is None or terminal
            if needs_item != (mutation.transcript_item is not None):
                raise ValueError(
                    "Agent session creation/terminal mutation must own its lifecycle item"
                )
            if (
                mutation.transcript_item is not None
                and mutation.transcript_item.session_id
                != mutation.summary.session_id
            ):
                raise ValueError(
                    "Agent session lifecycle item must belong to its session"
                )
        return self


class BatchVerification(DomainEnum):
    COMMITTED = "committed"
    NOT_COMMITTED = "not_committed"


class PersistenceWriteConflictError(RuntimeError):
    pass
